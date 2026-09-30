import json
from datetime import timedelta

from app.adapters.fixture import FixtureAdapters
from app.engine import FULL_TOOL_OUTPUTS, ResearchEngine
from app.llm import FixtureModel, FunctionCall, ModelTurn
from app.models import (
    MaterialGap,
    ResearchManagerReview,
    ResearchRequest,
    ResearchSummary,
    StageReview,
    utcnow,
)
from app.tools import EvidenceReadInput, ToolRegistry


class LargeDocuments(FixtureAdapters):
    def read(self, ticker, url):
        record = super().read(ticker, url)
        record.text = "Long filing paragraph with operating detail. " * 5000
        record.payload["pages"] = [{"page": i, "text": "page text " * 500} for i in range(200)]
        return record


class Recording(FixtureModel):
    def __init__(self, scenario="balanced"):
        super().__init__(scenario)
        self.contexts, self.outputs = [], []

    def complete(self, role, messages, tools, schema):
        self.contexts.append(messages[1]["content"])
        self.outputs += [m["output"] for m in messages if m.get("type") == "function_call_output"]
        return super().complete(role, messages, tools, schema)


def test_context_is_an_index_not_the_documents(store, settings):
    model = Recording()
    engine = ResearchEngine(settings, store, model=model, reader=LargeDocuments())
    report = engine.run(store.enqueue(ResearchRequest(ticker="TEST")))
    assert report.validation.valid
    largest = max(len(c) for c in model.contexts)
    assert largest < 40_000, largest
    assert all(len(o) < 12_000 for o in model.outputs)
    document = next(
        e for e in json.loads(model.contexts[-1])["evidence"] if e["evidence_type"] == "document"
    )
    assert "pages" not in document["summary"] and len(document["snippet"]) <= 300
    assert document["text_characters"] > 200_000 - 1
    ohlcv = next(
        e for e in json.loads(model.contexts[-1])["evidence"] if e["evidence_type"] == "ohlcv"
    )
    assert "candles" not in ohlcv["summary"] and len(ohlcv["summary"]["recent_candles"]) == 5


def test_evidence_read_is_a_view_and_creates_no_evidence():
    tools = ToolRegistry("TEST")
    document = FixtureAdapters().read("TEST", "https://example.com")
    document.text = "alpha " * 3000 + "needle sentence here. " + "omega " * 3000
    tools.records.append(document)
    tools.add("evidence_read", EvidenceReadInput, tools.read_evidence, "")
    view = tools.execute("evidence_read", {"evidence_id": document.evidence_id, "query": "needle"})
    assert isinstance(view, dict) and "needle sentence" in view["text"]
    assert view["next_start"] is not None and len(tools.records) == 1
    financials = FixtureAdapters().financials("TEST")
    financials.facts = financials.facts * 120
    tools.records.append(financials)
    page = tools.execute(
        "evidence_read", {"evidence_id": financials.evidence_id, "part": "facts", "start": 0}
    )
    assert len(page["facts"]) == 50 and page["next_start"] == 50 and page["total_facts"] == 120


def test_prune_condenses_old_outputs_and_keeps_latest_screenshot():
    messages = [{"role": "system", "content": "p"}, {"role": "user", "content": "c"}]
    for i in range(10):
        messages.append(
            {
                "type": "function_call_output",
                "call_id": str(i),
                "output": json.dumps({"evidence_id": f"ev-{i}", "text_preview": "x" * 4000}),
            }
        )
        messages.append({"role": "user", "content": [{"type": "input_image", "image_url": i}]})
    ResearchEngine._prune(messages)
    outputs = [m for m in messages if m.get("type") == "function_call_output"]
    assert len(outputs) == 10  # every call keeps its paired output
    condensed = outputs[: 10 - FULL_TOOL_OUTPUTS]
    assert all("text_preview" not in m["output"] for m in condensed)
    assert json.loads(condensed[0]["output"])["evidence_id"] == "ev-0"
    assert all("text_preview" in m["output"] for m in outputs[-FULL_TOOL_OUTPUTS:])
    images = [m for m in messages if isinstance(m.get("content"), list)]
    assert len(images) == 1 and images[0]["content"][0]["image_url"] == 9


class Gaps(FixtureModel):
    """Director unsure, bull proposes a blocking gap; the research manager decides."""

    def __init__(self, manager_sufficient=True, manager_blocking=False):
        super().__init__()
        self.manager_sufficient, self.manager_blocking = manager_sufficient, manager_blocking

    def complete(self, role, messages, tools, schema):
        turn = super().complete(role, messages, tools, schema)
        if isinstance(turn.result, ResearchSummary):
            turn.result.evidence_sufficient = False
            turn.result.material_gaps = [
                MaterialGap(description="경쟁사 가격 자료 없음", severity="blocking")
            ]
        elif role == "bull" and isinstance(turn.result, StageReview):
            turn.result.material_gaps = [
                MaterialGap(description="신제품 수율 미확인", severity="blocking")
            ]
        elif isinstance(turn.result, ResearchManagerReview):
            turn.result.evidence_sufficient = self.manager_sufficient
            if self.manager_blocking:
                turn.result.material_gaps = [
                    MaterialGap(description="공시와 IR 수치 충돌", severity="blocking")
                ]
        return turn


