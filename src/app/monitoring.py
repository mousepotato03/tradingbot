from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.adapters.http import ToolError
from app.evidence import content_hash, evidence_is_fresh
from app.models import ResearchRequest, utcnow
from app.storage import OutboxRow, RunRow, WatchRow


def error_code(error):
    if isinstance(error, ToolError):
        return error.code
    if isinstance(error, ValueError) and str(error) in {"STALE_QUOTE", "INCOMPLETE_ACCOUNT"}:
        return str(error)
    return type(error).__name__


class Monitor:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def watch(self, ticker):
        request = ResearchRequest(ticker=ticker)
        with self.store.transaction() as session:
            if not session.get(WatchRow, request.ticker):
                session.add(WatchRow(ticker=request.ticker))

    def tick(self):
        now = utcnow()
        with self.store.transaction() as session:
            watched = [
                (row.ticker, dict(row.body))
                for row in session.scalars(
                    select(WatchRow).where(WatchRow.next_condition_at <= now)
                ).all()
            ]
        for ticker, body in watched:
            previous = self.store.previous(ticker)
            signals, details, error = set(), {}, None
            plan = previous.decision.trade_plan if previous else None
            if plan and previous.validation.valid:
                try:
                    quote = self.engine.market.quote(ticker)
                    if not evidence_is_fresh(quote, utcnow()):
                        raise ValueError("STALE_QUOTE")
                    price = Decimal(str(quote.payload["price"]))
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
                    if (
                        plan.entry_low.value <= price <= plan.entry_high.value
                        and quote.payload["market_state"] == "REGULAR"
                    ):
                        entry_signal = (
                            "ENTRY_REVIEW_REQUIRED" if plan.conditions else "ENTRY_CONDITION"
                        )
                        signals.add(entry_signal)
                        details[entry_signal] = (
                            f"현재가 {price} {plan.currency}; 진입 범위 {plan.entry_low.value}~{plan.entry_high.value} 도달 (정규장)"
                        )
                        if plan.conditions:
                            details[entry_signal] += "; 추가 조건 재검토 필요: " + "; ".join(
                                plan.conditions
                            )
                    self.store.add_evidence(previous.run_id, quote)
                except Exception as exc:
                    error = error_code(exc)
                    signals.add("DATA_QUALITY_FAILURE")
                    details["DATA_QUALITY_FAILURE"] = (
                        f"가격 근거 조회 실패: {error}; 기존 거래안을 재검증해야 합니다."
                    )
            if previous:
                next_account = body.get("next_account_at")
                if next_account is None or datetime.fromisoformat(next_account) <= now:
                    try:
                        portfolio = self.engine.market.portfolio(ticker)
                        if not evidence_is_fresh(portfolio, utcnow()) or not portfolio.payload.get(
                            "holdings_complete"
                        ):
                            raise ValueError("INCOMPLETE_ACCOUNT")
                        quantities = {
                            h["ticker"]: h["quantity"] for h in portfolio.payload["holdings"]
                        }
                        old = body.get("quantities")
                        if old is not None and old != quantities:
                            signals.add("POSITION_CHANGED")
                            changed = [
                                f"{symbol}: {old.get(symbol, 0)} → {quantities.get(symbol, 0)}주"
                                for symbol in sorted(set(old) | set(quantities))
                                if old.get(symbol, 0) != quantities.get(symbol, 0)
                            ]
                            details["POSITION_CHANGED"] = "보유 수량 변경: " + "; ".join(changed)
                        body["quantities"] = quantities
                        self.store.add_evidence(previous.run_id, portfolio)
                    except Exception as exc:
                        error = error_code(exc)
                        signals.add("DATA_QUALITY_FAILURE")
                        details["DATA_QUALITY_FAILURE"] = (
                            f"계좌 근거 조회 실패: {error}; 마지막 확인된 보유 상태를 유지합니다."
                        )
                    body["next_account_at"] = (now + timedelta(seconds=300)).isoformat()
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
                    row.next_research_at = now + timedelta(hours=24)
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
                    ResearchRequest(
                        ticker=ticker,
                        mode="critical" if "INVALIDATION_PRICE" in new_signals else "deep",
                    )
                )
