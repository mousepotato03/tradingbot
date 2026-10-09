import json
from datetime import timedelta
from decimal import Decimal

import pytest

from app.adapters.fixture import FixtureAdapters
from app.adapters.http import ToolError
from app.engine import ResearchEngine
from app.evidence import validate_claims
from app.llm import FixtureModel, request_context
from app.models import Claim, PortfolioDecision, ResearchRequest, utcnow
from app.tools import ToolRegistry, add_technical_record
from app.validation import validate_decision


def test_peer_derivations_keep_the_security_their_values_describe():
    fixture, tools = FixtureAdapters(), ToolRegistry("TEST")
    bars, quote = fixture.ohlcv("PEER"), fixture.quote("PEER")
    document = fixture.read("PEER", "https://fixture.example/peer")
    document.text = "Revenue was $123 million."
    tools.records = [bars, quote, document]
    calculated = tools.calculate(
        operation="multiply",
        evidence_id=quote.evidence_id,
        fact_name="last_price",
        assumption="1.2",
        result_name="scenario_target",
    )
    extracted = tools.extract_fact(
        evidence_id=document.evidence_id,
        fact_name="revenue",
        quoted_text=document.text,
        value=123,
        unit="USD",
        scale=1000000,
    )
    assert {add_technical_record(tools, bars).ticker, calculated.ticker, extracted.ticker} == {
        "PEER"
    }


@pytest.mark.parametrize("mislabelled", [False, True])
def test_peer_history_cannot_supply_a_primary_holding_stop(mislabelled):
    fixture, tools = FixtureAdapters(), ToolRegistry("TEST")
    bars = fixture.ohlcv("PEER")
    technical = add_technical_record(tools, bars)
    if mislabelled:
        # Old records and a subsequent calculation must not launder the wrong identity.
        technical = technical.model_copy(update={"ticker": "TEST"})
    tools.records = [bars, technical]
    calculated = tools.calculate(
        operation="multiply",
        evidence_id=technical.evidence_id,
        fact_name="recent_low",
        assumption=1,
        result_name="holding_stop",
    )
    portfolio = fixture.portfolio("TEST")
    portfolio.payload["holdings"] = [{"ticker": "TEST", "quantity": "1", "market_value": "100"}]
    decision = PortfolioDecision(
        rating="Hold",
        confidence="중간",
        new_entry_action="WAIT",
        holder_action="HOLD",
        executive_summary="합성 자료로 종목 구분을 검증한다.",
        thesis=[
            Claim(
                classification="INTERPRETATION",
                text="관측 자료를 검토했다.",
                evidence_ids=[bars.evidence_id],
                confidence="중간",
            )
        ],
        thesis_state="ACTIVE",
        invalidation_conditions=[],
        monitoring_checklist=[],
        material_changes=[],
        material_gaps=[],
        trade_plan=None,
        position_guard={
            "currency": "USD",
            "stop": {
                "value": calculated.facts[0].value,
                "evidence_ids": [calculated.evidence_id],
                "basis": "다른 종목에서 파생된 수준",
                "classification": "INTERPRETATION",
            },
            "take_profit": [],
            "rationale": "합성 손절선",
        },
    )
    result = validate_decision(
        decision,
        [bars, technical, calculated, fixture.quote("TEST"), portfolio],
        ResearchRequest(ticker="TEST"),
        utcnow(),
    )
    assert not result.valid
    assert {i.code for i in result.issues} & {"UNSUPPORTED_LEVEL", "LEVEL_IDENTITY"}


@pytest.mark.parametrize(
    "body,quote,unit,error",
    [
        (
            "Revenue was $123. Later an unrelated table is in millions and contains other figures.",
            "Revenue was $123",
            "USD",
            "EXTRACTION_SCALE_UNSUPPORTED",
        ),
        (
            "Revenue was 123 million. Later figures use USD but apply to a different table.",
            "Revenue was 123 million",
            "USD",
            "EXTRACTION_CURRENCY_UNSUPPORTED",
        ),
    ],
)
def test_extraction_does_not_borrow_units_from_after_the_quote(body, quote, unit, error):
    tools = ToolRegistry("TEST")
    document = FixtureAdapters().read("TEST", "https://fixture.example/units")
    document.text = body
    tools.records = [document]
    with pytest.raises(ToolError, match=error):
        tools.extract_fact(
            evidence_id=document.evidence_id,
            fact_name="revenue",
            quoted_text=quote,
            value=123,
            unit=unit,
            scale=1000000,
        )


