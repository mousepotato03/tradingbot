"""Read-only Toss adapter, informed by legacy bf186fd8 and the official OpenAPI spec."""

import threading
import time
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.adapters.http import ToolError, Transport
from app.config import Settings
from app.evidence import content_hash
from app.models import EvidenceRecord, NumericFact, Security, utcnow


def number(value, positive=False) -> Decimal:
    try:
        result = Decimal(str(value))
    except ArithmeticError:
        raise ToolError("INVALID_NUMBER") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ToolError("INVALID_NUMBER")
    return result


class TossAdapter:
    BASE = "https://openapi.tossinvest.com"
    READ_PATHS = frozenset(
        {
            "/api/v1/accounts",
            "/api/v1/holdings",
            "/api/v1/stocks",
            "/api/v1/stocks/all",
            "/api/v1/prices",
            "/api/v1/candles",
            "/api/v1/buying-power",
            "/api/v1/commissions",
            "/api/v1/market-calendar/US",
            "/api/v1/exchange-rate",
        }
    )

    def __init__(self, settings: Settings, transport: Transport | None = None):
        self.settings, self.transport = settings, transport or Transport()
        self._token, self._expires, self._account = "", 0, None
        self._lock = threading.RLock()
        self._next = 0

    def _send(self, method, path, **kwargs):
        if not (
            (method == "GET" and path in self.READ_PATHS)
            or (method == "POST" and path == "/oauth2/token")
        ):
            raise ToolError("READ_ONLY_VIOLATION")
        with self._lock:
            time.sleep(max(0, self._next - time.monotonic()))
            self._next = time.monotonic() + 1.05
            return self.transport.request(method, self.BASE + path, **kwargs)

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
        if path not in self.READ_PATHS:
            raise ToolError("READ_ONLY_VIOLATION")
        headers = {"Authorization": "Bearer " + self._access_token()}
        if account:
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

    def record(self, ticker, kind, path, payload, facts=None, effective=None, stale=None):
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
        )

    def identity(self, ticker):
        rows = self.get("/api/v1/stocks", {"symbols": ticker})
        row = next((r for r in rows if r["symbol"] == ticker), None)
        if (
            row is None
            or row["currency"] != "USD"
            or row["market"] not in {"NYSE", "NASDAQ", "AMEX", "US_ETC"}
        ):
            raise ToolError("SECURITY_IDENTITY_MISMATCH")
        if row["status"] != "ACTIVE" or row["securityType"] not in {
            "STOCK",
            "FOREIGN_STOCK",
            "DEPOSITARY_RECEIPT",
            "REIT",
        }:
            raise ToolError("UNSUPPORTED_SECURITY")
        security = Security(
            ticker=ticker,
            name=row.get("englishName") or row["name"],
            exchange=row["market"],
            country="US",
            currency="USD",
        )
        return self.record(ticker, "identity", "/api/v1/stocks", security.model_dump(mode="json"))

    def quote(self, ticker):
        rows = self.get("/api/v1/prices", {"symbols": ticker})
        row = next((r for r in rows if r["symbol"] == ticker), None)
        if row is None or row["currency"] != "USD" or not row.get("timestamp"):
            raise ToolError("QUOTE_TIMESTAMP_UNAVAILABLE")
        now = utcnow()
        effective = datetime.fromisoformat(row["timestamp"])
        calendar = self.get(
            "/api/v1/market-calendar/US",
            {"date": now.astimezone(ZoneInfo("America/New_York")).date().isoformat()},
        )
        session = calendar["today"].get("regularMarket")
        regular = session and datetime.fromisoformat(
            session["startTime"]
        ) <= now < datetime.fromisoformat(session["endTime"])
        price = number(row["lastPrice"], positive=True)
        payload = {
            "price": str(price),
            "currency": "USD",
            "market_state": "REGULAR" if regular else "CLOSED",
            "quote_timestamp": effective.isoformat(),
            "calendar": calendar,
        }
        return self.record(
            ticker,
            "quote",
            "/api/v1/prices",
            payload,
            [NumericFact(name="last_price", value=price, unit="USD/share", currency="USD")],
            effective,
            self.settings.quote_max_age_seconds,
        )

    def ohlcv(self, ticker, count=300):
        by_time, before, raw = {}, None, []
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
                # A daily bar is stamped at local midnight, not at completion.
                if stamp.date() >= utcnow().astimezone(ZoneInfo("America/New_York")).date():
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
        return self.record(
            ticker,
            "ohlcv",
            "/api/v1/candles",
            {"currency": "USD", "adjusted": True, "candles": candles},
        )

    def portfolio(self, ticker):
        rows = self.get("/api/v1/holdings", account=True)
        cash = self.get("/api/v1/buying-power", {"currency": "USD"}, account=True)
        if cash["currency"] != "USD":
            raise ToolError("CURRENCY_MISMATCH")
        holdings = [
            {
                "ticker": r["symbol"],
                "currency": r["currency"],
                "quantity": str(number(r["quantity"])),
                "market_value": str(number(r["marketValue"]["amount"])),
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
            effective=utcnow(),
            stale=self.settings.portfolio_max_age_seconds,
        )

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
        unique = {}
        for exchange in ("NASDAQ", "NYSE", "AMEX"):
            rows = self.get("/api/v1/stocks/all", {"market": exchange, "status": "ACTIVE"})
            if not isinstance(rows, list):
                raise ToolError("INVALID_UNIVERSE_RESPONSE")
            for row in rows:
                if row["currency"] == "USD" and row["securityType"] in {
                    "STOCK",
                    "FOREIGN_STOCK",
                    "DEPOSITARY_RECEIPT",
                    "REIT",
                }:
                    ticker = Security.ticker_valid(row["symbol"])
                    unique[ticker] = {
                        "ticker": ticker,
                        "name": row.get("englishName") or row["name"],
                        "exchange": row["market"],
                        "country": "US",
                        "currency": "USD",
                        "asset_type": "equity",
                    }
        return [unique[t] for t in sorted(unique)]
