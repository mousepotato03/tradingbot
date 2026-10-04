import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.adapters.fixture import FixtureAdapters
from app.engine import ResearchEngine
from app.lifecycle import candidate_state, guard_of
from app.llm import FixtureModel
from app.models import CandidateState as State
from app.models import (
    EvidenceRecord,
    PortfolioDecision,
    PositionGuard,
    ResearchRequest,
    utcnow,
)
from app.monitoring import Monitor
from app.schedule import first_research_at, next_research_at
from app.storage import OutboxRow, ReportRow, RunRow, WatchRow
from app.validation import validate_decision

NEW_YORK = ZoneInfo("America/New_York")


def holding(quantity="5"):
    return {
        "ticker": "TEST",
        "currency": "USD",
        "quantity": quantity,
        "market_value": "500",
        "average_price": "90",
        "sector": None,
    }


class Held(FixtureAdapters):
    """The synthetic account holds TEST; quotes can be moved and the session closed."""

    def __init__(self):
        self.price, self.state, self.age = "100", "REGULAR", timedelta(0)
        self.quantity = "5"

    def portfolio(self, ticker):
        record = super().portfolio(ticker)
        record.payload["holdings"] = [holding(self.quantity)] if self.quantity else []
        return record

    def account_snapshot(self):
        return self.portfolio("ACCOUNT")

    def quotes(self, tickers):
        quotes = super().quotes(tickers)
        for record in quotes.values():
            record.payload.update(price=self.price, market_state=self.state)
            record.facts[0].value = self.price
            record.effective_at = record.retrieved_at - self.age
        return quotes


class Guarding(FixtureModel):
    """PM keeps the holding with a stop at the technical recent low."""

    def complete(self, role, messages, tools, schema):
        turn = super().complete(role, messages, tools, schema)
        if schema is PortfolioDecision:
            technical = next(
                r
                for r in json.loads(messages[1]["content"])["evidence"]
                if r["evidence_type"] == "technical"
            )
            low = next(f for f in technical["facts"] if f["name"] == "recent_low")
            turn.result.position_guard = PositionGuard(
                currency="USD",
                stop={
                    "value": low["value"],
                    "evidence_ids": [technical["evidence_id"]],
                    "basis": "최근 20거래일 저점",
                    "classification": "INTERPRETATION",
                },
                take_profit=[],
                rationale="최근 저점 이탈 시 추세 훼손으로 본다",
            )
        return turn


def held_report(store, settings):
    market = Held()
    engine = ResearchEngine(settings, store, model=Guarding(), market=market)
    report = engine.run(store.enqueue(ResearchRequest(ticker="TEST")))
    assert report.validation.valid, report.validation.issues
    return engine, market, report


def test_a_held_position_gets_a_validated_stop_the_monitor_watches(store, settings):
    engine, market, report = held_report(store, settings)
    stop = report.decision.position_guard.stop.value
    assert report.candidate_state.value == "ACTIVE_POSITION"
    market.price = str(stop - 1)
    with store.transaction() as session:
        session.get(WatchRow, "TEST").next_condition_at = utcnow() - timedelta(seconds=1)
    Monitor(engine).tick()
    with store.transaction() as session:
        alerts = [r.body for r in session.scalars(select(OutboxRow))]
        follow = session.scalar(select(RunRow).where(RunRow.status == "PENDING"))
        mode = ResearchRequest.model_validate(follow.request).mode
    stop_alerts = [a for a in alerts if a["kind"] == "HOLDER_STOP"]
    assert len(stop_alerts) == 1 and "보유 손절선" in stop_alerts[0]["content"]
    assert stop_alerts[0]["content"].startswith("TEST · 보유 손절선 도달\n")
    assert "직전 리서치(" in stop_alerts[0]["content"]
    assert report.run_id not in stop_alerts[0]["content"]
    assert mode == "critical"


