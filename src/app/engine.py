import json
from datetime import datetime
from decimal import Decimal

from pydantic import ValidationError

from app.adapters.browser import BrowserAdapter
from app.adapters.fixture import FixtureAdapters
from app.adapters.http import ToolError
from app.adapters.sec import SecAdapter
from app.adapters.toss import TossAdapter
from app.adapters.web import BraveSearch, DocumentReader, TavilySearch
from app.config import Settings
from app.evidence import (
    content_hash,
    evidence_is_fresh,
    normalize_span,
    quotation_record,
    validate_claims,
)
from app.lifecycle import (
    candidate_state,
    changes,
    guard_of,
    inherit_guard,
    own_guard,
    report_alert,
)
from app.llm import FixtureModel, ModelPort, OpenAIModel, policy_prompt
from app.models import (
    CandidateState,
    EvidenceRecord,
    HolderAction,
    MaterialGap,
    NewEntryAction,
    PortfolioDecision,
    Rating,
    ResearchManagerReview,
    ResearchReport,
    ResearchRequest,
    ResearchSummary,
    Security,
    StageReview,
    ValidationIssue,
    ValidationResult,
    new_evidence_id,
    utcnow,
)
from app.reporting import markdown
from app.schedule import next_research_at
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

# (model-requested tool calls, seconds). Reasoning-model calls take 15-30 s each and every
# mode runs twelve stages, so elapsed time must cover at least that.
BUDGETS = {"quick": (10, 900), "normal": (30, 1800), "deep": (80, 3600), "critical": (150, 5400)}
# Cost-weighted tokens (see billable_tokens). A live quick run used ~320k; a run cut off before
# the PM wastes everything spent, so the ceilings keep a margin for correction retries.
TOKEN_BUDGETS = {"quick": 450_000, "normal": 900_000, "deep": 1_800_000, "critical": 3_000_000}
DEBATE_STAGES = [
    "bull",
    "bear",
    "bull_rebuttal",
    "bear_rebuttal",
    "research_manager",
]
RISK_STAGES = [
    "aggressive_risk",
    "neutral_risk",
    "conservative_risk",
    "premortem",
]
STAGES = DEBATE_STAGES + RISK_STAGES
STAGE_SCHEMAS = {"research_manager": ResearchManagerReview}
# Context limits: the model sees an index and short previews and reads details on demand.
INDEX_FACTS = 40
INDEX_SNIPPET = 300
TOOL_TEXT_PREVIEW = 4000
FULL_TOOL_OUTPUTS = 6
# "metrics" duplicates the technical facts; the facts are what claims must reference.
HEAVY_PAYLOAD_KEYS = {
    "pages",
    "candles",
    "elements",
    "downloads",
    "calendar",
    "observations",
    "metrics",
}
# Cached input is billed at roughly a tenth of fresh input; budgets follow cost.
CACHED_INPUT_WEIGHT = 0.1


def billable_tokens(usage: dict) -> float:
    """Cost-weighted tokens; falls back to total_tokens when no breakdown is reported."""
    if "input_tokens" not in usage:
        return usage.get("total_tokens", 0)
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0
    fresh = usage.get("input_tokens", 0) - cached
    return fresh + CACHED_INPUT_WEIGHT * cached + usage.get("output_tokens", 0)


def parse_review(role: str, data: dict) -> StageReview:
    """Checkpoints from before ResearchManagerReview existed load as plain reviews."""
    schema = STAGE_SCHEMAS.get(role, StageReview)
    try:
        return schema.model_validate(data)
    except ValidationError:
        return StageReview.model_validate(data)


