from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.evidence import content_hash
from app.models import EvidenceRecord, NumericFact, Security, utcnow

FIXTURE_ETFS = {"FUND"}
NEW_YORK = ZoneInfo("America/New_York")


class FixtureAdapters:
    """Clearly synthetic, deterministic data for offline verification."""

    def record(self, ticker, kind, payload, facts=None, **kwargs):
        return EvidenceRecord(
            ticker=ticker,
            evidence_type=kind,
            source_name="SYNTHETIC FIXTURE",
            source_tier=2,
            source_url=f"fixture://synthetic/{kind}",
            content_hash=content_hash(payload),
            payload=payload,
            facts=facts or [],
            warnings=["Synthetic fixture; not real market evidence"],
            **kwargs,
        )

    def identity(self, ticker):
        etf = ticker in FIXTURE_ETFS
        return self.record(
            ticker,
            "identity",
            Security(
                ticker=ticker,
                name="Synthetic Test Fund" if etf else "Synthetic Test Issuer",
                exchange="TEST",
                country="US",
                currency="USD",
                asset_type="etf" if etf else "equity",
                security_type="ETF" if etf else "STOCK",
            ).model_dump(mode="json"),
            []
            if etf
            else [NumericFact(name="shares_outstanding", value="100000000", unit="shares")],
        )

    def quote(self, ticker):
        result = self.quotes([ticker])[ticker]
        if isinstance(result, Exception):
            raise result
        return result

    def quotes(self, tickers):
        return {
            ticker: self.record(
                ticker,
                "quote",
                {"price": "100", "currency": "USD", "market_state": "REGULAR"},
                [NumericFact(name="last_price", value="100", unit="USD/share", currency="USD")],
                effective_at=utcnow(),
                stale_after_seconds=90,
            )
            for ticker in tickers
        }

    def next_regular_open(self, after):
        """Synthetic calendar: weekdays 09:30-16:00 New York, no holidays."""
        day = after.astimezone(NEW_YORK).date()
        for offset in range(8):
            start = datetime.combine(day + timedelta(days=offset), time(9, 30), NEW_YORK)
            if start.weekday() < 5 and start > after:
                return start
        return None

    def regular_session_open(self, at):
        local = at.astimezone(NEW_YORK)
        return local.weekday() < 5 and time(9, 30) <= local.time() < time(16)

    def ohlcv(self, ticker, count=300):
        bars = []
        for i in range(count):
            value = Decimal("70") + Decimal(i) / 10
            bars.append(
                {
                    "timestamp": (utcnow() - timedelta(days=count - i)).isoformat(),
                    "open": str(value),
                    "high": str(value + 2),
                    "low": str(value - 2),
                    "close": str(value + 1),
                    "volume": "1000000",
                }
            )
        return self.record(
            ticker,
            "ohlcv",
            {"currency": "USD", "adjusted": True, "candles": bars},
            effective_at=utcnow() - timedelta(days=1),
            stale_after_seconds=5 * 86400,
        )

    def portfolio(self, ticker):
        from app.adapters.toss import portfolio_facts

        payload = {
            "holdings": [],
            "complete": True,
            "holdings_complete": True,
            "currency": "USD",
            "buying_power": "10000",
            "portfolio_value": None,
        }
        return self.record(
            ticker,
            "portfolio",
            payload,
            portfolio_facts(payload),
            effective_at=utcnow(),
            stale_after_seconds=300,
        )

    def account_snapshot(self):
        return self.portfolio("ACCOUNT")

    def fees(self, ticker):
        return self.record(
            ticker, "fees", {"rate": "0.001"}, effective_at=utcnow(), stale_after_seconds=86400
        )

    def filings(self, ticker):
        return self.record(
            ticker, "filings", {"filings": [{"form": "SYNTHETIC", "url": "fixture://filing"}]}
        )

    def financials(self, ticker):
        return self.record(
            ticker,
            "financials",
            {"basis": "GAAP", "synthetic": True},
            [
                NumericFact(
                    name="revenue",
                    value="100000000",
                    unit="USD",
                    currency="USD",
                    accounting_basis="GAAP",
                )
            ],
        )

    def fund_holdings(self, ticker):
        return self.record(
            ticker,
            "fund_holdings",
            {
                "series_name": "Synthetic Test Fund",
                "report_date": "2026-06-30",
                "holdings": [{"name": "Synthetic Holding", "pct": "5"}],
            },
            [
                NumericFact(name="net_assets", value="1000000000", unit="USD", currency="USD"),
                NumericFact(name="holdings_count", value="100", unit="count"),
            ],
        )

    def search(self, ticker, query, count=10, **options):
        return self.record(
            ticker,
            "search",
            {
                "query": query,
                "options": options,
                "results": [{"url": "https://fixture.example/issuer-release"}],
            },
            usable_as_fact=False,
        )

    def read(self, ticker, url):
        record = self.record(ticker, "document", {"original_url": url})
        record.text = "Synthetic issuer release: evidence-backed testing only."
        return record

    def universe(self):
        return [self.identity(ticker).payload for ticker in ("DEMO", "TEST")]

    def liquidity_leaders(self, count=100):
        """Synthetic stand-in for the broker's trading-amount ranking."""
        return ["TEST", "DEMO"][:count]

    def stock_details(self, tickers):
        return {ticker: {"shares_outstanding": "100000000"} for ticker in tickers}
