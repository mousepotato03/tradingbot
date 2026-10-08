import json
from decimal import Decimal

import pytest

from app.adapters.fixture import FixtureAdapters
from app.discovery import Discovery
from app.engine import ResearchEngine
from app.lifecycle import transition
from app.llm import FixtureModel, ModelTurn
from app.models import CandidateState as State
from app.models import ResearchRequest, TriageDecision
from app.storage import StateRow


@pytest.mark.parametrize("state", list(State))
def test_unchanged_state_is_idempotent(state):
    assert transition(state, state) == state


@pytest.mark.parametrize(
    "old,new",
    [
        (State.UNIVERSE, State.RESEARCH),
        (State.RESEARCH, State.WATCH),
        (State.RESEARCH, State.ENTRY),
        (State.WATCH, State.ENTRY),
        (State.ENTRY, State.WATCH),
        (State.WATCH, State.RESEARCH),
        (State.ENTRY, State.ACTIVE),
        (State.WATCH, State.ACTIVE),
        (State.ACTIVE, State.EXITED),
        (State.EXITED, State.RESEARCH),
        (State.INVALIDATED, State.RESEARCH),
        (State.RESEARCH, State.INVALIDATED),
    ],
)
def test_research_entry_and_actual_holding_lifecycle(old, new):
    assert transition(old, new) == new


@pytest.mark.parametrize(
    "old,new",
    [
        (State.UNIVERSE, State.ENTRY),
        (State.UNIVERSE, State.EXITED),
        (State.WATCH, State.EXITED),
        (State.ACTIVE, State.WATCH),
        (State.ACTIVE, State.INVALIDATED),
        (State.INVALIDATED, State.ENTRY),
    ],
)
def test_cannot_skip_research_or_fabricate_a_position_exit(old, new):
    with pytest.raises(ValueError):
        transition(old, new)


def test_discovery_keeps_verified_universe_and_rotates_batch(store, settings):
    discovery = Discovery(ResearchEngine(settings, store))
    first, second = discovery.scan(1), discovery.scan(1)
    assert len(first) == len(second) == 1 and first != second
    assert not discovery.scan(1)
    with store.transaction() as session:
        assert session.get(StateRow, "TEST").state == State.RESEARCH


class Universe(FixtureAdapters):
    """Synthetic universe with controllable shares, trends and security types."""

    def __init__(self, rows, shares=None, falling=()):
        self.rows, self.shares, self.falling = rows, shares or {}, set(falling)
        self.ohlcv_calls = []

    def universe(self):
        return [
            {
                "ticker": ticker,
                "name": ticker,
                "exchange": "NYSE",
                "country": "US",
                "currency": "USD",
                "asset_type": "etf" if kind == "ETF" else "equity",
                "security_type": kind,
            }
            for ticker, kind in self.rows
        ]

    def liquidity_leaders(self, count=100):
        return [ticker for ticker, _ in self.rows][::-1]

    def stock_details(self, tickers):
        return {t: {"shares_outstanding": self.shares.get(t, "100000000")} for t in tickers}

    def ohlcv(self, ticker, count=300):
        self.ohlcv_calls.append(ticker)
        record = super().ohlcv(ticker, count)
        if ticker in self.falling:
            candles = record.payload["candles"]
            closes = [c["close"] for c in candles][::-1]
            for candle, close in zip(candles, closes, strict=True):
                candle["close"] = close
                candle["open"] = close
                candle["high"] = str(Decimal(close) + 2)
                candle["low"] = str(Decimal(close) - 2)
        return record


def discovery(store, settings, market, model=None):
    return Discovery(ResearchEngine(settings, store, model=model, market=market))


def test_size_is_a_gate_but_a_downtrend_only_ranks(store, settings):
    market = Universe(
        [("UPTR", "STOCK"), ("DOWN", "STOCK"), ("TINY", "STOCK")],
        shares={"TINY": "1000"},
        falling={"DOWN"},
    )
    run_ids = discovery(store, settings, market).scan(5)
    queued = {store.get_run(r)["ticker"] for r in run_ids}
    assert queued == {"UPTR", "DOWN"}
    with store.transaction() as session:
        tiny = session.get(StateRow, "TINY")
        assert tiny.state == State.UNIVERSE and tiny.body["screen"]["reason"] == "GATE_MARKET_CAP"
    screens = {t: store.observations(t, "screening")[0] for t in ("UPTR", "DOWN")}
    score = {
        t: next(f.value for f in r.facts if f.name == "screen_score") for t, r in screens.items()
    }
    assert score["UPTR"] > score["DOWN"]
    first = store.get_run(run_ids[0])
    assert first["ticker"] == "UPTR" and first["request"]["trigger"].startswith("발굴")


