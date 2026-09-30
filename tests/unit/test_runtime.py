import json
from datetime import timedelta
from unittest.mock import Mock

from sqlalchemy import func, select

from app.adapters.fixture import FixtureAdapters
from app.config import Settings
from app.engine import ResearchEngine
from app.llm import FixtureModel, ModelTurn, OpenAIModel
from app.models import Claim, PortfolioDecision, ResearchRequest, ResearchSummary, TradePlan, utcnow
from app.monitoring import Monitor
from app.notifications import DiscordNotifier
from app.storage import OutboxRow, WatchRow


class Planner(FixtureModel):
    def complete(self, role, messages, tools, schema):
        turn = super().complete(role, messages, tools, schema)
        if schema is PortfolioDecision:
            context = json.loads(messages[1]["content"])
            technical = next(r for r in context["evidence"] if r["evidence_type"] == "technical")

            def level(name):
                fact = next(f for f in technical["facts"] if f["name"] == name)
                return {
                    "value": fact["value"],
                    "evidence_ids": [technical["evidence_id"]],
                    "basis": "Observed fixture calculation",
                    "classification": "INTERPRETATION",
                }

            turn.result.trade_plan = TradePlan(
                currency="USD",
                entry_low=level("sma50"),
                entry_high=level("sma50"),
                stop=level("recent_low"),
                targets=[level("recent_high")],
                conditions=[],
                invalidation=[],
                horizon="medium",
                no_trade_conditions=[],
            )
        return turn


def test_outside_entry_range_keeps_watch_candidate_and_monitor_detects_hit(store, settings):
    engine = ResearchEngine(settings, store, model=Planner())
    run_id = store.enqueue(ResearchRequest(ticker="TEST"))
    report = engine.run(run_id)
    assert report.validation.valid and report.candidate_state.value == "WATCH_CANDIDATE"
    entry = report.decision.trade_plan.entry_high.value

    def reached(ticker):
        quote = FixtureAdapters().quote(ticker)
        quote.payload["price"] = str(entry)
        quote.facts[0].value = entry
        return quote

    engine.market.quote = reached
    with store.transaction() as session:
        session.get(WatchRow, "TEST").next_condition_at = utcnow() - timedelta(seconds=1)
    Monitor(engine).tick()
    with store.transaction() as session:
        alert = next(
            r for r in session.scalars(select(OutboxRow)) if r.body["kind"] == "ENTRY_CONDITION"
        )
        assert str(entry) in alert.body["content"] and "진입 범위" in alert.body["content"]
        count = session.scalar(select(func.count()).select_from(OutboxRow))
        session.get(WatchRow, "TEST").next_condition_at = utcnow() - timedelta(seconds=1)
    Monitor(engine).tick()
    with store.transaction() as session:
        assert session.scalar(select(func.count()).select_from(OutboxRow)) == count


def test_closed_session_retains_valid_plan_as_watch(store, settings):
    class ClosedMarket(FixtureAdapters):
        def quote(self, ticker):
            quote = super().quote(ticker)
            quote.payload["market_state"] = "CLOSED"
            return quote

    engine = ResearchEngine(settings, store, model=Planner(), market=ClosedMarket())
    report = engine.run(store.enqueue(ResearchRequest(ticker="TEST")))
    assert report.validation.valid and report.decision.trade_plan
    assert report.candidate_state.value == "WATCH_CANDIDATE"


