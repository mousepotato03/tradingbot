import json
from datetime import datetime

from pydantic import ValidationError

from app.adapters.browser import BrowserAdapter
from app.adapters.fixture import FixtureAdapters
from app.adapters.http import ToolError
from app.adapters.sec import SecAdapter
from app.adapters.toss import TossAdapter
from app.adapters.web import BraveSearch, DocumentReader
from app.config import Settings
from app.evidence import evidence_is_fresh, validate_claims
from app.lifecycle import candidate_state, changes
from app.llm import FixtureModel, ModelPort, OpenAIModel, policy_prompt
from app.models import (
    CandidateState,
    PortfolioDecision,
    Rating,
    ResearchReport,
    ResearchRequest,
    ResearchSummary,
    Security,
    StageReview,
    utcnow,
)
from app.reporting import markdown
from app.storage import Store
from app.tools import (
    BrowserAction,
    CalculateInput,
    EvidenceReadInput,
    ExtractFactInput,
    SearchInput,
    SymbolInput,
    ToolRegistry,
    UrlInput,
    add_market_tools,
    add_technical_record,
)
from app.validation import validate_decision

BUDGETS = {"quick": (10, 300), "normal": (30, 900), "deep": (80, 2700), "critical": (150, 5400)}
TOKEN_BUDGETS = {"quick": 120_000, "normal": 300_000, "deep": 800_000, "critical": 1_200_000}
STAGES = [
    "bull",
    "bear",
    "bull_rebuttal",
    "bear_rebuttal",
    "research_manager",
    "aggressive_risk",
    "neutral_risk",
    "conservative_risk",
    "premortem",
]