def _risk_terms(decision: PortfolioDecision) -> dict:
    """Terms requiring renewed risk review; explanatory wording and citations may change."""

    def amount(value: Decimal | None):
        if value is None:
            return None
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text

    plan, guard = decision.trade_plan, decision.position_guard
    return {
        "rating": decision.rating.value,
        "new_entry_action": decision.new_entry_action.value,
        "holder_action": decision.holder_action.value,
        "thesis_state": decision.thesis_state,
        "invalidation_conditions": decision.invalidation_conditions,
        "trade_plan": {
            "currency": plan.currency,
            "entry_low": amount(plan.entry_low.value),
            "entry_high": amount(plan.entry_high.value),
            "stop": amount(plan.stop.value),
            "targets": [amount(level.value) for level in plan.targets],
            "quantity": amount(plan.quantity),
            "reward_risk": amount(plan.reward_risk),
            "sizing_unit": plan.sizing_unit,
            "conditions": plan.conditions,
            "invalidation": plan.invalidation,
            "horizon": plan.horizon,
            "no_trade_conditions": plan.no_trade_conditions,
        }
        if plan
        else None,
        "position_guard": {
            "currency": guard.currency,
            "stop": amount(guard.stop.value),
            "take_profit": [amount(level.value) for level in guard.take_profit],
        }
        if guard
        else None,
    }