def test_price_hit_requests_review_of_unverified_entry_conditions(store, settings):
    class ConditionalPlanner(Planner):
        def complete(self, *args):
            turn = super().complete(*args)
            if isinstance(turn.result, PortfolioDecision):
                turn.result.trade_plan.conditions = ["새 공시에서 규제 불확실성 해소 확인"]
            return turn

    engine = ResearchEngine(settings, store, model=ConditionalPlanner())
    report = engine.run(store.enqueue(ResearchRequest(ticker="TEST")))
    entry = report.decision.trade_plan.entry_low.value

    def quote(ticker):
        record = FixtureAdapters().quote(ticker)
        record.payload["price"] = str(entry)
        record.facts[0].value = entry
        return record

    engine.market.quote = quote
    with store.transaction() as session:
        session.get(WatchRow, "TEST").next_condition_at = utcnow() - timedelta(seconds=1)
    Monitor(engine).tick()
    with store.transaction() as session:
        alerts = [r.body for r in session.scalars(select(OutboxRow))]
        assert any(a["kind"] == "ENTRY_REVIEW_REQUIRED" for a in alerts)
        assert not any(a["kind"] == "ENTRY_CONDITION" for a in alerts)


def test_changed_words_with_unchanged_evidence_do_not_alert(store, settings):
    from app.lifecycle import changes

    engine = ResearchEngine(settings, store)
    first = engine.run(store.enqueue(ResearchRequest(ticker="TEST")))
    second = engine.run(store.enqueue(ResearchRequest(ticker="TEST")))
    second.decision.material_changes = [
        second.decision.thesis[0].model_copy(update={"text": "달라진 표현"})
    ]
    assert not changes(first, second, store.evidence(second.run_id), store.evidence(first.run_id))


def test_unresolvable_claims_are_deferred_and_audited(store, settings):
    class Unsupported(FixtureModel):
        def complete(self, role, messages, tools, schema):
            if schema is ResearchSummary:
                return ModelTurn(
                    result=ResearchSummary(
                        sections=[
                            {
                                "name": "fundamentals",
                                "claims": [
                                    Claim(
                                        classification="FACT",
                                        text="Claim",
                                        evidence_ids=["invented-id"],
                                        confidence="높음",
                                    )
                                ],
                                "limitations": [],
                            }
                        ],
                        unresolved_questions=[],
                        material_gaps=[],
                        evidence_sufficient=True,
                    )
                )
            return super().complete(role, messages, tools, schema)

    run_id = store.enqueue(ResearchRequest(ticker="TEST"))
    report = ResearchEngine(settings, store, model=Unsupported()).run(run_id)
    assert report.decision.rating.value == "판단 보류"
    assert sum(t["kind"] == "claim_rejection" for t in store.traces(run_id)) == 3


def test_discord_mentions_disabled_and_fixture_does_not_send(store):
    live = Settings(
        _env_file=None, mode="live", discord_webhook="https://discord.com/api/webhooks/fixture"
    )
    transport = Mock()
    with store.transaction() as session:
        session.add(OutboxRow(id="test-alert", body={"content": "@everyone rating changed"}))
    notifier = DiscordNotifier(live, store, transport)
    assert notifier.flush() == 1
    assert transport.request.call_args.kwargs["json"]["allowed_mentions"] == {"parse": []}
    assert notifier.flush() == 0
    transport.reset_mock()
    assert DiscordNotifier(Settings(_env_file=None), store, transport).flush() == 0
    assert not transport.request.called


def test_openai_adapter_uses_strict_contract_and_preserves_usage(settings):
    client = Mock()
    response = Mock(
        status="completed",
        output=[],
        output_text=json.dumps(
            {
                "sections": [],
                "unresolved_questions": [],
                "material_gaps": ["missing"],
                "evidence_sufficient": False,
            }
        ),
    )
    response.usage.model_dump.return_value = {
        "input_tokens": 20,
        "output_tokens": 10,
        "total_tokens": 30,
    }
    client.responses.create.return_value = response
    turn = OpenAIModel(settings, client).complete(
        "research", [{"role": "user", "content": "test"}], [], ResearchSummary
    )
    assert turn.result.material_gaps == ["missing"] and turn.usage["total_tokens"] == 30
    arguments = client.responses.create.call_args.kwargs
    assert arguments["store"] is False and arguments["text"]["format"]["strict"]