@pytest.mark.parametrize(
    "text,value,accepted",
    [
        ("현재가는 100달러다.", "100", True),
        ("현재가는 100달러이며 매출은 999달러다.", "100", False),
        ("현재가는 999달러다.", "100", False),
        ("현재가는 -100달러다.", "100", False),
        ("현재가는 −100달러다.", "100", False),
        ("현재가는 1e2달러다.", "100", True),
    ],
)
def test_each_prose_number_needs_a_matching_reference(text, value, accepted):
    quote = FixtureAdapters().quote("TEST")
    claim = Claim(
        classification="FACT",
        text=text,
        evidence_ids=[quote.evidence_id],
        numeric_references=[
            {
                "value": value,
                "unit": "USD/share",
                "evidence_id": quote.evidence_id,
                "fact_name": "last_price",
            }
        ],
        confidence="높음",
    )
    issues = validate_claims([claim], [quote], utcnow())
    assert (not issues) == accepted
    if not accepted:
        assert "UNSTRUCTURED_NUMBER" in {i.code for i in issues}


class RecordingModel(FixtureModel):
    def __init__(self):
        super().__init__()
        self.contexts = []

    def complete(self, role, messages, tools, schema):
        context = request_context(messages)
        self.contexts.append((role, context))
        return super().complete(role, messages, tools, schema)


def test_historical_context_omits_a_future_report(store, settings):
    first = ResearchEngine(settings, store).run(store.enqueue(ResearchRequest(ticker="TEST")))
    model = RecordingModel()
    run_id = store.enqueue(ResearchRequest(ticker="TEST", as_of=utcnow() - timedelta(days=365)))
    report = ResearchEngine(settings, store, model=model).run(run_id)
    assert all(context["previous_report"] is None for _, context in model.contexts)
    assert report.previous_report_id is None
    assert not store.evidence(run_id)
    assert store.previous("TEST").run_id == first.run_id


def test_replay_selects_the_report_available_at_the_cutoff(store, settings):
    engine = ResearchEngine(settings, store)
    first = engine.run(store.enqueue(ResearchRequest(ticker="TEST")))
    cutoff = utcnow()
    latest = engine.run(store.enqueue(ResearchRequest(ticker="TEST")))
    model = RecordingModel()
    run_id = store.enqueue(ResearchRequest(ticker="TEST", as_of=cutoff))
    report = ResearchEngine(settings, store, model=model).run(run_id)
    assert report.previous_report_id == first.run_id
    assert report.decision.rating.value == "Hold"
    assert store.evidence(run_id)
    assert all(r.retrieved_at <= cutoff for r in store.evidence(run_id))
    assert {c["previous_report"]["run_id"] for _, c in model.contexts} == {first.run_id}
    assert store.previous("TEST").run_id == latest.run_id


class PlannedModel(RecordingModel):
    def __init__(self, revise=False, keep_changing=False):
        super().__init__()
        self.revise, self.keep_changing = revise, keep_changing
        self.pm_calls = 0

    def complete(self, role, messages, tools, schema):
        turn = super().complete(role, messages, tools, schema)
        if not isinstance(turn.result, PortfolioDecision):
            return turn
        context = request_context(messages)
        quote = next(r for r in context["evidence"] if r["evidence_type"] == "quote")
        technical = next(r for r in context["evidence"] if r["evidence_type"] == "technical")
        facts = {f["name"]: Decimal(f["value"]) for f in technical["facts"]}
        if role == "portfolio_manager":
            self.pm_calls += 1
        revised = self.revise and self.pm_calls > 0
        if self.keep_changing:
            revised = bool(self.pm_calls % 2)

        def level(value, evidence):
            return {
                "value": value,
                "evidence_ids": [evidence["evidence_id"]],
                "basis": "합성 관측 수준",
                "classification": "INTERPRETATION",
            }

        data = turn.result.model_dump()
        data.update(
            rating="Buy",
            new_entry_action="ENTER_NOW",
            holder_action="ADD",
            trade_plan={
                "currency": "USD",
                "entry_low": level(100, quote),
                "entry_high": level(100, quote),
                "stop": level(facts["low_52w" if revised else "recent_low"], technical),
                "targets": [level(facts["recent_high"], technical)],
                "conditions": [],
                "invalidation": [],
                "horizon": "합성 중기 계획",
                "quantity": None,
                "no_trade_conditions": [],
            },
        )
        turn.result = PortfolioDecision.model_validate(data)
        return turn


