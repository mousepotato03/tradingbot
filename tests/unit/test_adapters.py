from decimal import Decimal
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


def test_sec_reads_ifrs_filers_with_reporting_currency(settings):
    adapter = SecAdapter(settings)
    adapter.cik = Mock(return_value="0001046179")

    def observation(value, form="20-F", start="2025-01-01"):
        return {
            "val": value,
            "start": start,
            "end": "2025-12-31",
            "filed": "2026-04-16",
            "accn": "acc-1",
            "form": form,
        }

    adapter._get = Mock(
        return_value={
            "facts": {
                "ifrs-full": {
                    "Revenue": {"units": {"TWD": [observation(100)], "USD": [observation(3)]}},
                    "LongtermBorrowings": {"units": {"TWD": [observation(5, start=None)]}},
                    "ShorttermBorrowings": {"units": {"TWD": [observation(2, start=None)]}},
                },
                "dei": {
                    "EntityCommonStockSharesOutstanding": {
                        "units": {"shares": [observation(25, start=None)]}
                    }
                },
            }
        }
    )
    record = adapter.financials("TSM")
    revenue = {f.unit: f for f in record.facts if f.name.startswith("revenue:")}
    assert set(revenue) == {"TWD", "USD"}
    assert revenue["TWD"].currency == "TWD" and revenue["TWD"].accounting_basis == "IFRS"
    assert revenue["TWD"].name.endswith(":TWD") and revenue["TWD"].name != revenue["USD"].name
    assert sum(f.name.startswith("debt:") for f in record.facts) == 2  # components kept
    assert any(f.name.startswith("shares:dei:") for f in record.facts)
    assert record.payload["basis"] == "IFRS"
    assert any("more than one currency" in w for w in record.warnings)


def atom_feed(form):
    entries = "".join(
        f"""<entry><content type="text/xml">
    <accession-number>0000000001-26-00000{n}</accession-number>
    <filing-date>{filed}</filing-date>
    <filing-href>https://www.sec.gov/Archives/edgar/data/1/00000000012600000{n}/index.htm</filing-href>
    <filing-type>{form}</filing-type>
  </content></entry>"""
        for n, filed in ((1, "2026-04-30"), (2, "2026-07-30"))
    )
    return (
        '<?xml version="1.0" encoding="ISO-8859-1" ?>'
        f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'
    ).encode()


