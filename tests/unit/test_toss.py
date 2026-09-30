from datetime import datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pytest

from app.adapters.http import ToolError
from app.adapters.toss import RateLimiter, TossAdapter
from app.models import utcnow


class FakeClock:
    def __init__(self):
        self.now, self.slept = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class Response:
    def __init__(self, body, headers=None, status=200):
        self.body, self.headers, self.status_code = body, headers or {}, status

    def json(self):
        return self.body


class FakeTransport:
    """Routes Toss paths to canned bodies and records every call."""

    def __init__(self, routes, headers=None):
        self.routes, self.headers, self.calls = routes, headers or {}, []

    def request(self, method, url, **kwargs):
        path = urlsplit(url).path
        self.calls.append((method, path, kwargs.get("params")))
        if path == "/oauth2/token":
            return Response({"access_token": "token", "expires_in": 3600})
        body = self.routes[path]
        body = body(kwargs.get("params")) if callable(body) else body
        return Response({"result": body}, self.headers.get(path))


def adapter(settings, routes, headers=None):
    clock = FakeClock()
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)
    return TossAdapter(settings, FakeTransport(routes, headers), limiter), clock


def test_bucket_is_per_group_and_waits_only_within_a_group():
    clock = FakeClock()
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)
    for _ in range(15):
        limiter.acquire("MARKET_DATA")
    assert clock.slept == []
    limiter.acquire("MARKET_DATA")
    assert clock.slept == [pytest.approx(1 / 15)]
    limiter.acquire("ACCOUNT")  # a different group is not delayed by market data
    assert len(clock.slept) == 1
    limiter.acquire("ACCOUNT")
    assert clock.slept[-1] == pytest.approx(1.0)


