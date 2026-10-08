import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.adapters.fixture import FixtureAdapters
from app.adapters.http import ToolError
from app.engine import ResearchEngine
from app.llm import FixtureModel
from app.models import PortfolioDecision, ResearchRequest, TradePlan, utcnow
from app.monitoring import Monitor
from app.storage import OutboxRow, RunRow, StateRow, WatchRow


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


class CountingMarket(FixtureAdapters):
    def __init__(self):
        self.quote_batches, self.single_quotes, self.snapshots = [], 0, 0
        self.holdings = []
        self.fail_account = False

    def quote(self, ticker):
        self.single_quotes += 1
        return super().quote(ticker)

    def quotes(self, tickers):
        self.quote_batches.append(list(tickers))
        return super().quotes(tickers)

    def account_snapshot(self):
        self.snapshots += 1
        if self.fail_account:
            raise ToolError("ACCOUNT_UNAVAILABLE")
        record = super().account_snapshot()
        record.payload["holdings"] = list(self.holdings)
        return record


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def due_now(store, *tickers):
    with store.transaction() as session:
        for ticker in tickers:
            session.get(WatchRow, ticker).next_condition_at = utcnow() - timedelta(seconds=1)


@pytest.fixture
def watched(store, settings):
    market = CountingMarket()
    engine = ResearchEngine(settings, store, model=Planner(), market=market)
    for ticker in ("AAA", "BBB", "CCC"):
        report = engine.run(store.enqueue(ResearchRequest(ticker=ticker)))
        assert report.validation.valid and report.decision.trade_plan
    market.quote_batches.clear()
    market.single_quotes = market.snapshots = 0
    return engine, market


def test_one_quote_batch_and_one_account_read_per_tick(store, watched):
    engine, market = watched
    clock = Clock()
    monitor = Monitor(engine, clock=clock)
    due_now(store, "AAA", "BBB", "CCC")
    monitor.tick()
    assert market.quote_batches == [["AAA", "BBB", "CCC"]] and market.single_quotes == 0
    assert market.snapshots == 1
    clock.now += 60
    due_now(store, "AAA", "BBB", "CCC")
    monitor.tick()
    assert len(market.quote_batches) == 2 and market.snapshots == 1  # shared snapshot reused
    clock.now += 300
    due_now(store, "AAA", "BBB", "CCC")
    monitor.tick()
    assert market.snapshots == 2
    assert len(store.observations("ACCOUNT", "account")) == 2


def test_position_change_alerts_only_the_changed_ticker(store, watched):
    engine, market = watched
    clock = Clock()
    monitor = Monitor(engine, clock=clock)
    due_now(store, "AAA", "BBB", "CCC")
    monitor.tick()
    market.holdings = [
        {"ticker": "BBB", "currency": "USD", "quantity": "3", "market_value": "300", "sector": None}
    ]
    clock.now += 301
    due_now(store, "AAA", "BBB", "CCC")
    monitor.tick()
    with store.transaction() as session:
        alerts = [r.body for r in session.scalars(select(OutboxRow))]
    changed = [a for a in alerts if a["kind"] == "POSITION_CHANGED"]
    assert [a["ticker"] for a in changed] == ["BBB"] and "0 → 3" in changed[0]["content"]


def test_sold_position_is_alerted_once_and_no_longer_watched(store, watched):
    engine, market = watched
    clock = Clock()
    monitor = Monitor(engine, clock=clock)
    market.holdings = [
        {"ticker": "BBB", "currency": "USD", "quantity": "3", "market_value": "300", "sector": None}
    ]
    due_now(store, "AAA", "BBB", "CCC")
    monitor.tick()
    market.holdings = []
    clock.now += 301
    due_now(store, "AAA", "BBB", "CCC")
    monitor.tick()
    with store.transaction() as session:
        assert session.get(WatchRow, "BBB") is None
        sold = [
            r.body
            for r in session.scalars(select(OutboxRow))
            if r.body["kind"] == "POSITION_CHANGED" and "3 → 0" in r.body["content"]
        ]
    assert [a["ticker"] for a in sold] == ["BBB"]


def test_account_failure_alerts_only_held_positions(store, watched):
    engine, market = watched
    market.fail_account = True
    with store.transaction() as session:
        session.get(StateRow, "AAA").state = "ACTIVE_POSITION"
    # candidate_state lives in the report; mark the latest AAA report as a held position.
    report = store.previous("AAA")
    from app.storage import ReportRow

    with store.transaction() as session:
        row = session.get(ReportRow, report.run_id)
        row.body = {**row.body, "candidate_state": "ACTIVE_POSITION"}
    due_now(store, "AAA", "BBB")
    Monitor(engine, clock=Clock()).tick()
    with store.transaction() as session:
        failures = [
            r.body["ticker"]
            for r in session.scalars(select(OutboxRow))
            if r.body["kind"] == "DATA_QUALITY_FAILURE"
        ]
    assert failures == ["AAA"]


def test_follow_up_research_inherits_investor_conditions(store, settings):
    request = ResearchRequest(
        ticker="TEST",
        mode="normal",
        investor_status="기존 보유",
        horizon="6~12개월",
        question="마진 회복이 가격에 반영됐는가",
        risk={
            "portfolio_value": 50000,
            "currency": "USD",
            "max_loss_fraction": ".005",
            "max_position_fraction": ".1",
            "slippage_fraction": ".001",
            "tax_fraction": 0,
        },
    )
    engine = ResearchEngine(settings, store, model=Planner())
    first = engine.run(store.enqueue(request))
    with store.transaction() as session:
        row = session.get(WatchRow, "TEST")
        row.next_research_at = utcnow() - timedelta(seconds=1)
        row.next_condition_at = utcnow() - timedelta(seconds=1)
    Monitor(engine, clock=Clock()).tick()
    with store.transaction() as session:
        follow = session.scalar(
            select(RunRow).where(RunRow.ticker == "TEST", RunRow.status == "PENDING")
        )
        follow_id, inherited = follow.id, ResearchRequest.model_validate(follow.request)
    for field in ("mode", "horizon", "question", "risk", "report_policy"):
        assert getattr(inherited, field) == getattr(request, field), field
    # The account shows no TEST position, so the actual status overrides the requested one.
    assert inherited.investor_status == "신규 진입 검토"
    assert inherited.trigger and inherited.as_of is None
    # A monitor-triggered run does not replace the stored investor conditions.
    engine.run(follow_id)
    with store.transaction() as session:
        assert session.get(WatchRow, "TEST").body["base_request"]["question"] == request.question
    assert first.run_id != follow_id


def test_invalidation_escalates_mode_and_describes_the_change(store, watched):
    engine, market = watched
    report = store.previous("AAA")
    stop = report.decision.trade_plan.stop.value

    def crashed(tickers):
        quotes = FixtureAdapters().quotes(tickers)
        for record in quotes.values():
            record.payload["price"] = str(stop - 1)
        return quotes

    market.quotes = crashed
    due_now(store, "AAA")
    Monitor(engine, clock=Clock()).tick()
    with store.transaction() as session:
        follow = session.scalar(
            select(RunRow).where(RunRow.ticker == "AAA", RunRow.status == "PENDING")
        )
        request = ResearchRequest.model_validate(follow.request)
    assert request.mode == "critical" and "INVALIDATION_PRICE" in request.trigger


def test_completed_research_evidence_is_immutable(store, settings):
    report = ResearchEngine(settings, store).run(store.enqueue(ResearchRequest(ticker="TEST")))
    with pytest.raises(ValueError, match="immutable"):
        store.add_evidence(report.run_id, FixtureAdapters().quote("TEST"))