def nport(series="S000000001", holdings=12):
    items = "".join(
        f"""<invstOrSec><name>Holding {i}</name><title>Holding {i}</title>
        <cusip>CUSIP{i:04}</cusip><identifiers><isin value="US000000{i:04}"/></identifiers>
        <balance>10</balance><units>NS</units><curCd>USD</curCd><valUSD>{100 * (i + 1)}</valUSD>
        <pctVal>{i + 1}</pctVal><payoffProfile>Long</payoffProfile><assetCat>EC</assetCat>
        <issuerCat>CORP</issuerCat><invCountry>{"US" if i % 3 else "JP"}</invCountry></invstOrSec>"""
        for i in range(holdings)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/nport"><formData>
<genInfo><seriesName>Synthetic Index ETF</seriesName><seriesId>{series}</seriesId>
<repPdEnd>2026-08-31</repPdEnd><repPdDate>2026-05-31</repPdDate></genInfo>
<fundInfo><totAssets>1000.5</totAssets><netAssets>990.25</netAssets></fundInfo>
<invstOrSecs>{items}</invstOrSecs></formData></edgarSubmission>""".encode()


FUND_MAPPING = {
    "fields": ["cik", "seriesId", "classId", "symbol"],
    "data": [[1, "S000000001", "C000000001", "SYNE"]],
}


def test_fund_holdings_from_latest_series_nport(settings):
    adapter = SecAdapter(settings)
    adapter._get = Mock(return_value=FUND_MAPPING)
    requested = []

    def raw(url):
        requested.append(url)
        if "type=NPORT-P" in url:
            return atom_feed("NPORT-P")
        if "type=497K" in url:
            return atom_feed("497K")
        return nport()

    adapter._get_raw = raw
    record = adapter.fund_holdings("SYNE")
    assert requested[1].endswith("000000000126000002/primary_doc.xml")  # latest filing
    facts = {f.name: f for f in record.facts}
    assert facts["net_assets"].value == Decimal("990.25") and facts["net_assets"].unit == "USD"
    assert facts["holdings_count"].value == 12
    # pctVal is already in percent: the ten largest weights are 12 + 11 + ... + 3.
    assert facts["top10_weight"].value == sum(range(3, 13))
    assert facts["country_weight:JP"].unit == "percent"
    assert record.payload["summary_prospectus"]["form"] == "497K"
    assert record.effective_at.date().isoformat() == "2026-05-31"


def test_fund_holdings_rejects_foreign_series_and_unregistered_trusts(settings):
    adapter = SecAdapter(settings)
    adapter._get = Mock(return_value=FUND_MAPPING)
    adapter._get_raw = lambda url: (
        atom_feed("NPORT-P") if "browse-edgar" in url else nport("S000000999")
    )
    with pytest.raises(ToolError, match="NPORT_SERIES_MISMATCH"):
        adapter.fund_holdings("SYNE")
    with pytest.raises(ToolError, match="FUND_SERIES_UNAVAILABLE"):
        adapter.fund_holdings("SPY")


ARTICLE = """<html><head><title>Site | Release</title>
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
{"@type":"WebSite","name":"Site"},
{"@type":"NewsArticle","headline":"Issuer reports quarter","datePublished":"2026-02-03T13:30:00Z",
"author":[{"@type":"Person","name":"Reporter"}]}]}</script></head>
<body><nav><a href="/">Home</a><a href="/markets">Markets menu</a></nav>
<div class="cookie-consent">We use cookies to improve your experience. Accept all cookies.</div>
<header><h1>Site header</h1></header>
<div id="content"><div class="story">
<p>The issuer reported quarterly revenue growth driven by data center demand across regions.</p>
<p>Management said gross margin improved on mix while operating expenses rose modestly.</p>
<p>The company kept its annual outlook and announced an expanded buyback authorization.</p>
<table><tr><th>Metric</th><th>Q4</th></tr><tr><td>Revenue</td><td>$123 million</td></tr></table>
<p>Analysts on the call asked about supply constraints and the timing of new capacity.</p>
<p>The company expects the new capacity to begin contributing in the second half of the year.</p>
</div></div>
<aside class="related">Related: other stories you may like to read today and tomorrow.</aside>
<footer>Copyright footer text</footer></body></html>""".encode()


def test_reader_extracts_main_content_json_ld_and_tables(settings):
    transport = Mock()
    transport.public_get.return_value = ("https://news.example.com/a", "text/html", ARTICLE)
    record = DocumentReader(settings, transport).read("TEST", "https://news.example.com/a")
    assert record.payload["extraction_method"] == "html-main-content"
    assert "gross margin improved" in record.text
    assert "Revenue | $123 million" in record.text
    for chrome in ("Markets menu", "cookies", "Copyright footer", "Related:", "Site header"):
        assert chrome not in record.text
    assert record.published_at.isoformat() == "2026-02-03T13:30:00+00:00"
    assert record.payload["title"] == "Issuer reports quarter"
    assert record.payload["author"] == "Reporter"


def test_reader_ignores_future_publication_times(settings):
    transport = Mock()
    transport.public_get.return_value = (
        "https://example.com/x",
        "text/html",
        b'<meta property="article:published_time" content="2999-01-01T00:00:00Z"><p>x</p>',
    )
    record = DocumentReader(settings, transport).read("TEST", "https://example.com/x")
    assert record.published_at is None and any("future" in w for w in record.warnings)


def test_reader_sends_sec_contact_only_to_sec_hosts(settings):
    settings.sec_user_agent = "Research Bot ops@example.org"
    reader = DocumentReader(settings, Mock())
    assert reader._headers("https://www.sec.gov/Archives/x.htm") == {
        "User-Agent": "Research Bot ops@example.org"
    }
    assert reader._headers("https://efts.sec.gov/LATEST/") is not None
    assert reader._headers("https://notsec.gov.example.com/") is None
    assert reader._headers("https://example.com/sec.gov") is None


def test_public_get_applies_per_hop_headers_on_redirect(monkeypatch):
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers.get("User-Agent")))
        if request.url.host == "www.sec.gov":
            return httpx.Response(302, headers={"location": "https://example.com/final"})
        return httpx.Response(200, content=b"ok")

    monkeypatch.setattr("app.adapters.http.public_url", lambda url: url)
    transport = Transport(httpx.Client(transport=httpx.MockTransport(handler)))
    transport.public_get(
        "https://www.sec.gov/doc",
        headers_for=lambda url: {"User-Agent": "contact"} if "sec.gov" in url else None,
    )
    assert seen[0] == ("www.sec.gov", "contact") and seen[1][1] != "contact"


def test_brave_search_freshness_domain_country_language(settings):
    captured = {}

    def handler(request):
        captured.update(request.url.params)
        return httpx.Response(200, json={"web": {"results": []}})

    adapter = BraveSearch(settings, Transport(httpx.Client(transport=httpx.MockTransport(handler))))
    record = adapter.search(
        "TEST",
        "guidance",
        count=5,
        freshness="2026-01-01to2026-03-31",
        domain="investor.example.com",
        country="US",
        search_lang="en",
        offset=2,
    )
    assert captured["q"] == "guidance site:investor.example.com"
    assert captured["freshness"] == "2026-01-01to2026-03-31"
    assert (captured["country"], captured["search_lang"], captured["offset"]) == ("US", "en", "2")
    assert record.payload["options"]["domain"] == "investor.example.com"


def test_search_contract_rejects_malformed_options():
    from pydantic import ValidationError

    from app.tools import SearchInput

    for bad in ({"freshness": "yesterday"}, {"domain": "https://x.com/"}, {"offset": 10}):
        with pytest.raises(ValidationError):
            SearchInput(query="q", **bad)
