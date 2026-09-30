import time
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from app.adapters.http import ToolError
from app.evidence import content_hash, evidence_is_fresh
from app.models import CandidateState, ResearchRequest, Security, utcnow
from app.schedule import first_research_at, next_research_at
from app.storage import OutboxRow, RunRow, WatchRow

# Price-level signals are only (re)evaluated on regular-session prices.
PRICE_SIGNALS = (
    "INVALIDATION_PRICE",
    "TARGET_",
    "ENTRY_CONDITION",
    "ENTRY_REVIEW_REQUIRED",
    "HOLDER_STOP",
    "HOLDER_TAKE_PROFIT_",
)


def error_code(error):
    if isinstance(error, ToolError):
        return error.code
    if isinstance(error, ValueError) and str(error) in {"STALE_QUOTE", "INCOMPLETE_ACCOUNT"}:
        return str(error)
    return type(error).__name__


class Monitor:
    """Condition checks for watched candidates.

    Quotes are read in one batch per tick and the account in one shared snapshot; observations
    are stored separately so a completed report's evidence ledger never changes.
    """

    def __init__(self, engine, clock=time.monotonic):
        self.engine, self.store, self.clock = engine, engine.store, clock
        self._account = None  # (monotonic fetch time, record or None, error or None)

    def watch(self, ticker, request: ResearchRequest | None = None):
        request = request or ResearchRequest(ticker=ticker)
        base = request.model_dump(mode="json", exclude={"as_of", "trigger"})
        with self.store.transaction() as session:
            row = session.get(WatchRow, request.ticker)
            if row is None:
                session.add(WatchRow(ticker=request.ticker, body={"base_request": base}))
            elif "base_request" not in row.body:
                row.body = {**row.body, "base_request": base}

    def _account_snapshot(self):
        """One account read per interval for all watched tickers; returns (record, error, new)."""
        interval = self.engine.settings.monitor_account_interval_seconds
        if self._account and self.clock() - self._account[0] < interval:
            return self._account[1], self._account[2], False
        record, error = None, None
        try:
            record = self.engine.market.account_snapshot()
            if not evidence_is_fresh(record, utcnow()) or not record.payload.get(
                "holdings_complete"
            ):
                raise ValueError("INCOMPLETE_ACCOUNT")
            self.store.add_observation("ACCOUNT", "account", record)
        except Exception as exc:
            record, error = None, error_code(exc)
        self._account = (self.clock(), record, error)
        return record, error, True

    def _watch_holdings(self, account, now):
        """Watch every USD position in the account; new ones are due for research at `now`."""
        settings = self.engine.settings
        for holding in account.payload["holdings"]:
            try:
                ticker = Security.ticker_valid(holding["ticker"])
                quantity = Decimal(str(holding["quantity"]))
            except (KeyError, ValueError, ArithmeticError):
                continue
            if holding.get("currency") != "USD" or quantity <= 0:
                continue
            base = ResearchRequest(
                ticker=ticker, mode=settings.holding_research_mode, investor_status="기존 보유"
            ).model_dump(mode="json", exclude={"as_of", "trigger"})
            with self.store.transaction() as session:
                if session.get(WatchRow, ticker) is None:
                    session.add(
                        WatchRow(
                            ticker=ticker,
                            next_research_at=first_research_at(
                                self.engine.settings, self.engine.market, now
                            ),
                            next_condition_at=now,
                            body={
                                "base_request": base,
                                "origin": "holding",
                                "quantity": str(quantity),
                            },
                        )
                    )

    @staticmethod
    def _check_levels(report, body, price, signals, details):
        """Compare a regular-session price with the report's validated levels."""
        plan, guard = report.decision.trade_plan, report.decision.position_guard
        if plan:
            if price <= plan.stop.value:
                signals.add("INVALIDATION_PRICE")
                details["INVALIDATION_PRICE"] = (
                    f"현재가 {price} {plan.currency} ≤ 무효화 가격 {plan.stop.value}"
                )
            for i, target in enumerate(plan.targets):
                if price >= target.value:
                    signals.add(f"TARGET_{i}")
                    details[f"TARGET_{i}"] = (
                        f"현재가 {price} {plan.currency} ≥ 목표 가격 {target.value}"
                    )
            if plan.entry_low.value <= price <= plan.entry_high.value:
                entry_signal = "ENTRY_REVIEW_REQUIRED" if plan.conditions else "ENTRY_CONDITION"
                signals.add(entry_signal)
                details[entry_signal] = (
                    f"현재가 {price} {plan.currency}; 진입 범위 "
                    f"{plan.entry_low.value}~{plan.entry_high.value} 도달 (정규장)"
                )
                if plan.conditions:
                    details[entry_signal] += "; 추가 조건 재검토 필요: " + "; ".join(
                        plan.conditions
                    )
        quantity = body.get("quantity")
        held = (
            Decimal(quantity) > 0
            if quantity is not None
            else report.candidate_state == CandidateState.ACTIVE
        )
        if guard and held:
            if price <= guard.stop.value:
                signals.add("HOLDER_STOP")
                details["HOLDER_STOP"] = (
                    f"현재가 {price} {guard.currency} ≤ 보유 손절선 {guard.stop.value} "
                    f"(근거: {guard.stop.basis})"
                )
            for i, level in enumerate(guard.take_profit):
                if price >= level.value:
                    signals.add(f"HOLDER_TAKE_PROFIT_{i}")
                    details[f"HOLDER_TAKE_PROFIT_{i}"] = (
                        f"현재가 {price} {guard.currency} ≥ 익절 검토 가격 {level.value} "
                        f"(근거: {level.basis})"
                    )

    def tick(self):
        now = utcnow()
        account, account_error, account_new = self._account_snapshot()
        if account_new and account is not None and self.engine.settings.holdings_auto_watch:
            self._watch_holdings(account, now)
        with self.store.transaction() as session:
            watched = [
                (row.ticker, dict(row.body))
                for row in session.scalars(
                    select(WatchRow).where(WatchRow.next_condition_at <= now)
                ).all()
            ]
        if not watched:
            return
        reports = {ticker: self.store.previous(ticker) for ticker, _ in watched}
        # Reports whose validated levels the monitor can check: an entry plan, a holding guard.
        priced = {
            ticker: report
            for ticker, report in reports.items()
            if report
            and report.validation.valid
            and (report.decision.trade_plan or report.decision.position_guard)
        }
        quotes, batch_error = {}, None
        if priced:
            try:
                quotes = self.engine.market.quotes(sorted(priced))
            except Exception as exc:
                batch_error = error_code(exc)
        for ticker, body in watched:
            previous = reports[ticker]
            signals, details, error = set(), {}, None
            if ticker in priced:
                try:
                    quote = quotes.get(ticker)
                    if batch_error or quote is None:
                        raise ToolError(batch_error or "QUOTE_UNAVAILABLE")
                    if isinstance(quote, Exception):
                        raise quote
                    self.store.add_observation(ticker, "quote", quote, previous.run_id)
                    if quote.payload["market_state"] != "REGULAR":
                        # Levels are judged on regular-session prices only. Outside it, keep
                        # what was already signalled so nothing is re-sent at the next open.
                        signals |= {
                            s for s in body.get("signals", []) if s.startswith(PRICE_SIGNALS)
                        }
                    else:
                        if not evidence_is_fresh(quote, utcnow()):
                            raise ValueError("STALE_QUOTE")
                        price = Decimal(str(quote.payload["price"]))
                        self._check_levels(previous, body, price, signals, details)
                except Exception as exc:
                    error = error_code(exc)
                    signals.add("DATA_QUALITY_FAILURE")
                    details["DATA_QUALITY_FAILURE"] = (
                        f"가격 근거 조회 실패: {error}; 기존 거래안과 손절선을 재검증해야 합니다."
                    )
            if previous and account_new:
                if account is not None:
                    quantity = str(
                        sum(
                            (
                                Decimal(str(h["quantity"]))
                                for h in account.payload["holdings"]
                                if h["ticker"] == ticker
                            ),
                            Decimal(0),
                        )
                    )
                    old = body.get("quantity")
                    if old is not None and Decimal(old) != Decimal(quantity):
                        signals.add("POSITION_CHANGED")
                        details["POSITION_CHANGED"] = (
                            f"보유 수량 변경: {ticker} {old} → {quantity}주"
                        )
                    body["quantity"] = quantity
                elif previous.candidate_state == CandidateState.ACTIVE:
                    # Account data only affects the current decision for held positions.
                    error = error or account_error
                    signals.add("DATA_QUALITY_FAILURE")
                    details["DATA_QUALITY_FAILURE"] = (
                        f"계좌 근거 조회 실패: {account_error}; 마지막 확인된 보유 상태를 유지합니다."
                    )
            new_signals = signals - set(body.get("signals", []))
            if "POSITION_CHANGED" in signals:
                new_signals.add("POSITION_CHANGED")
            queued = False
            with self.store.transaction() as session:
                row = session.get(WatchRow, ticker)
                pending = session.scalar(
                    select(RunRow)
                    .where(RunRow.ticker == ticker, RunRow.status.in_(["PENDING", "RUNNING"]))
                    .limit(1)
                )
                due = (
                    row.next_research_at.replace(tzinfo=now.tzinfo)
                    if row.next_research_at.tzinfo is None
                    else row.next_research_at
                )
                if pending is None and (due <= now or new_signals):
                    queued = True
                    row.next_research_at = next_research_at(
                        self.engine.settings, self.engine.market, now
                    )
                row.next_condition_at = now + timedelta(seconds=60)
                row.body = {**body, "signals": sorted(signals), "last_error": error}
                for signal in new_signals:
                    event_id = content_hash(
                        {
                            "ticker": ticker,
                            "report": previous.run_id if previous else None,
                            "signal": signal,
                            "detail": details[signal],
                        }
                    )
                    if session.get(OutboxRow, event_id) is None:
                        session.add(
                            OutboxRow(
                                id=event_id,
                                body={
                                    "ticker": ticker,
                                    "fixture": self.engine.settings.mode == "fixture",
                                    "expires_at": (now + timedelta(minutes=15)).isoformat(),
                                    "kind": signal,
                                    "content": f"{ticker} · {signal}\n{details[signal]}\n이전 보고서 {previous.run_id if previous else '없음'}의 조건과 비교한 변화로 재조사를 요청합니다.",
                                },
                            )
                        )
            if queued:
                self.store.enqueue(
                    self._follow_up(
                        ticker,
                        body,
                        new_signals,
                        details,
                        previous is None,
                        self.engine.settings.research_interval_hours,
                    )
                )

    @staticmethod
    def _follow_up(
        ticker, body, new_signals, details, first=False, interval_hours=24
    ) -> ResearchRequest:
        """Re-research with the investor's original conditions and what changed since then."""
        base = dict(body.get("base_request") or {})
        base.update(ticker=ticker, as_of=None)
        if new_signals & {"INVALIDATION_PRICE", "HOLDER_STOP"}:
            base["mode"] = "critical"
        # The account is authoritative for whether the investor holds the position now.
        quantity = body.get("quantity")
        if quantity is not None:
            held = Decimal(quantity) > 0
            if held and base.get("investor_status", "신규 진입 검토") == "신규 진입 검토":
                base["investor_status"] = "기존 보유"
            elif not held and base.get("investor_status") == "기존 보유":
                base["investor_status"] = "신규 진입 검토"
        if new_signals:
            base["trigger"] = "; ".join(
                f"{signal}: {details[signal]}" for signal in sorted(new_signals)
            )
        elif first and body.get("origin") == "holding":
            base["trigger"] = f"계좌 보유 종목 자동 감시 등록: 보유 수량 {quantity}주, 첫 조사"
        elif first:
            base["trigger"] = "감시 등록 후 첫 조사"
        else:
            base["trigger"] = f"정기 재조사: 이전 보고서 이후 {interval_hours}시간 경과"
        return ResearchRequest.model_validate(base)
