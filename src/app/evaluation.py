from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo


def forward_outcomes(prices: list[Decimal], benchmark: list[Decimal]) -> dict:
    if (
        not prices
        or len(prices) != len(benchmark)
        or any(p <= 0 or not p.is_finite() for p in prices + benchmark)
    ):
        raise ValueError("Aligned, positive session prices required")
    result = {}
    for sessions in (1, 5, 20, 60):
        if len(prices) > sessions:
            stock_return = prices[sessions] / prices[0] - 1
            benchmark_return = benchmark[sessions] / benchmark[0] - 1
            result[str(sessions)] = {
                "return": str(stock_return),
                "benchmark_relative": str(stock_return - benchmark_return),
            }
    result["close_mfe"] = str(max(prices) / prices[0] - 1)
    result["close_mae"] = str(min(prices) / prices[0] - 1)
    result["realized_r"] = None  # Requires independently confirmed actual fills.
    return result


def excursions(highs: list[Decimal], lows: list[Decimal], entry: Decimal) -> dict:
    if (
        not highs
        or len(highs) != len(lows)
        or not all(v.is_finite() for v in highs + lows + [entry])
        or not entry > 0
        or any(h < lo or lo <= 0 for h, lo in zip(highs, lows, strict=True))
    ):
        raise ValueError("Validated high/low session series required")
    return {
        "mfe": str(max(Decimal(0), max(highs) / entry - 1)),
        "mae": str(min(Decimal(0), min(lows) / entry - 1)),
    }


def reference_date(created_at: datetime):
    if created_at.tzinfo is None:
        raise ValueError("Report timestamp requires timezone")
    local = created_at.astimezone(ZoneInfo("America/New_York"))
    return local.date() + timedelta(days=int(local.hour >= 16))


class OutcomeTracker:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def update(self):
        from sqlalchemy import select

        from app.models import utcnow
        from app.storage import OutcomeRow, ReportRow

        # Settled reports (all windows observed, or nothing to evaluate) are never reloaded.
        with self.store.transaction() as session:
            settled = select(OutcomeRow.report_id).where(OutcomeRow.mature.is_(True))
            reports = [
                (row.id, dict(row.body))
                for row in session.scalars(select(ReportRow).where(ReportRow.id.not_in(settled)))
            ]
        updated = 0
        cache = {}
        for report_id, report in reports:
            if report["decision"]["rating"] == "판단 보류":
                with self.store.transaction() as session:
                    session.merge(
                        OutcomeRow(
                            id=report_id + ":forward",
                            report_id=report_id,
                            body={"skipped": "판단 보류 carries no directional decision"},
                            mature=True,
                        )
                    )
                continue
            bars = []
            for ticker in (report["ticker"], self.engine.settings.benchmark_ticker):
                if ticker not in cache:
                    cache[ticker] = self.engine.market.ohlcv(ticker, count=1000)
                bars.append(cache[ticker])
            by_date = [
                {
                    datetime.fromisoformat(bar["timestamp"])
                    .astimezone(ZoneInfo("America/New_York"))
                    .date(): bar
                    for bar in record.payload["candles"]
                }
                for record in bars
            ]
            start = reference_date(datetime.fromisoformat(report["created_at"]))
            dates = sorted(d for d in set(by_date[0]) & set(by_date[1]) if d >= start)[:61]
            if len(dates) < 2:
                continue
            closes = [[Decimal(by_date[i][d]["close"]) for d in dates] for i in (0, 1)]
            result = forward_outcomes(closes[0], closes[1])
            result.update(
                excursions(
                    [Decimal(by_date[0][d]["high"]) for d in dates[1:]],
                    [Decimal(by_date[0][d]["low"]) for d in dates[1:]],
                    closes[0][0],
                )
            )
            body = {
                "metrics": result,
                "reference_session": str(dates[0]),
                "last_session": str(dates[-1]),
                "reference": "First common adjusted session close on/after NY report date (next date after 16:00); subsequent-session excursions; not an actual fill",
                "benchmark": self.engine.settings.benchmark_ticker,
                "retrieved_at": utcnow().isoformat(),
                "source_hashes": [r.content_hash for r in bars],
                "fixture": report["fixture"],
            }
            # Reference session plus 60 forward sessions completes every tracked window.
            mature = len(dates) >= 61
            body["mature"] = mature
            with self.store.transaction() as session:
                session.merge(
                    OutcomeRow(
                        id=report_id + ":forward", report_id=report_id, body=body, mature=mature
                    )
                )
            updated += 1
        return updated
