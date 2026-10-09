"""Read-only Toss adapter, informed by legacy bf186fd8 and the official OpenAPI spec."""

import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from app.adapters.http import ToolError, Transport
from app.config import Settings
from app.evidence import content_hash
from app.models import EvidenceRecord, NumericFact, Security, utcnow

NEW_YORK = ZoneInfo("America/New_York")
SEOUL = ZoneInfo("Asia/Seoul")
# Rate-limit group per endpoint, from each operation's "Rate Limits Group" in the OpenAPI spec.
PATH_GROUPS = {
    "/oauth2/token": "AUTH",
    "/api/v1/accounts": "ACCOUNT",
    "/api/v1/holdings": "ASSET",
    "/api/v1/stocks": "STOCK",
    "/api/v1/stocks/all": "STOCK_ALL",
    "/api/v1/prices": "MARKET_DATA",
    "/api/v1/candles": "MARKET_DATA_CHART",
    "/api/v1/buying-power": "ORDER_INFO",
    "/api/v1/commissions": "ORDER_INFO",
    "/api/v1/market-calendar/US": "MARKET_INFO",
    "/api/v1/exchange-rate": "MARKET_INFO",
    "/api/v1/rankings": "RANKING",
}
# Documented requests/second per client and group. Toss may change them without notice, so these
# only seed the limiter until X-RateLimit-Limit reports the current allowance.
DEFAULT_RATES = {
    "AUTH": 5.0,
    "ACCOUNT": 1.0,
    "ASSET": 5.0,
    "STOCK": 5.0,
    "STOCK_ALL": 1.0,
    "MARKET_DATA": 15.0,
    "MARKET_DATA_CHART": 20.0,
    "ORDER_INFO": 6.0,
    "MARKET_INFO": 3.0,
    "RANKING": 5.0,
}
PEAK_RATES = {"ORDER_INFO": 3.0}  # 09:00-09:10 KST
EQUITY_TYPES = {"STOCK", "FOREIGN_STOCK", "DEPOSITARY_RECEIPT", "REIT"}
FUND_TYPES = {"ETF", "FOREIGN_ETF"}
US_MARKETS = {"NYSE", "NASDAQ", "AMEX", "US_ETC"}
PRICE_BATCH = 200


def number(value, positive=False) -> Decimal:
    try:
        result = Decimal(str(value))
    except ArithmeticError:
        raise ToolError("INVALID_NUMBER") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ToolError("INVALID_NUMBER")
    return result


def _header_number(headers, name):
    try:
        value = float(headers.get(name))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def portfolio_facts(payload) -> list[NumericFact]:
    """Account numbers as referenceable facts: buying power and each holding's position."""
    facts = [
        NumericFact(
            name="buying_power",
            value=Decimal(payload["buying_power"]),
            unit=payload["currency"],
            currency=payload["currency"],
        )
    ]
    for holding in payload["holdings"]:
        currency = holding["currency"]
        for field, unit in (
            ("quantity", "shares"),
            ("average_price", f"{currency}/share"),
            ("market_value", currency),
        ):
            if holding.get(field) is not None:
                facts.append(
                    NumericFact(
                        name=f"{holding['ticker']}.{field}",
                        value=Decimal(holding[field]),
                        unit=unit,
                        currency=None if unit == "shares" else currency,
                    )
                )
    return facts


class RateLimiter:
    """Token bucket per Toss rate-limit group, updated from response headers."""

    def __init__(self, clock=time.monotonic, sleep=None, wall=utcnow):
        self.clock, self._sleep, self.wall = clock, sleep, wall
        self.limits: dict[str, float] = {}
        self.buckets: dict[str, dict] = {}
        self.lock = threading.Lock()

    def rate(self, group: str) -> float:
        rate = self.limits.get(group, DEFAULT_RATES.get(group, 1.0))
        local = self.wall().astimezone(SEOUL)
        if group in PEAK_RATES and local.hour == 9 and local.minute < 10:
            rate = min(rate, PEAK_RATES[group])
        return max(rate, 0.1)

    def _bucket(self, group, now):
        rate = self.rate(group)
        bucket = self.buckets.setdefault(
            group, {"tokens": rate, "updated": now, "blocked_until": 0.0}
        )
        bucket["tokens"] = min(rate, bucket["tokens"] + (now - bucket["updated"]) * rate)
        bucket["updated"] = now
        return bucket, rate

    def acquire(self, group: str):
        while True:
            with self.lock:
                now = self.clock()
                bucket, rate = self._bucket(group, now)
                wait = bucket["blocked_until"] - now
                if wait <= 0 and bucket["tokens"] >= 1:
                    bucket["tokens"] -= 1
                    return
                if wait <= 0:
                    wait = (1 - bucket["tokens"]) / rate
            (self._sleep or time.sleep)(wait)

    def observe(self, group: str, headers, status: int):
        limit = _header_number(headers, "X-RateLimit-Limit")
        remaining = _header_number(headers, "X-RateLimit-Remaining")
        reset = _header_number(headers, "X-RateLimit-Reset")
        retry = _header_number(headers, "Retry-After")
        with self.lock:
            if limit:
                self.limits[group] = limit
            now = self.clock()
            bucket, _ = self._bucket(group, now)
            if status == 429 or remaining == 0:
                bucket["tokens"] = 0.0
                bucket["blocked_until"] = now + min(30.0, retry or reset or 1.0)
            elif remaining is not None:
                # The server's bucket is authoritative when it has fewer tokens than ours.
                bucket["tokens"] = min(bucket["tokens"], remaining)