def test_triage_skip_is_remembered_and_not_rescreened(store, settings):
    class Skip(FixtureModel):
        def complete(self, role, messages, tools, schema):
            if schema is TriageDecision:
                evidence = json.loads(messages[1]["content"])["evidence"][0]["evidence_id"]
                return ModelTurn(
                    result=TriageDecision(
                        priority="skip", reasons=["조사 우선순위 낮음"], evidence_ids=[evidence]
                    )
                )
            return super().complete(role, messages, tools, schema)

    market = Universe([("SKIP", "STOCK")])
    assert discovery(store, settings, market, Skip()).scan(1) == []
    with store.transaction() as session:
        body = session.get(StateRow, "SKIP").body
        assert body["triage"]["priority"] == "skip" and body["screen_settled_at"]
    assert discovery(store, settings, market, Skip()).scan(1) == []
    assert market.ohlcv_calls == ["SKIP"]


def test_triage_citing_unknown_evidence_is_not_trusted(store, settings):
    class Invented(FixtureModel):
        def complete(self, role, messages, tools, schema):
            return ModelTurn(
                result=TriageDecision(priority="deep_research", reasons=["x"], evidence_ids=["no"])
            )

    assert discovery(store, settings, Universe([("SOME", "STOCK")]), Invented()).scan(1) == []
    with store.transaction() as session:
        assert session.get(StateRow, "SOME").body["triage"]["error"] == "TRIAGE_UNKNOWN_EVIDENCE"


def test_malformed_triage_output_does_not_abort_the_scan(store, settings):
    class Malformed(FixtureModel):
        def complete(self, role, messages, tools, schema):
            return ModelTurn(result=TriageDecision.model_validate_json('{"priority": "maybe"}'))

    market = Universe([("ONE", "STOCK"), ("TWO", "STOCK")])
    assert discovery(store, settings, market, Malformed()).scan(2) == []
    with store.transaction() as session:
        for ticker in ("ONE", "TWO"):
            assert session.get(StateRow, ticker).body["triage"]["error"] == "TRIAGE_INVALID"


def test_receipts_and_etfs_are_not_screened_by_default(store, settings):
    market = Universe([("ADR", "DEPOSITARY_RECEIPT"), ("FUND", "ETF"), ("CORP", "STOCK")])
    run_ids = discovery(store, settings, market).scan(5)
    assert [store.get_run(r)["ticker"] for r in run_ids] == ["CORP"]
    assert market.ohlcv_calls == ["CORP"]


def test_outcomes_mature_once_and_deferrals_settle(store, settings):
    from datetime import timedelta

    from sqlalchemy import select

    from app.evaluation import OutcomeTracker
    from app.models import utcnow
    from app.storage import OutcomeRow, ReportRow

    class Counting(FixtureAdapters):
        calls = 0

        def ohlcv(self, ticker, count=300):
            Counting.calls += 1
            return super().ohlcv(ticker, count)

    engine = ResearchEngine(settings, store, market=Counting())
    held = engine.run(store.enqueue(ResearchRequest(ticker="TEST")))
    deferred = ResearchEngine(settings, store, model=FixtureModel("missing")).run(
        store.enqueue(ResearchRequest(ticker="DEMO"))
    )
    with store.transaction() as session:
        row = session.get(ReportRow, held.run_id)
        row.body = {**row.body, "created_at": (utcnow() - timedelta(days=100)).isoformat()}
    Counting.calls = 0
    tracker = OutcomeTracker(engine)
    assert tracker.update() == 1
    assert Counting.calls == 2  # the report ticker and the benchmark
    with store.transaction() as session:
        rows = {r.report_id: r for r in session.scalars(select(OutcomeRow))}
        assert rows[held.run_id].mature and "60" in rows[held.run_id].body["metrics"]
        assert rows[deferred.run_id].mature and "skipped" in rows[deferred.run_id].body
    assert tracker.update() == 0 and Counting.calls == 2


def test_folded_trigger_keeps_only_signals_left_to_the_research_alert():
    from app.lifecycle import folded_trigger

    trigger = (
        "POSITION_CHANGED: 보유 수량 변경: AAA 3 → 5주; "
        "HOLDER_STOP: 현재가 9 ≤ 보유 손절선 10; DATA_QUALITY_FAILURE: 가격 근거 조회 실패"
    )
    assert folded_trigger(trigger) == "보유 수량 변경: AAA 3 → 5주 / 가격 근거 조회 실패"
    assert folded_trigger("정기 재조사: 24시간 경과") == "" and folded_trigger(None) == ""