class ResearchEngine:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        model: ModelPort | None = None,
        market=None,
        filings=None,
        search=None,
        reader=None,
    ):
        self.settings, self.store = settings, store
        if settings.mode == "fixture":
            fixture = FixtureAdapters()
            self.model, self.market = model or FixtureModel(), market or fixture
            self.filings, self.search, self.reader = (
                filings or fixture,
                search or fixture,
                reader or fixture,
            )
        elif settings.mode == "live":
            settings.require_live()
            self.model, self.market = (
                model or OpenAIModel(settings),
                market or TossAdapter(settings),
            )
            self.filings, self.search = (
                filings or SecAdapter(settings),
                search or BraveSearch(settings),
            )
            self.reader = reader or DocumentReader(settings)
        else:
            raise ValueError("mode must be fixture or live")

    def registry(self, run_id, request):
        tools = ToolRegistry(request.ticker)
        add_market_tools(tools, self.market)
        tools.add(
            "filings_read",
            SymbolInput,
            self.filings.filings,
            "Retrieve official SEC filing index; open filing URLs.",
        )
        tools.add(
            "financials_read",
            SymbolInput,
            self.filings.financials,
            "Retrieve official SEC XBRL facts with units, periods and accessions.",
        )
        tools.add(
            "web_search",
            SearchInput,
            lambda **kw: self.search.search(request.ticker, **kw),
            "Discover sources through Brave Search. Snippets are not fact evidence.",
        )
        tools.add(
            "web_read",
            UrlInput,
            lambda **kw: self.reader.read(request.ticker, **kw),
            "Open an original public HTML or PDF source and extract text.",
        )
        tools.add(
            "calculate",
            CalculateInput,
            tools.calculate,
            "Calculate only using evidence facts and explicit assumptions.",
        )
        tools.add(
            "extract_fact",
            ExtractFactInput,
            tools.extract_fact,
            "Verify an exact document quote and numeric token before extracting a fact. Labels/periods need source review.",
        )
        tools.add(
            "evidence_read",
            EvidenceReadInput,
            tools.read_evidence,
            "Search within or read another chunk of already stored evidence, including long filings.",
        )
        browser = BrowserAdapter(self.settings, run_id, tools)
        tools.add(
            "browser_open",
            UrlInput,
            browser.open,
            "Browser fallback for dynamic sources. Returns text, element refs and screenshot.",
        )
        tools.add(
            "browser_action",
            BrowserAction,
            browser.action,
            "Operate the current browser by element refs or screenshot coordinates.",
        )
        tools.records = self.store.evidence(run_id)
        return tools, browser

    def run(self, run_id: str) -> ResearchReport:
        run = self.store.get_run(run_id)
        if run["status"] == "COMPLETED":
            return self.store.get_report(run_id)
        self.store.start_run(run_id, self.settings.worker_lease_seconds)
        request = ResearchRequest.model_validate(run["request"])
        tools, browser = self.registry(run_id, request)
        checkpoint = run["checkpoint"]
        if "execution_started_at" not in checkpoint:
            checkpoint["execution_started_at"] = utcnow().isoformat()
        limit, seconds = BUDGETS[request.mode]
        historical = request.as_of is not None
        if historical:
            # Replay only persisted snapshots. Never fetch today's data into a historical run.
            previous = self.store.previous(request.ticker)
            if previous:
                for record in self.store.evidence(previous.run_id):
                    if record.retrieved_at <= request.as_of:
                        record = record.model_copy(
                            update={"evidence_id": __import__("uuid").uuid4().hex}
                        )
                        self.store.add_evidence(run_id, record)
                        tools.records.append(record)
        previous = self.store.previous(request.ticker)
        if previous and previous.fixture != (self.settings.mode == "fixture"):
            previous = None
        prior_traces = self.store.traces(run_id)
        count = sum(t.get("kind") == "tool" for t in prior_traces)
        model_calls = sum(t.get("kind") == "model" for t in prior_traces)
        token_count = sum(t.get("usage", {}).get("total_tokens", 0) for t in prior_traces)

        def exhausted():
            elapsed = (
                utcnow() - datetime.fromisoformat(checkpoint["execution_started_at"])
            ).total_seconds()
            return (
                count >= limit
                or model_calls >= limit + 24
                or elapsed >= seconds
                or token_count >= TOKEN_BUDGETS[request.mode]
            )

        def execute(name, arguments):
            nonlocal count
            if historical:
                return {"error": "HISTORICAL_NETWORK_DISABLED"}
            if exhausted():
                return {"error": "RESEARCH_BUDGET_EXHAUSTED"}
            count += 1
            before = utcnow()
            trace = {
                "kind": "tool",
                "name": name,
                "arguments": arguments,
                "started_at": before.isoformat(),
            }
            try:
                record = tools.execute(name, arguments)
                self.store.add_evidence(run_id, record)
                trace.update(
                    status="OK", evidence_ids=[record.evidence_id], source_url=record.source_url
                )
                if record.evidence_type == "ohlcv" and record.payload.get("candles"):
                    try:
                        technical = add_technical_record(tools, record)
                        self.store.add_evidence(run_id, technical)
                        tools.records.append(technical)
                    except ValueError:
                        pass
                return self._record_context(record)
            except (ToolError, ValidationError, KeyError, ValueError, ArithmeticError) as error:
                code = error.code if isinstance(error, ToolError) else type(error).__name__
                trace.update(status="ERROR", error_class=code, evidence_ids=[])
                return {"error": code, "tool": name}
            finally:
                trace["ended_at"] = utcnow().isoformat()
                self.store.trace(run_id, trace)

        def context(role, extra=None):
            return json.dumps(
                {
                    "role": role,
                    "request": request.model_dump(mode="json"),
                    "fixture": self.settings.mode == "fixture",
                    "evidence": [self._record_context(r) for r in tools.records],
                    "completed_stages": checkpoint,
                    "previous_report": previous.model_dump(mode="json") if previous else None,
                    "instructions": extra,
                },
                ensure_ascii=False,
                default=str,
            )

        def ask(role, schema, extra=None):
            nonlocal model_calls, token_count
            messages = [
                {"role": "system", "content": policy_prompt()},
                {"role": "user", "content": context(role, extra)},
            ]
            invalid_claim_attempts = 0
            for _ in range(limit + 4):
                if exhausted():
                    raise ToolError("RESEARCH_BUDGET_EXHAUSTED")
                messages[1]["content"] = context(role, extra)
                model_calls += 1
                result = self.model.complete(
                    role, messages, [] if historical else tools.contracts(), schema
                )
                token_count += result.usage.get("total_tokens", 0)
                self.store.trace(
                    run_id,
                    {
                        "kind": "model",
                        "role": role,
                        "usage": result.usage,
                        "at": utcnow().isoformat(),
                    },
                )
                if token_count >= TOKEN_BUDGETS[request.mode]:
                    raise ToolError("RESEARCH_BUDGET_EXHAUSTED")
                if result.calls:
                    messages.extend(result.continuation)
                    for call in result.calls:
                        output = execute(call.name, call.arguments)
                        messages.append(
                            {
                                "type": "function_call_output",
                                "call_id": call.call_id,
                                "output": json.dumps(output, ensure_ascii=False, default=str),
                            }
                        )
                        while tools.images:
                            messages.append(
                                {
                                    "role": "user",
                                    "content": [
                                        {"type": "input_image", "image_url": tools.images.pop(0)}
                                    ],
                                }
                            )
                    continue
                if result.result is None:
                    raise ToolError("MODEL_EMPTY")
                claims = []
                if isinstance(result.result, ResearchSummary):
                    claims = [
                        claim for section in result.result.sections for claim in section.claims
                    ]
                elif isinstance(result.result, StageReview):
                    claims = result.result.claims
                issues = validate_claims(claims, tools.records, request.as_of or utcnow())
                if issues:
                    self.store.trace(
                        run_id,
                        {
                            "kind": "claim_rejection",
                            "role": role,
                            "claims": [c.model_dump(mode="json") for c in claims],
                            "issues": [i.model_dump() for i in issues],
                        },
                    )
                    invalid_claim_attempts += 1
                    if invalid_claim_attempts > 2:
                        raise ToolError("MODEL_UNSUPPORTED_CLAIMS")
                    messages.append(
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "claim_errors": [i.model_dump() for i in issues],
                                    "instruction": "Correct unsupported claims, obtain missing evidence or explicitly report material gaps.",
                                }
                            ),
                        }
                    )
                    continue
                return result.result
            raise ToolError("RESEARCH_BUDGET_EXHAUSTED")

        try:
            self.store.checkpoint(run_id, checkpoint)
            if not tools.records and not historical:
                for name in [
                    "market_identity",
                    "market_quote",
                    "market_ohlcv",
                    "portfolio_read",
                    "fees_read",
                    "filings_read",
                    "financials_read",
                ]:
                    execute(name, {"ticker": request.ticker})
            if "research" not in checkpoint:
                research = ask("research_director", ResearchSummary)
                checkpoint["research"] = research.model_dump(mode="json")
                self.store.checkpoint(run_id, checkpoint)
            research = ResearchSummary.model_validate(checkpoint["research"])
            for role in STAGES:
                if role not in checkpoint:
                    checkpoint[role] = ask(role, StageReview).model_dump(mode="json")
                    self.store.checkpoint(run_id, checkpoint)
            quotes = [
                r
                for r in tools.records
                if r.evidence_type == "quote" and r.ticker == request.ticker
            ]
            if not historical and quotes and not evidence_is_fresh(quotes[-1], utcnow()):
                execute("market_quote", {"ticker": request.ticker})
            decision = ask("portfolio_manager", PortfolioDecision)
            rejected = []
            issues_from_research = validate_claims(
                [c for s in research.sections for c in s.claims], tools.records, utcnow()
            )
            issues_from_reviews = validate_claims(
                [c for role in STAGES for c in StageReview.model_validate(checkpoint[role]).claims],
                tools.records,
                utcnow(),
            )
            for attempt in range(3):
                validation = validate_decision(
                    decision, tools.records, request, request.as_of or utcnow()
                )
                material_gaps = research.material_gaps + [
                    gap
                    for role in STAGES
                    for gap in StageReview.model_validate(checkpoint[role]).material_gaps
                ]
                if not research.evidence_sufficient:
                    material_gaps.append(
                        "Research director has not established evidence sufficiency"
                    )
                if material_gaps and decision.rating != Rating.DEFER:
                    from app.models import ValidationIssue

                    validation.issues.append(
                        ValidationIssue(
                            code="MATERIAL_RESEARCH_GAP",
                            field="rating",
                            message="; ".join(material_gaps),
                        )
                    )
                validation.issues += issues_from_research + issues_from_reviews
                validation.valid = not validation.issues
                if validation.valid:
                    break
                rejected.append(decision)
                if attempt == 2 or exhausted():
                    break
                decision = ask(
                    "portfolio_manager",
                    PortfolioDecision,
                    {
                        "validation_errors": [i.model_dump() for i in validation.issues],
                        "retry": attempt + 1,
                        "instruction": "Resolve missing evidence or withdraw invalid precise plan. Do not silently patch numbers.",
                    },
                )
            if not validation.valid:
                decision = self._defer([i.message for i in validation.issues])
        except ToolError as error:
            if error.code not in {"RESEARCH_BUDGET_EXHAUSTED", "MODEL_UNSUPPORTED_CLAIMS"}:
                self.store.fail(run_id, error.code)
                raise
            research = ResearchSummary.model_validate(
                checkpoint.get("research")
                or {
                    "sections": [],
                    "unresolved_questions": ["Research budget exhausted"],
                    "material_gaps": ["Research incomplete"],
                    "evidence_sufficient": False,
                }
            )
            decision, rejected = (
                self._defer([error.code + ": evidence sufficiency not established"]),
                [],
            )
            validation = validate_decision(
                decision, tools.records, request, request.as_of or utcnow()
            )
        except Exception as error:
            self.store.fail(run_id, type(error).__name__)
            raise
        finally:
            browser.close()
        identity = next(
            (
                r
                for r in tools.records
                if r.evidence_type == "identity" and r.ticker == request.ticker
            ),
            None,
        )
        if (
            not identity
            or not any(
                r.evidence_type == "quote" and r.ticker == request.ticker for r in tools.records
            )
            or not any(
                r.evidence_type == "financials" and r.ticker == request.ticker and r.facts
                for r in tools.records
            )
        ):
            decision = self._defer(
                ["Verified identity, quote or official financial evidence unavailable"]
            )
        all_quotes = [
            r for r in tools.records if r.evidence_type == "quote" and r.ticker == request.ticker
        ]
        if (
            all_quotes
            and not evidence_is_fresh(all_quotes[-1], request.as_of or utcnow())
            and decision.rating != Rating.DEFER
        ):
            rejected.append(decision)
            decision = self._defer(["Current quote is stale at the decision cutoff"])
        report = ResearchReport(
            run_id=run_id,
            ticker=request.ticker,
            as_of=request.as_of or utcnow(),
            created_at=utcnow(),
            fixture=self.settings.mode == "fixture",
            security=Security.model_validate(identity.payload) if identity else None,
            research=research,
            reviews={
                role: StageReview.model_validate(checkpoint[role])
                for role in STAGES
                if role in checkpoint
            },
            decision=decision,
            validation=validation,
            rejected_decisions=rejected,
            evidence_ids=[r.evidence_id for r in tools.records],
            candidate_state=CandidateState.RESEARCH,
            previous_report_id=previous.run_id if previous else None,
            tool_calls=count,
            limitations=["Synthetic offline fixture"] if self.settings.mode == "fixture" else [],
        )
        report.candidate_state = candidate_state(report, tools.records, previous)
        events = (
            changes(
                previous,
                report,
                tools.records,
                self.store.evidence(previous.run_id) if previous else [],
            )
            if request.report_policy != "none"
            else []
        )
        if request.report_policy == "always" and not events:
            events = changes(None, report)
        self.store.commit_report(report, markdown(report, tools.records), events)
        return report

    @staticmethod
    def _record_context(record):
        data = record.model_dump(mode="json")
        data["text"] = data["text"][:24000]
        if record.evidence_type == "ohlcv":
            data["payload"] = {**data["payload"], "candles": data["payload"]["candles"][-20:]}
        return data

    @staticmethod
    def _defer(gaps):
        return PortfolioDecision(
            rating=Rating.DEFER,
            confidence="낮음",
            new_entry_action="핵심 근거 확인까지 판단 보류",
            holder_action="기존 보유 상태를 확인하고 부족한 근거를 재조사",
            executive_summary="핵심 증거 또는 거래안 검증이 부족해 판단을 보류한다.",
            thesis=[],
            thesis_state="UNKNOWN",
            invalidation_conditions=[],
            monitoring_checklist=["누락·충돌 근거 재확인"],
            material_changes=[],
            material_gaps=gaps,
            trade_plan=None,
        )
