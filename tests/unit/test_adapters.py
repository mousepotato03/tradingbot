from unittest.mock import Mock

import httpx
import pytest

from app.adapters.http import ToolError, Transport, public_url
from app.adapters.sec import SecAdapter
from app.adapters.toss import TossAdapter
from app.adapters.web import BraveSearch, DocumentReader


def test_brave_search_contract_and_snippet_quality(settings):
    def handler(request):
        assert request.headers["X-Subscription-Token"] == "fixture-key"
        assert request.url.params["q"] == "issuer risks"
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Release",
                            "url": "https://example.com/release",
                            "description": "Discovery only",
                        }
                    ]
                }
            },
        )

    from pydantic import SecretStr

    settings.brave_api_key = SecretStr("fixture-key")
    adapter = BraveSearch(settings, Transport(httpx.Client(transport=httpx.MockTransport(handler))))
    result = adapter.search("TEST", "issuer risks")
    assert not result.usable_as_fact and result.payload["results"][0]["url"].endswith("release")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/x",
        "http://169.254.169.254/",
        "https://user:pass@example.com/",
        "http://example.com:8000/",
    ],
)
def test_reader_blocks_private_and_credential_urls(url):
    with pytest.raises(ToolError):
        public_url(url, resolver=lambda *a: [(None, None, None, None, ("127.0.0.1", 80))])


def test_reader_extracts_original_text_and_date(settings):
    transport = Mock()
    transport.public_get.return_value = (
        "https://example.com/release",
        "text/html",
        b'<title>Release</title><meta property="article:published_time" content="2026-01-01T12:00:00Z"><p>Revenue $123 million</p>',
    )
    record = DocumentReader(settings, transport).read("TEST", "https://example.com/release")
    assert "Revenue $123 million" in record.text
    assert record.published_at.year == 2026
    assert record.payload["extraction_method"] == "html-text"


def test_toss_read_only_and_auth_redaction(settings, monkeypatch):
    monkeypatch.setattr("app.adapters.toss.time.sleep", lambda _: None)
    transport = Mock()
    transport.request.side_effect = ToolError("HTTP_403")
    adapter = TossAdapter(settings, transport)
    with pytest.raises(ToolError, match="READ_ONLY_VIOLATION"):
        adapter._send("POST", "/api/v1/orders")
    assert not transport.request.called
    with pytest.raises(ToolError, match="HTTP_403"):
        adapter.get("/api/v1/accounts")


def test_toss_quote_without_timestamp_not_fresh(settings):
    adapter = TossAdapter(settings)
    adapter.get = Mock(
        return_value=[{"symbol": "TEST", "currency": "USD", "lastPrice": "100", "timestamp": None}]
    )
    with pytest.raises(ToolError, match="QUOTE_TIMESTAMP_UNAVAILABLE"):
        adapter.quote("TEST")


def test_toss_ohlcv_order_pagination_and_completed_sessions(settings):
    adapter = TossAdapter(settings)

    def bar(day, price):
        return {
            "timestamp": f"2025-01-{day:02}T00:00:00-05:00",
            "currency": "USD",
            "openPrice": str(price),
            "highPrice": str(price + 1),
            "lowPrice": str(price - 1),
            "closePrice": str(price),
            "volume": "1000",
        }

    adapter.get = Mock(
        side_effect=[
            {"candles": [bar(3, 103), bar(2, 102)], "nextBefore": "older"},
            {"candles": [bar(2, 102), bar(1, 101)], "nextBefore": None},
        ]
    )
    result = adapter.ohlcv("TEST", 3)
    assert [b["close"] for b in result.payload["candles"]] == ["101", "102", "103"]


def test_sec_preserves_units_periods_and_accession(settings):
    adapter = SecAdapter(settings)
    adapter.cik = Mock(return_value="0000000001")
    adapter._get = Mock(
        return_value={
            "facts": {
                "us-gaap": {
                    "Revenues": {
                        "units": {
                            "USD": [
                                {
                                    "val": 123,
                                    "start": "2025-01-01",
                                    "end": "2025-12-31",
                                    "filed": "2026-02-01",
                                    "accn": "accession",
                                    "form": "10-K",
                                }
                            ]
                        }
                    }
                }
            }
        }
    )
    record = adapter.financials("TEST")
    assert record.facts[0].unit == "USD" and record.facts[0].accounting_basis == "GAAP"
    assert "accession" in record.facts[0].name
    assert record.facts[0].period_start == "2025-01-01"