def test_closed_market_prices_neither_trigger_nor_fail_and_breaches_are_not_resent(store, settings):
    engine, market, report = held_report(store, settings)
    stop = report.decision.position_guard.stop.value
    monitor = Monitor(engine)

    def tick():
        with store.transaction() as session:
            session.get(WatchRow, "TEST").next_condition_at = utcnow() - timedelta(seconds=1)
        monitor.tick()
        with store.transaction() as session:
            return [r.body["kind"] for r in session.scalars(select(OutboxRow))]

    market.price, market.state, market.age = str(stop - 1), "CLOSED", timedelta(days=2)
    kinds = tick()
    assert "HOLDER_STOP" not in kinds and "DATA_QUALITY_FAILURE" not in kinds
    market.state, market.age = "REGULAR", timedelta(0)
    assert tick().count("HOLDER_STOP") == 1  # judged at the regular session
    market.state = "CLOSED"
    tick()
    market.state = "REGULAR"
    assert tick().count("HOLDER_STOP") == 1  # still breached next session: not sent again


def outbox(store):
    with store.transaction() as session:
        return [r.body for r in session.scalars(select(OutboxRow).order_by(OutboxRow.created_at))]


def test_a_deferred_holding_keeps_its_validated_stop(store, settings):
    engine, market, first = held_report(store, settings)
    stop = first.decision.position_guard.stop.value
    deferring = ResearchEngine(settings, store, model=FixtureModel("missing"), market=market)
    second = deferring.run(store.enqueue(ResearchRequest(ticker="TEST")))
    assert second.decision.rating.value == "판단 보류" and second.decision.position_guard is None
    assert second.candidate_state == State.ACTIVE
    assert guard_of(second).stop.value == stop and second.inherited_guard_from == first.run_id
    third = deferring.run(store.enqueue(ResearchRequest(ticker="TEST")))
    assert third.inherited_guard_from == first.run_id  # chained, not re-attributed
    contents = [body["content"] for body in outbox(store)]
    assert not any("감시 해제" in content for content in contents)
    assert any("이전 보고서 기준 유지" in content for content in contents)
    with store.transaction() as session:
        assert "기준을 유지합니다" in session.get(ReportRow, third.run_id).markdown
    market.price = str(stop - 1)
    with store.transaction() as session:
        session.get(WatchRow, "TEST").next_condition_at = utcnow() - timedelta(seconds=1)
    Monitor(deferring).tick()
    assert [b["kind"] for b in outbox(store)].count("HOLDER_STOP") == 1


def test_a_sold_position_does_not_inherit_a_stop(store, settings):
    engine, market, first = held_report(store, settings)
    market.quantity = ""
    deferring = ResearchEngine(settings, store, model=FixtureModel("missing"), market=market)
    second = deferring.run(store.enqueue(ResearchRequest(ticker="TEST")))
    assert second.candidate_state == State.EXITED and guard_of(second) is None
    assert any("보유 손절선 감시 해제" in body["content"] for body in outbox(store))


def test_a_holding_read_early_in_a_long_run_stays_active(store, settings):
    engine, market, report = held_report(store, settings)
    records = store.evidence(report.run_id)
    late = report.model_copy(update={"created_at": report.created_at + timedelta(minutes=15)})
    assert candidate_state(late, records, None) == State.ACTIVE
    for record in records:
        if record.evidence_type == "portfolio":
            record.payload["holdings"] = []
    # An empty but stale account read cannot conclude an exit; a fresh one can.
    assert candidate_state(late, records, report) == State.ACTIVE
    assert candidate_state(report, records, report) == State.EXITED


def decision(**changes):
    base = {
        "rating": "Hold",
        "confidence": "중간",
        "new_entry_action": "WAIT",
        "new_entry_note": "",
        "holder_action": "HOLD",
        "holder_note": "",
        "executive_summary": "s",
        "thesis": [
            {
                "classification": "INTERPRETATION",
                "text": "t",
                "evidence_ids": ["levels"],
                "confidence": "중간",
            }
        ],
        "thesis_state": "ACTIVE",
        "invalidation_conditions": [],
        "monitoring_checklist": [],
        "material_changes": [],
        "material_gaps": [],
        "trade_plan": None,
    }
    return PortfolioDecision.model_validate(base | changes)


