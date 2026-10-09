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

            # A price level away from the current quote is a conditional entry, not a wait.
            turn.result.new_entry_action = "CONDITIONAL_ENTRY"
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

    engine.market.quotes = lambda tickers: {t: reached(t) for t in tickers}
    evidence_before = [r.evidence_id for r in store.evidence(run_id)]
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
    # The completed report's evidence ledger is unchanged; monitor quotes are observations.
    assert [r.evidence_id for r in store.evidence(run_id)] == evidence_before
    observed = store.observations("TEST", "quote")
    assert len(observed) == 2 and observed[-1].facts[0].value == entry


def test_prune_removes_only_old_routine_observations(store):
    market, now = FixtureAdapters(), utcnow()

    def observe(kind, record, days_ago):
        stamp = now - timedelta(days=days_ago)
        record = record.model_copy(
            update={
                "evidence_id": f"{kind}-{days_ago}",
                "retrieved_at": stamp,
                "effective_at": record.effective_at and stamp,
                "published_at": record.published_at and stamp,
            }
        )
        store.add_observation("TEST", kind, record)

    observe("quote", market.quote("TEST"), 20)
    observe("quote", market.quote("TEST"), 1)
    observe("account", market.account_snapshot(), 20)
    observe("screening", market.quote("TEST"), 20)
    assert store.prune_observations(now - timedelta(days=14)) == 2
    assert [r.evidence_id for r in store.observations("TEST", "quote")] == ["quote-1"]
    assert store.observations("TEST", "account") == []
    assert len(store.observations("TEST", "screening")) == 1


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

    engine.market.quotes = lambda tickers: {t: quote(t) for t in tickers}
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


def test_unresolvable_claims_are_removed_and_audited(store, settings):
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
    traces = store.traces(run_id)
    # One correction, then the still-invalid claim is removed; the stage itself survives.
    assert sum(t["kind"] == "claim_rejection" for t in traces) == 2
    [removal] = [t for t in traces if t["kind"] == "claim_removal"]
    assert removal["role"] == "research_director" and removal["removed"][0]["text"] == "Claim"
    assert report.research.sections[0].claims == []
    gap = report.research.material_gaps[-1]
    assert gap.severity == "non_blocking" and "1건" in gap.description
    assert len(report.reviews) == 9 and report.validation.valid


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
    assert [g.description for g in turn.result.material_gaps] == ["missing"]
    assert turn.usage["total_tokens"] == 30
    arguments = client.responses.create.call_args.kwargs
    assert arguments["store"] is False and arguments["text"]["format"]["strict"]
    # Provider default reasoning unless configured per role.
    assert "reasoning" not in arguments
    tuned = settings.model_copy(
        update={"pm_reasoning_effort": "high", "research_reasoning_effort": "low"}
    )
    OpenAIModel(tuned, client).complete("portfolio_manager", [], [], ResearchSummary)
    assert client.responses.create.call_args.kwargs["reasoning"] == {"effort": "high"}
    OpenAIModel(tuned, client).complete("trade_proposal", [], [], ResearchSummary)
    assert client.responses.create.call_args.kwargs["reasoning"] == {"effort": "high"}
    assert client.responses.create.call_args.kwargs["model"] == tuned.pm_model
    OpenAIModel(tuned, client).complete("bull", [], [], ResearchSummary)
    assert client.responses.create.call_args.kwargs["reasoning"] == {"effort": "low"}


def test_openai_adapter_reports_incomplete_reason_and_invalid_output(settings):
    from app.adapters.http import ToolError
    from app.llm import InvalidModelOutput

    client = Mock()
    client.responses.create.return_value = Mock(
        status="incomplete", incomplete_details=Mock(reason="max_output_tokens")
    )
    model = OpenAIModel(settings, client)
    try:
        model.complete("bull", [], [], ResearchSummary)
        raise AssertionError("incomplete response accepted")
    except ToolError as error:
        assert error.code == "MODEL_INCOMPLETE:max_output_tokens"
    response = Mock(status="completed", output=[], output_text=json.dumps({"sections": 1}))
    response.usage.model_dump.return_value = {"input_tokens": 5, "output_tokens": 1}
    client.responses.create.return_value = response
    try:
        model.complete("bull", [], [], ResearchSummary)
        raise AssertionError("invalid output accepted")
    except InvalidModelOutput as error:
        assert error.code == "MODEL_OUTPUT_INVALID" and error.usage["input_tokens"] == 5
        json.dumps(error.errors)  # safe to hand back to the model