def test_stage_gaps_are_advisory_until_the_research_manager_binds_them(store, settings):
    report = ResearchEngine(settings, store, model=Gaps()).run(
        store.enqueue(ResearchRequest(ticker="TEST"))
    )
    assert report.decision.rating.value == "Hold" and report.validation.valid
    assert report.reviews["bull"].material_gaps[0].severity == "blocking"


def test_research_manager_blocking_gap_forces_deferral(store, settings):
    for model in (Gaps(manager_blocking=True), Gaps(manager_sufficient=False)):
        report = ResearchEngine(settings, store, model=model).run(
            store.enqueue(ResearchRequest(ticker="TEST"))
        )
        assert report.decision.rating.value == "판단 보류"
        assert report.rejected_decisions
        assert any(
            gap.description.startswith("Blocking evidence gaps require")
            for gap in report.decision.material_gaps
        )


def test_pm_sees_open_gaps_from_every_stage(store, settings):
    seen = []

    class Watching(Gaps):
        def complete(self, role, messages, tools, schema):
            if role == "portfolio_manager":
                seen.append(json.loads(messages[1]["content"])["open_gaps"])
            return super().complete(role, messages, tools, schema)

    ResearchEngine(settings, store, model=Watching()).run(
        store.enqueue(ResearchRequest(ticker="TEST"))
    )
    stages = {gap["stage"] for gap in seen[0]}
    assert {"research_director", "bull"} <= stages


def test_etf_uses_fund_holdings_instead_of_issuer_financials(store, settings):
    run_id = store.enqueue(ResearchRequest(ticker="FUND"))
    report = ResearchEngine(settings, store).run(run_id)
    names = [t["name"] for t in store.traces(run_id) if t.get("kind") == "tool"]
    assert "fund_holdings_read" in names and "financials_read" not in names
    assert report.security.asset_type == "etf"
    assert report.decision.rating.value == "Hold" and report.validation.valid


def test_historical_replay_keeps_derivation_links(store, settings):
    ResearchEngine(settings, store).run(store.enqueue(ResearchRequest(ticker="TEST")))
    run_id = store.enqueue(ResearchRequest(ticker="TEST", as_of=utcnow() + timedelta(seconds=1)))
    ResearchEngine(settings, store).run(run_id)
    replayed = {r.evidence_id: r for r in store.evidence(run_id)}
    technical = next(r for r in replayed.values() if r.evidence_type == "technical")
    assert all(i in replayed for i in technical.payload["input_evidence_ids"])


def test_follow_up_quotes_are_persisted_as_quotation_evidence(store, settings):
    class Quoting(FixtureModel):
        def complete(self, role, messages, tools, schema):
            turn = super().complete(role, messages, tools, schema)
            if role == "bull":
                context = json.loads(messages[1]["content"])
                document = next(e for e in context["evidence"] if e["evidence_type"] == "document")
                turn.result.claims = [
                    {
                        "classification": "FACT",
                        "text": "합성 발표문이 검증 목적임을 밝혔다",
                        "evidence_ids": [document["evidence_id"]],
                        "quotes": [
                            {
                                "evidence_id": document["evidence_id"],
                                "text": "evidence-backed testing only",
                            }
                        ],
                        "confidence": "높음",
                    }
                ]
                turn.result = StageReview.model_validate(turn.result.model_dump())
            return turn

    run_id = store.enqueue(ResearchRequest(ticker="TEST"))
    report = ResearchEngine(settings, store, model=Quoting()).run(run_id)
    assert report.validation.valid
    quotations = [r for r in store.evidence(run_id) if r.evidence_type == "quotation"]
    assert [q.text for q in quotations] == ["evidence-backed testing only"]


def test_evidence_read_view_is_traced_without_new_evidence(store, settings):
    class Reads(FixtureModel):
        calls = 0

        def complete(self, role, messages, tools, schema):
            if role == "research_director" and not Reads.calls:
                Reads.calls += 1
                context = json.loads(messages[1]["content"])
                identity = next(e for e in context["evidence"] if e["evidence_type"] == "identity")
                return ModelTurn(
                    calls=[
                        FunctionCall(
                            "view",
                            "evidence_read",
                            {
                                "evidence_id": identity["evidence_id"],
                                "part": "payload",
                                "query": None,
                                "start": 0,
                                "characters": 500,
                            },
                        )
                    ]
                )
            return super().complete(role, messages, tools, schema)

    run_id = store.enqueue(ResearchRequest(ticker="TEST"))
    ResearchEngine(settings, store, model=Reads()).run(run_id)
    traces = [t for t in store.traces(run_id) if t.get("name") == "evidence_read"]
    assert traces and traces[0]["status"] == "OK" and traces[0]["evidence_ids"] == []
    assert traces[0]["view_of"] and traces[0]["view_of"] in {
        r.evidence_id for r in store.evidence(run_id)
    }