def test_headers_update_the_allowance_and_block_after_exhaustion():
    clock = FakeClock()
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)
    limiter.observe("STOCK", {"X-RateLimit-Limit": "2", "X-RateLimit-Remaining": "1"}, 200)
    assert limiter.rate("STOCK") == 2
    limiter.acquire("STOCK")
    limiter.observe("STOCK", {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0.7"}, 200)
    limiter.acquire("STOCK")
    assert sum(clock.slept) >= 0.7
    limiter.observe("RANKING", {"Retry-After": "3"}, 429)
    before = clock.now
    limiter.acquire("RANKING")
    assert clock.now - before >= 3


def test_order_info_uses_the_documented_peak_limit():
    peak = datetime(2026, 9, 30, 9, 5, tzinfo=ZoneInfo("Asia/Seoul"))
    quiet = peak + timedelta(minutes=10)
    assert RateLimiter(wall=lambda: peak).rate("ORDER_INFO") == 3
    assert RateLimiter(wall=lambda: quiet).rate("ORDER_INFO") == 6


def calendar():
    now = utcnow()
    return {
        "today": {
            "date": now.date().isoformat(),
            "regularMarket": {
                "startTime": (now - timedelta(hours=1)).isoformat(),
                "endTime": (now + timedelta(hours=1)).isoformat(),
            },
        }
    }


def test_quotes_batch_200_symbols_per_call_with_one_calendar(settings):
    tickers = [f"T{i:03}" for i in range(450)]

    def prices(params):
        return [
            {
                "symbol": s,
                "currency": "USD",
                "lastPrice": "10",
                "timestamp": utcnow().isoformat(),
            }
            for s in params["symbols"].split(",")
            if s != "T001"
        ]

    toss, _ = adapter(
        settings,
        {"/api/v1/prices": prices, "/api/v1/market-calendar/US": calendar()},
    )
    results = toss.quotes(tickers)
    price_calls = [c for c in toss.transport.calls if c[1] == "/api/v1/prices"]
    assert [len(c[2]["symbols"].split(",")) for c in price_calls] == [200, 200, 50]
    assert sum(c[1] == "/api/v1/market-calendar/US" for c in toss.transport.calls) == 1
    assert isinstance(results["T001"], ToolError)
    assert results["T002"].payload["market_state"] == "REGULAR"


def test_account_snapshot_is_shared_within_cache_window(settings, monkeypatch):
    holdings = {
        "items": [
            {
                "symbol": "AAA",
                "currency": "USD",
                "quantity": "0.5",
                "marketValue": {"amount": "50"},
            }
        ]
    }
    toss, _ = adapter(
        settings,
        {
            "/api/v1/accounts": [{"accountType": "BROKERAGE", "accountSeq": 7}],
            "/api/v1/holdings": holdings,
            "/api/v1/buying-power": {"currency": "USD", "cashBuyingPower": "1000"},
        },
    )
    first = toss.portfolio("AAA")
    toss.account_snapshot()
    assert sum(c[1] == "/api/v1/holdings" for c in toss.transport.calls) == 1
    assert first.payload["holdings"][0]["quantity"] == "0.5"
    moment = __import__("time").monotonic() + settings.account_cache_seconds + 1
    monkeypatch.setattr("app.adapters.toss.time.monotonic", lambda: moment)
    toss.portfolio("AAA")
    assert sum(c[1] == "/api/v1/holdings" for c in toss.transport.calls) == 2


def stock(symbol, kind, leverage=None, status="ACTIVE"):
    return {
        "symbol": symbol,
        "name": symbol,
        "englishName": symbol,
        "market": "NYSE",
        "currency": "USD",
        "status": status,
        "securityType": kind,
        "leverageFactor": leverage,
        "sharesOutstanding": "1000",
    }


@pytest.mark.parametrize(
    "row,expected",
    [
        (stock("VOO", "ETF", "1.0"), "etf"),
        (stock("TSM", "DEPOSITARY_RECEIPT"), "equity"),
        (stock("AAPL", "STOCK"), "equity"),
    ],
)
def test_identity_supports_plain_etfs_and_flags_receipts(settings, row, expected):
    toss, _ = adapter(settings, {"/api/v1/stocks": [row]})
    record = toss.identity(row["symbol"])
    assert record.payload["asset_type"] == expected
    assert record.payload["security_type"] == row["securityType"]
    assert bool(record.warnings) == (row["securityType"] == "DEPOSITARY_RECEIPT")


@pytest.mark.parametrize(
    "row,code",
    [
        (stock("TQQQ", "ETF", "3.0"), "LEVERAGED_FUND_UNSUPPORTED"),
        (stock("SQQQ", "ETF", "-3.0"), "LEVERAGED_FUND_UNSUPPORTED"),
        (stock("XETN", "ETN"), "UNSUPPORTED_SECURITY"),
    ],
)
def test_identity_rejects_leveraged_funds_and_notes(settings, row, code):
    toss, _ = adapter(settings, {"/api/v1/stocks": [row]})
    with pytest.raises(ToolError, match=code):
        toss.identity(row["symbol"])


def test_universe_uses_documented_listing_fields(settings):
    def listing(params):
        if params["market"] != "NYSE":
            return []
        return [
            {"symbol": "KO", "name": "코카콜라", "securityType": "STOCK", "isCommonShare": True},
            {"symbol": "KO.PR", "name": "pref", "securityType": "STOCK", "isCommonShare": False},
            {"symbol": "SPY", "name": "SPDR", "securityType": "ETF", "isCommonShare": True},
            {"symbol": "bad ticker", "name": "x", "securityType": "STOCK", "isCommonShare": True},
            {"symbol": "XW", "name": "warrant", "securityType": "STOCK_WARRANTS"},
        ]

    toss, _ = adapter(settings, {"/api/v1/stocks/all": listing})
    universe = {row["ticker"]: row for row in toss.universe()}
    assert set(universe) == {"KO", "SPY"}
    assert universe["KO"]["exchange"] == "NYSE" and universe["SPY"]["asset_type"] == "etf"


def test_ohlcv_is_stamped_at_the_last_completed_session_close(settings):
    today = utcnow().astimezone(ZoneInfo("America/New_York")).date()

    def candles(params):
        return {
            "candles": [
                {
                    "timestamp": f"{day.isoformat()}T00:00:00-04:00",
                    "currency": "USD",
                    "openPrice": "10",
                    "highPrice": "11",
                    "lowPrice": "9",
                    "closePrice": "10",
                    "volume": "100",
                }
                for day in (today, today - timedelta(days=1), today - timedelta(days=2))
            ],
            "nextBefore": None,
        }

    toss, _ = adapter(settings, {"/api/v1/candles": candles})
    record = toss.ohlcv("TEST", 5)
    assert len(record.payload["candles"]) == 2  # today's bar is still in progress
    close = record.effective_at.astimezone(ZoneInfo("America/New_York"))
    assert close.date() == today - timedelta(days=1) and close.hour == 16
    assert record.stale_after_seconds == settings.ohlcv_max_age_seconds


def test_liquidity_ranking_and_share_details(settings):
    toss, _ = adapter(
        settings,
        {
            "/api/v1/rankings": {"rankings": [{"symbol": "NVDA"}, {"symbol": "AAPL"}]},
            "/api/v1/stocks": lambda p: [stock(s, "STOCK") for s in p["symbols"].split(",")],
        },
    )
    assert toss.liquidity_leaders() == ["NVDA", "AAPL"]
    ranking = next(c for c in toss.transport.calls if c[1] == "/api/v1/rankings")
    assert ranking[2] == {
        "type": "MARKET_TRADING_AMOUNT",
        "marketCountry": "US",
        "duration": "1mo",
        "count": 100,
    }
    details = toss.stock_details([f"S{i}" for i in range(250)])
    assert len(details) == 250 and details["S1"]["shares_outstanding"] == "1000"


def test_token_invalidated_elsewhere_is_renewed_once(settings):
    class Invalidated(FakeTransport):
        failures = 1

        def request(self, method, url, **kwargs):
            if urlsplit(url).path == "/api/v1/prices" and self.failures:
                self.failures -= 1
                raise ToolError("HTTP_401")
            return super().request(method, url, **kwargs)

    clock = FakeClock()
    transport = Invalidated(
        {
            "/api/v1/prices": [
                {
                    "symbol": "T",
                    "currency": "USD",
                    "lastPrice": "1",
                    "timestamp": utcnow().isoformat(),
                }
            ],
            "/api/v1/market-calendar/US": calendar(),
        }
    )
    toss = TossAdapter(settings, transport, RateLimiter(clock=clock, sleep=clock.sleep))
    assert toss.quote("T").payload["price"] == "1"
    assert sum(call[1] == "/oauth2/token" for call in transport.calls) == 2


def test_next_regular_open_skips_to_the_next_business_day(settings):
    now = utcnow()
    today = {
        "startTime": (now - timedelta(hours=2)).isoformat(),
        "endTime": (now + timedelta(hours=4)).isoformat(),
    }
    tomorrow = {
        "startTime": (now + timedelta(hours=22)).isoformat(),
        "endTime": (now + timedelta(hours=28)).isoformat(),
    }
    toss, _ = adapter(
        settings,
        {
            "/api/v1/market-calendar/US": {
                "today": {"date": "d", "regularMarket": today},
                "nextBusinessDay": {"date": "n", "regularMarket": tomorrow},
            }
        },
    )
    assert toss.regular_session_open(now)
    assert toss.next_regular_open(now) == datetime.fromisoformat(tomorrow["startTime"])
    assert toss.next_regular_open(now - timedelta(hours=3)) == datetime.fromisoformat(
        today["startTime"]
    )