def test_risk_committee_receives_the_current_trade_proposal(store, settings):
    model = PlannedModel()
    report = ResearchEngine(settings, store, model=model).run(
        store.enqueue(ResearchRequest(ticker="TEST"))
    )
    risks = [c for role, c in model.contexts if role.endswith("_risk") or role == "premortem"]
    assert len(risks) == 4
    assert all(c.get("trade_proposal", {}).get("trade_plan") for c in risks)
    assert all(c["trade_proposal_validation"]["valid"] for c in risks)
    assert report.trade_proposal.trade_plan.stop.value == report.decision.trade_plan.stop.value


def test_changed_final_plan_is_reviewed_again_before_publication(store, settings):
    model = PlannedModel(revise=True)
    report = ResearchEngine(settings, store, model=model).run(
        store.enqueue(ResearchRequest(ticker="TEST"))
    )
    aggressive = [c for role, c in model.contexts if role == "aggressive_risk"]
    assert len(aggressive) == 2
    assert (
        aggressive[0]["trade_proposal"]["trade_plan"]["stop"]["value"]
        != aggressive[1]["trade_proposal"]["trade_plan"]["stop"]["value"]
    )
    assert report.validation.valid and model.pm_calls == 2
    assert report.trade_proposal.trade_plan.stop.value == report.decision.trade_plan.stop.value


def test_endlessly_changed_plans_defer_instead_of_publishing_an_unreviewed_plan(store, settings):
    model = PlannedModel(keep_changing=True)
    report = ResearchEngine(settings, store, model=model).run(
        store.enqueue(ResearchRequest(ticker="TEST"))
    )
    assert report.decision.rating.value == "판단 보류"
    assert report.decision.trade_plan is None
    assert "UNREVIEWED_PLAN" in {i.code for i in report.validation.issues}


def test_interrupted_risk_review_resumes_with_the_same_proposal(store, settings):
    class SyntheticWorkerCrash(BaseException):
        pass

    class InterruptedModel(PlannedModel):
        interrupted = False

        def complete(self, role, messages, tools, schema):
            if role == "neutral_risk" and not self.interrupted:
                self.interrupted = True
                raise SyntheticWorkerCrash("synthetic worker interruption")
            return super().complete(role, messages, tools, schema)

    model = InterruptedModel()
    engine = ResearchEngine(settings, store, model=model)
    run_id = store.enqueue(ResearchRequest(ticker="TEST"))
    with pytest.raises(SyntheticWorkerCrash, match="synthetic worker interruption"):
        engine.run(run_id)
    store.recover_single_worker()
    report = engine.run(run_id)
    roles = [role for role, _ in model.contexts]
    assert roles.count("trade_proposal") == roles.count("aggressive_risk") == 1
    assert all(
        roles.count(role) == 1 for role in ("neutral_risk", "conservative_risk", "premortem")
    )
    risks = [c for role, c in model.contexts if role.endswith("_risk") or role == "premortem"]
    assert len({json.dumps(c["trade_proposal"], sort_keys=True) for c in risks}) == 1
    assert report.validation.valid and report.decision.trade_plan


def test_legacy_checkpoint_risk_reviews_are_replaced_without_a_proposal(store, settings):
    model = PlannedModel()
    run_id = store.enqueue(ResearchRequest(ticker="TEST"))
    store.checkpoint(
        run_id,
        {
            "aggressive_risk": {
                "claims": [],
                "arguments": ["Review of a previous unrelated plan"],
                "rebuttals": [],
                "early_warnings": [],
                "material_gaps": [],
            }
        },
        status="PENDING",
    )
    report = ResearchEngine(settings, store, model=model).run(run_id)
    assert sum(role == "aggressive_risk" for role, _ in model.contexts) == 1
    assert "Review of a previous unrelated plan" not in report.reviews["aggressive_risk"].arguments
    assert report.validation.valid and report.trade_proposal.trade_plan