class TossAdapter:
    BASE = "https://openapi.tossinvest.com"
    READ_PATHS = frozenset(PATH_GROUPS) - {"/oauth2/token"}

    def __init__(self, settings: Settings, transport: Transport | None = None, limiter=None):
        self.settings, self.transport = settings, transport or Transport()
        self.limiter = limiter or RateLimiter()
        self._token, self._expires, self._account = "", 0, None
        # Serializes token issuance: a new token invalidates the previous one for this client.
        self._lock = threading.RLock()
        self._account_lock = threading.Lock()
        self._account_cache = None
        self._calendar_cache = {}

    def _send(self, method, path, **kwargs):
        if not (
            (method == "GET" and path in self.READ_PATHS)
            or (method == "POST" and path == "/oauth2/token")
        ):
            raise ToolError("READ_ONLY_VIOLATION")
        group = PATH_GROUPS[path]
        self.limiter.acquire(group)
        response = self.transport.request(method, self.BASE + path, **kwargs)
        self.limiter.observe(group, response.headers, response.status_code)
        return response

    def _access_token(self):
        with self._lock:
            if not self._token or time.monotonic() >= self._expires:
                response = self._send(
                    "POST",
                    "/oauth2/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.settings.toss_client_id.get_secret_value(),
                        "client_secret": self.settings.toss_client_secret.get_secret_value(),
                    },
                ).json()
                token, seconds = response["access_token"], int(response["expires_in"])
                if not isinstance(token, str) or not token or seconds <= 0:
                    raise ToolError("INVALID_AUTH_RESPONSE")
                self._token, self._expires = token, time.monotonic() + max(1, seconds - 60)
            return self._token

    def get(self, path, params=None, account=False):
        try:
            return self._get(path, params, account)
        except ToolError as error:
            if error.code != "HTTP_401":
                raise
            # A token issued elsewhere with the same client invalidates ours; renew once.
            with self._lock:
                self._token = ""
            return self._get(path, params, account)

    def _get(self, path, params=None, account=False):
        if path not in self.READ_PATHS:
            raise ToolError("READ_ONLY_VIOLATION")
        headers = {"Authorization": "Bearer " + self._access_token()}
        if account:
            with self._lock:
                if self._account is None:
                    rows = self.get("/api/v1/accounts")
                    accounts = [a for a in rows if a["accountType"] == "BROKERAGE"]
                    selected = self.settings.toss_account_seq.get_secret_value()
                    if selected:
                        accounts = [a for a in accounts if str(a["accountSeq"]) == selected]
                    if len(accounts) != 1:
                        raise ToolError("ACCOUNT_SELECTION_REQUIRED")
                    self._account = str(accounts[0]["accountSeq"])
            headers["X-Tossinvest-Account"] = self._account
        try:
            return self._send("GET", path, headers=headers, params=params).json()["result"]
        except ToolError as error:
            if error.code == "HTTP_401":
                self._token = ""
            raise
        except (KeyError, TypeError, ValueError):
            raise ToolError("INVALID_TOSS_RESPONSE") from None

    def record(
        self, ticker, kind, path, payload, facts=None, effective=None, stale=None, warnings=None
    ):
        return EvidenceRecord(
            ticker=ticker,
            evidence_type=kind,
            source_name="Toss Securities",
            source_tier=2,
            source_url=self.BASE + path,
            content_hash=content_hash(payload),
            effective_at=effective,
            payload=payload,
            facts=facts or [],
            stale_after_seconds=stale,
            warnings=warnings or [],
        )

    def identity(self, ticker):
        rows = self.get("/api/v1/stocks", {"symbols": ticker})
        row = next((r for r in rows if r["symbol"] == ticker), None)
        if row is None or row["currency"] != "USD" or row["market"] not in US_MARKETS:
            raise ToolError("SECURITY_IDENTITY_MISMATCH")
        kind = row["securityType"]
        if row["status"] != "ACTIVE" or kind not in EQUITY_TYPES | FUND_TYPES:
            raise ToolError("UNSUPPORTED_SECURITY")
        warnings = []
        if kind in FUND_TYPES:
            leverage = row.get("leverageFactor")
            try:
                leveraged = leverage is not None and Decimal(str(leverage)) != 1
            except InvalidOperation:
                raise ToolError("INVALID_NUMBER") from None
            if leveraged:
                # Daily-reset leveraged/inverse funds need path-dependent analysis not built here.
                raise ToolError("LEVERAGED_FUND_UNSUPPORTED")
        if kind == "DEPOSITARY_RECEIPT":
            warnings.append(
                "Depositary receipt: per-share comparisons with issuer financials need the ADS "
                "ratio from the deposit agreement or annual report"
            )
        security = Security(
            ticker=ticker,
            name=row.get("englishName") or row["name"],
            exchange=row["market"],
            country="US",
            currency="USD",
            asset_type="etf" if kind in FUND_TYPES else "equity",
            security_type=kind,
        )
        facts = []
        if row.get("sharesOutstanding") not in (None, "") and kind not in FUND_TYPES:
            facts.append(
                NumericFact(
                    name="shares_outstanding",
                    value=number(row["sharesOutstanding"], positive=True),
                    unit="shares",
                )
            )
        return self.record(
            ticker,
            "identity",
            "/api/v1/stocks",
            security.model_dump(mode="json"),
            facts,
            warnings=warnings,
        )

    def _calendar(self, at=None):
        """US session calendar around the NY date of `at` (default now), cached briefly."""
        day = (at or utcnow()).astimezone(NEW_YORK).date().isoformat()
        cached = self._calendar_cache.get(day)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        calendar = self.get("/api/v1/market-calendar/US", {"date": day})
        self._calendar_cache = {
            key: value
            for key, value in self._calendar_cache.items()
            if time.monotonic() - value[0] < 300
        }
        self._calendar_cache[day] = (time.monotonic(), calendar)
        return calendar

    def next_regular_open(self, after):
        """Start of the next US regular session strictly after `after` (trading days only)."""
        calendar = self._calendar(after)
        for day in ("today", "nextBusinessDay"):
            session = (calendar.get(day) or {}).get("regularMarket")
            if session:
                start = datetime.fromisoformat(session["startTime"])
                if start > after:
                    return start
        return None

    def regular_session_open(self, at):
        session = (self._calendar().get("today") or {}).get("regularMarket")
        return bool(session) and (
            datetime.fromisoformat(session["startTime"])
            <= at
            < datetime.fromisoformat(session["endTime"])
        )

    @staticmethod
    def _sessions(calendar):
        """(label, start, end) for every listed session of the three calendar days."""
        labels = {
            "regularMarket": "REGULAR",
            "dayMarket": "DAY",
            "preMarket": "PRE",
            "afterMarket": "AFTER",
        }
        for day in ("previousBusinessDay", "today", "nextBusinessDay"):
            for key, label in labels.items():
                session = (calendar.get(day) or {}).get(key)
                if session:
                    yield (
                        label,
                        datetime.fromisoformat(session["startTime"]),
                        datetime.fromisoformat(session["endTime"]),
                    )

    def _completed_session_date(self):
        """NY date of the newest final daily bar: today's once its regular and after sessions end."""
        now = utcnow()
        today = now.astimezone(NEW_YORK).date()
        try:
            day = self._calendar().get("today") or {}
            ends = [
                datetime.fromisoformat(session["endTime"])
                for session in (day.get("regularMarket"), day.get("afterMarket"))
                if session
            ]
        except (ToolError, KeyError, TypeError, ValueError):
            ends = []
        return today if ends and now >= max(ends) else today - timedelta(days=1)

    def quotes(self, tickers):
        """One /prices call per 200 symbols; returns {ticker: EvidenceRecord | ToolError}."""
        tickers = list(dict.fromkeys(tickers))
        results = {}
        for start in range(0, len(tickers), PRICE_BATCH):
            chunk = tickers[start : start + PRICE_BATCH]
            rows = self.get("/api/v1/prices", {"symbols": ",".join(chunk)})
            by_symbol = {row["symbol"]: row for row in rows}
            for ticker in chunk:
                try:
                    results[ticker] = self._quote_record(ticker, by_symbol.get(ticker))
                except ToolError as error:
                    results[ticker] = error
        return results

    def quote(self, ticker):
        result = self.quotes([ticker])[ticker]
        if isinstance(result, Exception):
            raise result
        return result

    def _quote_record(self, ticker, row):
        if row is None or row["currency"] != "USD" or not row.get("timestamp"):
            raise ToolError("QUOTE_TIMESTAMP_UNAVAILABLE")
        effective = datetime.fromisoformat(row["timestamp"])
        price = number(row["lastPrice"], positive=True)
        calendar = self._calendar()
        now = utcnow()
        session = calendar["today"].get("regularMarket")
        regular = session and datetime.fromisoformat(
            session["startTime"]
        ) <= now < datetime.fromisoformat(session["endTime"])
        sessions = list(self._sessions(calendar))
        stale = self.settings.quote_max_age_seconds
        if not regular:
            # Between regular sessions the last observed price stays the reference until the next
            # open, provided it was current when the last session closed. Unknown bounds keep 90 s.
            closes = [end for label, _, end in sessions if label == "REGULAR" and end <= now]
            opens = [start for label, start, _ in sessions if label == "REGULAR" and start > now]
            if (
                closes
                and opens
                and effective >= max(closes) - timedelta(seconds=stale)
                and min(opens) > effective
            ):
                stale = max(stale, int((min(opens) - effective).total_seconds()))
        payload = {
            "price": str(price),
            "currency": "USD",
            "market_state": "REGULAR" if regular else "CLOSED",
            # Extended sessions (DAY/PRE/AFTER) trade thinly; levels are judged on REGULAR only.
            "session": next(
                (label for label, start, end in sessions if start <= now < end), "CLOSED"
            ),
            "quote_timestamp": effective.isoformat(),
            "valid_until": (effective + timedelta(seconds=stale)).isoformat(),
            "calendar": calendar,
        }
        return self.record(
            ticker,
            "quote",
            "/api/v1/prices",
            payload,
            [NumericFact(name="last_price", value=price, unit="USD/share", currency="USD")],
            effective,
            stale,
        )

    def ohlcv(self, ticker, count=300):
        by_time, before, raw = {}, None, []
        today, completed = utcnow().astimezone(NEW_YORK).date(), None
        for _ in range(10):
            params = {
                "symbol": ticker,
                "interval": "1d",
                "count": min(200, count),
                "adjusted": "true",
            }
            if before:
                params["before"] = before
            result = self.get("/api/v1/candles", params)
            raw.append(result)
            for row in result["candles"]:
                if row["currency"] != "USD":
                    raise ToolError("CURRENCY_MISMATCH")
                stamp = datetime.fromisoformat(row["timestamp"])
                # A daily bar is stamped at local midnight, not at completion; today's bar is
                # final only after the session ends.
                if stamp.date() >= today:
                    completed = completed or self._completed_session_date()
                    if stamp.date() > completed:
                        continue
                bar = {
                    name: str(number(row[field], positive=name != "volume"))
                    for name, field in {
                        "open": "openPrice",
                        "high": "highPrice",
                        "low": "lowPrice",
                        "close": "closePrice",
                        "volume": "volume",
                    }.items()
                }
                if (
                    not Decimal(bar["low"])
                    <= min(Decimal(bar["open"]), Decimal(bar["close"]))
                    <= max(Decimal(bar["open"]), Decimal(bar["close"]))
                    <= Decimal(bar["high"])
                ):
                    raise ToolError("INVALID_OHLCV")
                bar["timestamp"] = stamp.isoformat()
                by_time[stamp] = bar
            if len(by_time) >= count or not result.get("nextBefore"):
                break
            cursor = result["nextBefore"]
            if cursor == before:
                raise ToolError("PAGINATION_LOOP")
            before = cursor
        candles = [by_time[t] for t in sorted(by_time)][-count:]
        effective = None
        if candles:
            # Observation time of the latest completed session: its regular close. An early
            # close (13:00) ends before the usual 16:00 stamp, which must not lie in the future.
            session = datetime.fromisoformat(candles[-1]["timestamp"]).date()
            effective = min(
                datetime.combine(session, datetime.min.time(), NEW_YORK) + timedelta(hours=16),
                utcnow(),
            )
        return self.record(
            ticker,
            "ohlcv",
            "/api/v1/candles",
            {"currency": "USD", "adjusted": True, "candles": candles},
            effective=effective,
            stale=self.settings.ohlcv_max_age_seconds,
        )

    def _account_state(self):
        """Holdings and buying power, shared across callers for account_cache_seconds."""
        with self._account_lock:
            cached = self._account_cache
            if cached and time.monotonic() - cached[0] < self.settings.account_cache_seconds:
                return cached[1]
            rows = self.get("/api/v1/holdings", account=True)
            cash = self.get("/api/v1/buying-power", {"currency": "USD"}, account=True)
            state = (rows, cash, utcnow())
            self._account_cache = (time.monotonic(), state)
            return state

    def portfolio(self, ticker):
        rows, cash, fetched = self._account_state()
        if cash["currency"] != "USD":
            raise ToolError("CURRENCY_MISMATCH")
        holdings = [
            {
                "ticker": r["symbol"],
                "currency": r["currency"],
                "quantity": str(number(r["quantity"])),
                "market_value": str(number(r["marketValue"]["amount"])),
                "average_price": str(number(r["averagePurchasePrice"]))
                if r.get("averagePurchasePrice") is not None
                else None,
                "sector": None,
            }
            for r in rows["items"]
        ]
        payload = {
            "holdings": holdings,
            "holdings_complete": True,
            "complete": all(h["currency"] == "USD" for h in holdings),
            "currency": "USD",
            "buying_power": str(number(cash["cashBuyingPower"])),
            "portfolio_value": None,
            "security_sector": None,
            "warnings": [
                "Buying power is not net asset value; risk portfolio_value must be supplied."
            ],
        }
        return self.record(
            ticker,
            "portfolio",
            "/api/v1/holdings",
            payload,
            portfolio_facts(payload),
            effective=fetched,
            stale=self.settings.portfolio_max_age_seconds,
        )

    def account_snapshot(self):
        return self.portfolio("ACCOUNT")

    def fees(self, ticker):
        rows = self.get("/api/v1/commissions", account=True)
        matches = [r for r in rows if r["marketCountry"] == "US"]
        if len(matches) != 1:
            raise ToolError("FEES_UNAVAILABLE")
        return self.record(
            ticker,
            "fees",
            "/api/v1/commissions",
            {"rate": str(number(matches[0]["commissionRate"]))},
            effective=utcnow(),
            stale=86400,
        )

    def universe(self):
        """Tradable US listings. /stocks/all carries no currency: the market query defines it."""
        unique = {}
        for exchange in ("NASDAQ", "NYSE", "AMEX"):
            rows = self.get("/api/v1/stocks/all", {"market": exchange, "status": "ACTIVE"})
            if not isinstance(rows, list):
                raise ToolError("INVALID_UNIVERSE_RESPONSE")
            for row in rows:
                kind = row.get("securityType")
                if kind not in EQUITY_TYPES | FUND_TYPES or row.get("isCommonShare") is False:
                    continue
                try:
                    ticker = Security.ticker_valid(row["symbol"])
                except ValueError:
                    continue
                unique[ticker] = {
                    "ticker": ticker,
                    "name": row["name"],
                    "exchange": exchange,
                    "country": "US",
                    "currency": "USD",
                    "asset_type": "etf" if kind in FUND_TYPES else "equity",
                    "security_type": kind,
                }
        return [unique[t] for t in sorted(unique)]

    def liquidity_leaders(self, count=100):
        """US market trading-amount ranking over one month (liquidity screen input)."""
        result = self.get(
            "/api/v1/rankings",
            {
                "type": "MARKET_TRADING_AMOUNT",
                "marketCountry": "US",
                "duration": "1mo",
                "count": min(100, count),
            },
        )
        return [row["symbol"] for row in result.get("rankings", [])]

    def stock_details(self, tickers):
        """Shares outstanding and listing status for up to 200 symbols per call."""
        details = {}
        tickers = list(dict.fromkeys(tickers))
        for start in range(0, len(tickers), PRICE_BATCH):
            chunk = tickers[start : start + PRICE_BATCH]
            for row in self.get("/api/v1/stocks", {"symbols": ",".join(chunk)}):
                details[row["symbol"]] = {
                    "shares_outstanding": row.get("sharesOutstanding"),
                    "currency": row.get("currency"),
                    "market": row.get("market"),
                    "status": row.get("status"),
                    "security_type": row.get("securityType"),
                    "leverage_factor": row.get("leverageFactor"),
                }
        return details
