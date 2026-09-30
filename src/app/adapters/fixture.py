from datetime import timedelta
from decimal import Decimal

from app.evidence import content_hash
from app.models import EvidenceRecord, NumericFact, Security, utcnow


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
        return self.record(
            ticker,
            "identity",
            Security(
                ticker=ticker,
                name="Synthetic Test Issuer",
                exchange="TEST",
                country="US",
                currency="USD",
            ).model_dump(mode="json"),
        )

    def quote(self, ticker):
        return self.record(
            ticker,
            "quote",
            {"price": "100", "currency": "USD", "market_state": "REGULAR"},
            [NumericFact(name="last_price", value="100", unit="USD/share", currency="USD")],
            effective_at=utcnow(),
            stale_after_seconds=90,
        )

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
        return self.record(ticker, "ohlcv", {"currency": "USD", "adjusted": True, "candles": bars})

    def portfolio(self, ticker):
        return self.record(
            ticker,
            "portfolio",
            {
                "holdings": [],
                "complete": True,
                "holdings_complete": True,
                "currency": "USD",
                "buying_power": "10000",
                "portfolio_value": None,
            },
            effective_at=utcnow(),
            stale_after_seconds=300,
        )

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

    def search(self, ticker, query, count=10):
        return self.record(
            ticker,
            "search",
            {"query": query, "results": [{"url": "https://fixture.example/issuer-release"}]},
            usable_as_fact=False,
        )

    def read(self, ticker, url):
        record = self.record(ticker, "document", {"original_url": url})
        record.text = "Synthetic issuer release: evidence-backed testing only."
        return record

    def universe(self):
        return [self.identity(ticker).payload for ticker in ("DEMO", "TEST")]