def _compact(value, depth=0):
    if isinstance(value, dict):
        return {
            key: _compact(item, depth + 1)
            for key, item in value.items()
            if key not in HEAVY_PAYLOAD_KEYS and depth < 3
        }
    if isinstance(value, list):
        items = [_compact(item, depth + 1) for item in value[:10]]
        return items + ([{"omitted_items": len(value) - 10}] if len(value) > 10 else [])
    if isinstance(value, str):
        return value[:300]
    return value


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
            provider = TavilySearch if settings.search_provider == "tavily" else BraveSearch
            self.filings, self.search = (
                filings or SecAdapter(settings),
                search or provider(settings),
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
            "Retrieve official SEC XBRL facts (US-GAAP or IFRS) with units, periods, accessions.",
        )
        tools.add(
            "fund_holdings_read",
            SymbolInput,
            self.filings.fund_holdings,
            "ETF only: latest SEC N-PORT holdings, net assets, asset/country mix and the "
            "summary prospectus link (expense ratio, index, strategy).",
        )

        def search(**kwargs):
            # Local cap on limited search credits, independent of provider billing.
            if self.settings.mode == "live" and not self.store.consume_search(
                self.settings.search_provider, run_id, self.settings.search_monthly_limit
            ):
                raise ToolError("SEARCH_MONTHLY_LIMIT")
            return self.search.search(request.ticker, **kwargs)

        tools.add(
            "web_search",
            SearchInput,
            search,
            "Discover sources by web search; optional freshness, domain, country, language, "
            "offset and topic (general/news/finance). Snippets are not fact evidence. "
            "SEARCH_MONTHLY_LIMIT or SEARCH_QUOTA_EXHAUSTED means no more searches: continue "
            "with official tools and known source URLs.",
        )
        tools.add(
            "web_read",
            UrlInput,
            lambda **kw: self.reader.read(request.ticker, **kw),
            "Open an original public HTML or PDF source and extract its main text.",
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
            "Verify an exact document quote and numeric token before extracting a fact. "
            "Labels/periods need source review.",
        )
        tools.add(
            "evidence_read",
            EvidenceReadInput,
            tools.read_evidence,
            "Read stored evidence in detail: part=text (search/paginate the body), facts "
            "(filter by name) or payload. The context only carries an index and snippets.",
        )
        browser = BrowserAdapter(self.settings, run_id, tools)
        # The Chromium worker is an optional fallback; without it the model is not offered
        # tools that could only fail.
        if self.settings.browser_enabled:
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
                "Operate the current read-only browser by element refs or screenshot coordinates.",
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
        previous = self.store.previous(
            request.ticker,
            as_of=request.as_of,
            fixture=self.settings.mode == "fixture",
        )
        if previous and previous.fixture != (self.settings.mode == "fixture"):
            previous = None
        if historical and not tools.records:
            # Replay only persisted snapshots. Never fetch today's data into a historical run.
            if previous:
                self._replay(run_id, previous.run_id, request.as_of, tools)
        prior_traces = self.store.traces(run_id)
        tool_traces = [t for t in prior_traces if t.get("kind") == "tool"]
        # The mode's tool budget covers model-requested calls; baseline collection is fixed.
        count = sum(not t.get("baseline") for t in tool_traces)
        baseline_count = len(tool_traces) - count
        model_calls = sum(t.get("kind") == "model" for t in prior_traces)
        token_count = sum(billable_tokens(t.get("usage", {})) for t in prior_traces)

        def tools_left():
            return count < limit

        def exhausted():
            """Hard limits end research in 판단 보류; the tool budget only removes tools."""
            elapsed = (
                utcnow() - datetime.fromisoformat(checkpoint["execution_started_at"])
            ).total_seconds()
            return (
                model_calls >= limit + 24
                or elapsed >= seconds
                or token_count >= TOKEN_BUDGETS[request.mode]
            )

        def execute(name, arguments, baseline=False):
            nonlocal count, baseline_count
            if historical:
                return {"error": "HISTORICAL_NETWORK_DISABLED"}
            if exhausted():
                return {"error": "RESEARCH_BUDGET_EXHAUSTED"}
            if baseline:
                baseline_count += 1
            elif not tools_left():
                return {"error": "TOOL_BUDGET_EXHAUSTED", "tool": name}
            else:
                count += 1
            before = utcnow()
            trace = {
                "kind": "tool",
                "name": name,
                "arguments": arguments,
                "baseline": baseline,
                "started_at": before.isoformat(),
            }
            try:
                result = tools.execute(name, arguments)
                if not isinstance(result, EvidenceRecord):
                    # Read-only view of existing evidence: nothing new enters the ledger.
                    trace.update(status="OK", evidence_ids=[], view_of=arguments.get("evidence_id"))
                    return result
                self.store.add_evidence(run_id, result)
                trace.update(
                    status="OK", evidence_ids=[result.evidence_id], source_url=result.source_url
                )
                if result.evidence_type == "ohlcv" and result.payload.get("candles"):
                    try:
                        technical = add_technical_record(tools, result)
                        self.store.add_evidence(run_id, technical)
                        tools.records.append(technical)
                    except ValueError:
                        pass
                return self._tool_output(result)
            except (ToolError, ValidationError, KeyError, ValueError, ArithmeticError) as error:
                code = error.code if isinstance(error, ToolError) else type(error).__name__
                trace.update(status="ERROR", error_class=code, evidence_ids=[])
                return {"error": code, "tool": name}
            finally:
                trace["ended_at"] = utcnow().isoformat()
                self.store.trace(run_id, trace)

        def advisory_gaps():
            gaps = []
            if "research" in checkpoint:
                summary = ResearchSummary.model_validate(checkpoint["research"])
                gaps += [
                    {"stage": "research_director", **gap.model_dump()}
                    for gap in summary.material_gaps
                ]
            for role in STAGES:
                if role in checkpoint:
                    gaps += [
                        {"stage": role, **gap.model_dump()}
                        for gap in parse_review(role, checkpoint[role]).material_gaps
                    ]
            return gaps

        def context(role, extra=None):
            at = request.as_of or utcnow()
            return json.dumps(
                {
                    "role": role,
                    "request": request.model_dump(mode="json"),
                    "fixture": self.settings.mode == "fixture",
                    "evidence_note": (
                        "Compact evidence index with snippets. Baseline identity, quote, OHLCV "
                        "and technicals, account, fees and official filings/financials are "
                        "already collected: do not re-fetch them unless stale. Use "
                        "evidence_read for full text, all facts or payload before relying on "
                        "details. Cite numbers with numeric_references (exact name/value/unit), "
                        "not quotes. Outside the regular session a quote stays fresh until the "
                        "next regular open (payload valid_until): a closed market is not a data "
                        "gap. Its last price may include extended-hours trades, so it can differ "
                        "from the latest daily close without being a data conflict."
                    ),
                    "evidence": [self._index_entry(r, at) for r in tools.records],
                    "completed_stages": {
                        key: self._stage_digest(value)
                        for key, value in checkpoint.items()
                        if key == "research" or key in STAGES
                    },
                    "open_gaps": advisory_gaps(),
                    "tool_budget_remaining": 0 if historical else max(0, limit - count),
                    "previous_report": self._previous_summary(previous) if previous else None,
                    "trade_proposal": checkpoint.get("trade_proposal"),
                    "trade_proposal_validation": checkpoint.get("trade_proposal_validation"),
                    "instructions": extra,
                },
                ensure_ascii=False,
                default=str,
            )

        def persist_quotes(claims):
            ledger = {r.evidence_id: r for r in tools.records}
            existing = {
                (r.payload.get("source_evidence_id"), r.text)
                for r in tools.records
                if r.evidence_type == "quotation"
            }
            for claim in claims:
                for quote in claim.quotes:
                    source = ledger.get(quote.evidence_id)
                    key = (quote.evidence_id, normalize_span(quote.text))
                    if source is None or source.evidence_type == "quotation" or key in existing:
                        continue
                    record = quotation_record(quote, source)
                    self.store.add_evidence(run_id, record)
                    tools.records.append(record)
                    existing.add(key)

        def ask(role, schema, extra=None):
            nonlocal model_calls, token_count
            messages = [
                {"role": "system", "content": policy_prompt()},
                {"role": "user", "content": context(role, extra)},
            ]
            invalid_claim_attempts = 0
            closing = False
            for _ in range(limit + 4):
                if exhausted():
                    raise ToolError("RESEARCH_BUDGET_EXHAUSTED")
                messages[1]["content"] = context(role, extra)
                if not historical and not tools_left() and not closing:
                    closing = True
                    messages.append(
                        {
                            "role": "user",
                            "content": "The tool budget for this run is used up. Finish this "
                            "stage from the evidence already gathered and report what is still "
                            "missing as material gaps.",
                        }
                    )
                self._prune(messages)
                model_calls += 1
                offered = tools.contracts() if not historical and tools_left() else []
                result = self.model.complete(role, messages, offered, schema)
                token_count += billable_tokens(result.usage)
                self.store.trace(
                    run_id,
                    {
                        "kind": "model",
                        "role": role,
                        "usage": result.usage,
                        "at": utcnow().isoformat(),
                    },
                )
                # A finished answer is kept even if it crossed the budget; exhausted() stops
                # any further call at the top of the loop.
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
                    if invalid_claim_attempts > 1:
                        # Remove what still fails after one correction instead of discarding
                        # the whole stage; the removal is audited and reported as a gap.
                        failed = sorted({int(i.field) for i in issues})
                        self.store.trace(
                            run_id,
                            {
                                "kind": "claim_removal",
                                "role": role,
                                "removed": [claims[i].model_dump(mode="json") for i in failed],
                                "issues": [i.model_dump() for i in issues],
                            },
                        )
                        output = self._without_claims(result.result, set(failed))
                        persist_quotes([c for n, c in enumerate(claims) if n not in failed])
                        return output
                    messages.append(
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "claim_errors": [i.model_dump() for i in issues],
                                    "instruction": "Correct unsupported claims, quote exact "
                                    "source spans, obtain missing evidence or explicitly "
                                    "report material gaps.",
                                }
                            ),
                        }
                    )
                    continue
                persist_quotes(claims)
                return result.result
            raise ToolError("RESEARCH_BUDGET_EXHAUSTED")

        try:
            self.store.checkpoint(run_id, checkpoint)
            if not tools.records and not historical:
                self._collect_initial(request.ticker, tools, execute)
            if "research" not in checkpoint:
                research = ask("research_director", ResearchSummary)
                checkpoint["research"] = research.model_dump(mode="json")
                self.store.checkpoint(run_id, checkpoint)
            research = ResearchSummary.model_validate(checkpoint["research"])
            for role in DEBATE_STAGES:
                if role not in checkpoint:
                    schema = STAGE_SCHEMAS.get(role, StageReview)
                    checkpoint[role] = ask(role, schema).model_dump(mode="json")
                    self.store.checkpoint(run_id, checkpoint)
            binding = self._binding_gaps(
                research, parse_review("research_manager", checkpoint["research_manager"])
            )

            def refresh_quote():
                latest = [
                    r
                    for r in tools.records
                    if r.evidence_type == "quote" and r.ticker == request.ticker
                ]
                if not historical and latest and not evidence_is_fresh(latest[-1], utcnow()):
                    execute("market_quote", {"ticker": request.ticker}, baseline=True)

            def validate_current(proposal, roles=STAGES):
                # Any model call can outlast the quote window. Historical claims use the cutoff.
                refresh_quote()
                at = request.as_of or utcnow()
                result = validate_decision(
                    proposal,
                    tools.records,
                    request,
                    at,
                    blocking_gaps=binding,
                )
                claims = [c for section in research.sections for c in section.claims]
                claims += [
                    claim
                    for role in roles
                    if role in checkpoint
                    for claim in parse_review(role, checkpoint[role]).claims
                ]
                result.issues += validate_claims(claims, tools.records, at)
                result.valid = not result.issues
                return result

            def review_proposal(proposal):
                signature = content_hash(_risk_terms(proposal))
                if checkpoint.get("risk_proposal_hash") != signature:
                    # Legacy checkpoints have risk reviews but no reviewed proposal. Rebuild them.
                    for role in RISK_STAGES:
                        checkpoint.pop(role, None)
                checkpoint["trade_proposal"] = proposal.model_dump(mode="json")
                checkpoint["trade_proposal_validation"] = validate_current(
                    proposal, DEBATE_STAGES
                ).model_dump(mode="json")
                checkpoint["risk_proposal_hash"] = signature
                self.store.trace(
                    run_id,
                    {
                        "kind": "trade_proposal",
                        "proposal": checkpoint["trade_proposal"],
                        "validation": checkpoint["trade_proposal_validation"],
                        "risk_proposal_hash": signature,
                        "at": utcnow().isoformat(),
                    },
                )
                self.store.checkpoint(run_id, checkpoint)
                for role in RISK_STAGES:
                    if role not in checkpoint:
                        checkpoint[role] = ask(
                            role,
                            StageReview,
                            {
                                "instruction": "Review the current trade_proposal, including "
                                "its entry, stop, targets, size, conditions and holding guard. "
                                "Use trade_proposal_validation to identify unsupported or "
                                "invalid terms; do not treat the provisional draft as approved.",
                            },
                        ).model_dump(mode="json")
                        self.store.checkpoint(run_id, checkpoint)

            refresh_quote()
            proposal = (
                PortfolioDecision.model_validate(checkpoint["trade_proposal"])
                if "trade_proposal" in checkpoint
                else ask(
                    "trade_proposal",
                    PortfolioDecision,
                    {
                        "instruction": "Propose a concrete provisional decision for the risk "
                        "committee after the research manager. Supply supported trade levels "
                        "and holding guards where appropriate; omit unsupported sizing. This "
                        "is a draft, not the final portfolio decision.",
                    },
                )
            )
            review_proposal(proposal)
            decision = ask("portfolio_manager", PortfolioDecision)
            rejected = []
            for attempt in range(3):
                validation = validate_current(decision)
                if validation.valid and _risk_terms(decision) != _risk_terms(proposal):
                    if attempt == 2 or exhausted():
                        validation.issues.append(
                            ValidationIssue(
                                code="UNREVIEWED_PLAN",
                                field="trade_plan",
                                message="Final risk terms changed without completed committee review",
                            )
                        )
                        validation.valid = False
                    else:
                        proposal = decision
                        review_proposal(proposal)
                        decision = ask(
                            "portfolio_manager",
                            PortfolioDecision,
                            {
                                "instruction": "The revised trade_proposal has now been reviewed "
                                "by all risk perspectives. Finalize using those reviews. A "
                                "further material change requires another committee review.",
                            },
                        )
                        continue
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
                        "instruction": "Resolve missing evidence, align rating/actions/thesis "
                        "state/plan, or withdraw an invalid precise plan. Do not silently patch "
                        "numbers.",
                    },
                )
            if validation.valid:
                persist_quotes(decision.thesis + decision.material_changes)
            else:
                # A binding gap is itself the reason; other issues name the failed check.
                reasons = []
                for issue in validation.issues:
                    if issue.code == "BLOCKING_GAP":
                        reasons += binding + [g.description for g in decision.blocking_gaps()]
                    else:
                        reasons.append(f"PM 결정 검증 실패 {issue.code}: {issue.message}")
                decision = self._defer(reasons)
        except ToolError as error:
            if error.code != "RESEARCH_BUDGET_EXHAUSTED":
                self.store.fail(run_id, error.code)
                raise
            research = ResearchSummary.model_validate(
                checkpoint.get("research")
                or {
                    "sections": [],
                    "unresolved_questions": ["Research budget exhausted"],
                    "material_gaps": [
                        {"description": "Research incomplete", "severity": "blocking"}
                    ],
                    "evidence_sufficient": False,
                }
            )
            decision, rejected = (
                self._defer([f"조사 예산 소진({error.code})으로 근거 충분성을 확정하지 못함"]),
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
        security = Security.model_validate(identity.payload) if identity else None
        # ETFs have no issuer financial statements; their official basis is the N-PORT filing.
        basis = "fund_holdings" if security and security.asset_type == "etf" else "financials"
        if (
            not identity
            or not any(
                r.evidence_type == "quote" and r.ticker == request.ticker for r in tools.records
            )
            or not any(
                r.evidence_type == basis and r.ticker == request.ticker and r.facts
                for r in tools.records
            )
        ):
            official = "ETF 보유종목(N-PORT)" if basis == "fund_holdings" else "재무제표(XBRL)"
            decision = self._defer(
                [f"검증된 종목 식별·시세·공식 {official} 근거 중 일부를 확보하지 못함"]
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
            decision = self._defer(["결정 시점의 시세가 오래되어 가격 기반 판단을 검증할 수 없음"])
        report = ResearchReport(
            run_id=run_id,
            ticker=request.ticker,
            as_of=request.as_of or utcnow(),
            created_at=utcnow(),
            fixture=self.settings.mode == "fixture",
            security=security,
            research=research,
            reviews={
                role: parse_review(role, checkpoint[role]) for role in STAGES if role in checkpoint
            },
            decision=decision,
            validation=validation,
            rejected_decisions=rejected,
            evidence_ids=[r.evidence_id for r in tools.records],
            candidate_state=CandidateState.RESEARCH,
            previous_report_id=previous.run_id if previous else None,
            tool_calls=count + baseline_count,
            limitations=["Synthetic offline fixture"] if self.settings.mode == "fixture" else [],
            trade_proposal=(
                PortfolioDecision.model_validate(checkpoint["trade_proposal"])
                if "trade_proposal" in checkpoint
                else None
            ),
            trade_proposal_validation=(
                ValidationResult.model_validate(checkpoint["trade_proposal_validation"])
                if "trade_proposal_validation" in checkpoint
                else None
            ),
        )
        report.candidate_state = candidate_state(report, tools.records, previous)
        inherit_guard(report, previous)
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
            events = [report_alert(report, [])]
        self.store.commit_report(
            report,
            markdown(report, tools.records),
            events,
            next_research_at(self.settings, self.market, utcnow()),
        )
        return report

    def _collect_initial(self, ticker, tools, execute):
        execute("market_identity", {"ticker": ticker}, baseline=True)
        identity = next(
            (r for r in tools.records if r.evidence_type == "identity" and r.ticker == ticker),
            None,
        )
        etf = identity is not None and identity.payload.get("asset_type") == "etf"
        names = ["market_quote", "market_ohlcv", "portfolio_read", "fees_read"]
        names += ["fund_holdings_read"] if etf else ["filings_read", "financials_read"]
        for name in names:
            execute(name, {"ticker": ticker}, baseline=True)
        # Market cap is part of the market snapshot the research standard asks for.
        quote = next((r for r in tools.records if r.evidence_type == "quote"), None)
        shares = identity and any(f.name == "shares_outstanding" for f in identity.facts)
        if quote and shares:
            execute(
                "calculate",
                {
                    "operation": "multiply",
                    "evidence_id": quote.evidence_id,
                    "fact_name": "last_price",
                    "other_evidence_id": identity.evidence_id,
                    "other_fact_name": "shares_outstanding",
                    "assumption": None,
                    "result_name": "market_cap",
                },
                baseline=True,
            )

    def _replay(self, run_id, source_run_id, as_of, tools):
        """Copy snapshots available at `as_of` under new IDs, preserving derivation links."""
        records = [r for r in self.store.evidence(source_run_id) if r.retrieved_at <= as_of]
        mapping = {r.evidence_id: new_evidence_id(r.evidence_type) for r in records}
        for record in records:
            payload = dict(record.payload)
            for key in ("input_evidence_ids",):
                if key in payload:
                    payload[key] = [mapping.get(i, i) for i in payload[key]]
            if "source_evidence_id" in payload:
                payload["source_evidence_id"] = mapping.get(
                    payload["source_evidence_id"], payload["source_evidence_id"]
                )
            copy = record.model_copy(
                update={"evidence_id": mapping[record.evidence_id], "payload": payload}
            )
            self.store.add_evidence(run_id, copy)
            tools.records.append(copy)

    @staticmethod
    def _stage_digest(stage: dict) -> dict:
        """Earlier stages as later stages need them: claims as cited text, arguments, warnings.

        Quotes and numeric references were verified when the stage finished; the evidence index
        carries the facts. Gaps appear once, under open_gaps.
        """

        def claim(c):
            refs = ", ".join(c["evidence_ids"][:4])
            return f"[{c['classification']}] {c['text']}" + (f" (evidence: {refs})" if refs else "")

        digest = {
            key: stage[key]
            for key in ("arguments", "rebuttals", "early_warnings", "unresolved_questions")
            if stage.get(key)
        }
        if "sections" in stage:
            digest["sections"] = {
                section["name"]: {
                    "claims": [claim(c) for c in section["claims"]],
                    "limitations": section["limitations"],
                }
                for section in stage["sections"]
            }
        if stage.get("claims"):
            digest["claims"] = [claim(c) for c in stage["claims"]]
        if "evidence_sufficient" in stage:
            digest["evidence_sufficient"] = stage["evidence_sufficient"]
        return digest

    @staticmethod
    def _without_claims(output, failed: set[int]):
        """Drop claims (by flattened index) that failed verification; record them as a gap."""
        removed, index = [], 0

        def keep(claims):
            nonlocal index
            kept = []
            for claim in claims:
                (removed if index in failed else kept).append(claim)
                index += 1
            return kept

        data = output.model_dump()
        if isinstance(output, ResearchSummary):
            for section, section_data in zip(output.sections, data["sections"], strict=True):
                section_data["claims"] = [c.model_dump() for c in keep(section.claims)]
        else:
            data["claims"] = [c.model_dump() for c in keep(output.claims)]
        data["material_gaps"].append(
            {
                "description": f"검증에 실패해 제외한 주장 {len(removed)}건: "
                + "; ".join(claim.text[:80] for claim in removed[:3]),
                "severity": "non_blocking",
            }
        )
        return type(output).model_validate(data)

    @staticmethod
    def _binding_gaps(research: ResearchSummary, manager: StageReview) -> list[str]:
        """Only the research manager (here) and the PM (in the decision) make gaps binding."""
        binding = [gap.description for gap in manager.material_gaps if gap.severity == "blocking"]
        sufficient = (
            manager.evidence_sufficient
            if isinstance(manager, ResearchManagerReview)
            else research.evidence_sufficient
        )
        if not sufficient:
            binding.append("리서치 매니저가 근거 충분성을 확정하지 않음")
        return binding

    @staticmethod
    def _index_entry(record: EvidenceRecord, at: datetime) -> dict:
        entry = {
            "evidence_id": record.evidence_id,
            "ticker": record.ticker,
            "evidence_type": record.evidence_type,
            "source_name": record.source_name,
            "source_tier": record.source_tier,
            "source_url": record.source_url,
            "published_at": record.published_at,
            "effective_at": record.effective_at,
            "retrieved_at": record.retrieved_at,
            "usable_as_fact": record.usable_as_fact,
            "fresh": evidence_is_fresh(record, at),
            # name/value/unit is what a numeric reference must copy; periods and bases are in
            # the SEC fact names or available through evidence_read(part="facts").
            "facts": [
                {"name": fact.name, "value": str(fact.value), "unit": fact.unit}
                for fact in record.facts[:INDEX_FACTS]
            ],
            "facts_total": len(record.facts),
            "text_characters": len(record.text),
            "snippet": normalize_span(record.text)[:INDEX_SNIPPET],
            "summary": _compact(record.payload),
            "warnings": record.warnings,
        }
        if record.evidence_type == "ohlcv":
            entry["summary"]["recent_candles"] = record.payload.get("candles", [])[-5:]
        if record.evidence_type == "portfolio":
            entry["summary"]["holdings"] = [
                {
                    k: h.get(k)
                    for k in ("ticker", "quantity", "average_price", "market_value", "sector")
                }
                for h in record.payload.get("holdings", [])[:200]
            ]
        return entry

    def _tool_output(self, record: EvidenceRecord) -> dict:
        output = self._index_entry(record, utcnow())
        output["text_preview"] = record.text[:TOOL_TEXT_PREVIEW]
        output["text_truncated"] = len(record.text) > TOOL_TEXT_PREVIEW
        if record.evidence_type == "browser":
            # browser_action needs the current element refs and tabs.
            output["elements"] = record.payload.get("elements", [])
            output["tabs"] = record.payload.get("tabs", [])
            output["downloads"] = [
                {k: d.get(k) for k in ("url", "size", "content_hash", "warning")}
                for d in record.payload.get("downloads", [])
            ]
        return output

    @staticmethod
    def _prune(messages: list[dict]) -> None:
        """Keep recent tool outputs and the latest screenshot; older ones live in the index."""
        outputs = [m for m in messages if m.get("type") == "function_call_output"]
        for message in outputs[:-FULL_TOOL_OUTPUTS]:
            try:
                evidence_id = json.loads(message["output"]).get("evidence_id")
            except (ValueError, AttributeError):
                evidence_id = None
            message["output"] = json.dumps(
                {
                    "evidence_id": evidence_id,
                    "note": "Earlier tool output condensed; see the evidence index or "
                    "evidence_read.",
                }
            )
        images = [
            i
            for i, m in enumerate(messages)
            if isinstance(m.get("content"), list)
            and any(
                isinstance(part, dict) and part.get("type") == "input_image"
                for part in m["content"]
            )
        ]
        for index in reversed(images[:-1]):
            messages.pop(index)

    @staticmethod
    def _previous_summary(previous: ResearchReport) -> dict:
        decision, plan, guard = (
            previous.decision,
            previous.decision.trade_plan,
            guard_of(previous),
        )
        return {
            "run_id": previous.run_id,
            "as_of": previous.as_of,
            "rating": decision.rating.value,
            "confidence": decision.confidence,
            "new_entry_action": decision.new_entry_action.value,
            "holder_action": decision.holder_action.value,
            "thesis_state": decision.thesis_state,
            "thesis": [claim.text for claim in decision.thesis[:6]],
            "invalidation_conditions": decision.invalidation_conditions,
            "material_gaps": [gap.model_dump() for gap in decision.material_gaps],
            "candidate_state": previous.candidate_state.value,
            "validation_valid": previous.validation.valid,
            "trade_plan": None
            if plan is None
            else {
                "currency": plan.currency,
                "entry_low": plan.entry_low.value,
                "entry_high": plan.entry_high.value,
                "stop": plan.stop.value,
                "targets": [t.value for t in plan.targets],
                "conditions": plan.conditions,
                "sizing_unit": plan.sizing_unit,
            },
            # The stop the monitor watches now; it stays in force until a new guard validates.
            "position_guard": None
            if guard is None
            else {
                "currency": guard.currency,
                "stop": guard.stop.value,
                "stop_basis": guard.stop.basis,
                "take_profit": [level.value for level in guard.take_profit],
                "validated_in_report": previous.run_id
                if own_guard(previous)
                else previous.inherited_guard_from,
            },
        }

    @staticmethod
    def _defer(gaps):
        return PortfolioDecision(
            rating=Rating.DEFER,
            confidence="낮음",
            new_entry_action=NewEntryAction.DEFER,
            new_entry_note="핵심 근거 확인까지 판단 보류",
            holder_action=HolderAction.DEFER,
            holder_note="기존 보유 상태를 확인하고 부족한 근거를 재조사",
            executive_summary="핵심 증거 또는 거래안 검증이 부족해 판단을 보류한다.",
            thesis=[],
            thesis_state="UNKNOWN",
            invalidation_conditions=[],
            monitoring_checklist=["누락·충돌 근거 재확인"],
            material_changes=[],
            material_gaps=[
                MaterialGap(description=gap, severity="blocking") for gap in dict.fromkeys(gaps)
            ],
            trade_plan=None,
        )