def records(quantity="5"):
    fixture = FixtureAdapters()
    portfolio = fixture.portfolio("TEST")
    portfolio.payload["holdings"] = [holding(quantity)] if quantity else []
    levels = EvidenceRecord(
        evidence_id="levels",
        ticker="TEST",
        evidence_type="calculation",
        source_name="test",
        source_tier=2,
        source_url="fixture://levels",
        content_hash="levels",
        facts=[
            {"name": n, "value": v, "unit": "USD/share", "currency": "USD"}
            for n, v in (("support", 92), ("resistance", 115), ("above", 104))
        ],
    )
    return [fixture.quote("TEST"), levels, portfolio]


def guard(stop=92, take_profit=()):
    def level(value):
        return {
            "value": value,
            "evidence_ids": ["levels"],
            "basis": "b",
            "classification": "INTERPRETATION",
        }

    return {
        "currency": "USD",
        "stop": level(stop),
        "take_profit": [level(v) for v in take_profit],
        "rationale": "r",
    }


@pytest.mark.parametrize(
    "changes,quantity,code",
    [
        ({}, "5", "MISSING_POSITION_GUARD"),
        ({"position_guard": guard(104)}, "5", "GUARD_GEOMETRY"),
        ({"position_guard": guard(92, [92])}, "5", "GUARD_GEOMETRY"),
        ({"position_guard": guard()}, "", "GUARD_WITHOUT_POSITION"),
    ],
)
def test_holding_guard_rules(changes, quantity, code):
    result = validate_decision(
        decision(**changes), records(quantity), ResearchRequest(ticker="TEST"), utcnow()
    )
    assert code in {issue.code for issue in result.issues}


@pytest.mark.parametrize(
    "changes",
    [
        {"position_guard": guard(92, [115])},
        {"rating": "Sell", "new_entry_action": "AVOID", "holder_action": "EXIT"},
        {
            "rating": "판단 보류",
            "new_entry_action": "DEFER",
            "holder_action": "DEFER",
            "thesis": [],
            "thesis_state": "UNKNOWN",
            "material_gaps": [{"description": "실적 확인 불가", "severity": "blocking"}],
            "position_guard": guard(),
        },
    ],
)
def test_valid_holding_decisions(changes):
    result = validate_decision(
        decision(**changes), records(), ResearchRequest(ticker="TEST"), utcnow()
    )
    assert result.valid, result.issues


def ny(*args):
    return datetime(*args, tzinfo=NEW_YORK)


@pytest.mark.parametrize(
    "now,expected",
    [
        (ny(2026, 10, 3, 12, 0), ny(2026, 10, 5, 9, 40)),  # Saturday -> Monday
        (ny(2026, 10, 1, 8, 0), ny(2026, 10, 1, 9, 40)),  # before the open
        (ny(2026, 10, 1, 9, 45), ny(2026, 10, 2, 9, 40)),  # after today's slot
    ],
)
def test_routine_research_runs_after_the_open_on_trading_days(settings, now, expected):
    assert next_research_at(settings, FixtureAdapters(), now) == expected


def test_interval_schedule_and_first_research(settings):
    interval = settings.model_copy(update={"research_schedule": "interval"})
    now = ny(2026, 10, 3, 12, 0)
    assert next_research_at(interval, FixtureAdapters(), now) == now + timedelta(hours=24)
    assert first_research_at(interval, FixtureAdapters(), now) == now
    assert first_research_at(settings, FixtureAdapters(), now) == ny(2026, 10, 5, 9, 40)
    in_session = ny(2026, 10, 1, 11, 0)
    assert first_research_at(settings, FixtureAdapters(), in_session) == in_session
