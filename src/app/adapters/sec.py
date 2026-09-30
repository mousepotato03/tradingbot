import time
from decimal import Decimal

from app.adapters.http import ToolError, Transport
from app.config import Settings
from app.evidence import content_hash
from app.models import EvidenceRecord, NumericFact, utcnow

TAGS = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "capital_expenditure": ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "debt": ["LongTermDebtCurrent", "LongTermDebtNoncurrent"],
    "shares": ["CommonStockSharesOutstanding"],
}


class SecAdapter:
    def __init__(self, settings: Settings, transport=None):
        self.settings, self.transport = settings, transport or Transport()
        self._mapping = None
        self._next = 0

    def _get(self, url):
        time.sleep(max(0, self._next - time.monotonic()))
        self._next = time.monotonic() + 0.25
        return self.transport.request(
            "GET",
            url,
            headers={
                "User-Agent": self.settings.sec_user_agent,
                "Accept-Encoding": "gzip, deflate",
            },
        ).json()

    def cik(self, ticker):
        if self._mapping is None:
            data = self._get("https://www.sec.gov/files/company_tickers.json")
            self._mapping = {r["ticker"]: str(r["cik_str"]).zfill(10) for r in data.values()}
        if ticker not in self._mapping:
            raise ToolError("SEC_ISSUER_UNAVAILABLE")
        return self._mapping[ticker]

    def filings(self, ticker):
        cik = self.cik(ticker)
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        data = self._get(url)
        recent = data["filings"]["recent"]
        rows = []
        for i, form in enumerate(recent["form"]):
            if form not in {"10-K", "10-Q", "8-K", "20-F", "6-K", "10-K/A", "10-Q/A"}:
                continue
            document = recent["primaryDocument"][i]
            accession = recent["accessionNumber"][i]
            rows.append(
                {
                    "form": form,
                    "filed": recent["filingDate"][i],
                    "accession": accession,
                    "accepted_at": recent.get("acceptanceDateTime", [None] * len(recent["form"]))[
                        i
                    ],
                    "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{document}",
                }
            )
        return EvidenceRecord(
            ticker=ticker,
            evidence_type="filings",
            source_name="SEC EDGAR",
            source_tier=1,
            source_url=url,
            content_hash=content_hash(data),
            payload={"cik": cik, "company": data["name"], "filings": rows[:50]},
        )

    def financials(self, ticker):
        cik = self.cik(ticker)
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        data = self._get(url)
        gaap, normalized = data.get("facts", {}).get("us-gaap", {}), []
        warnings = []
        for name, tags in TAGS.items():
            found = False
            for tag in tags:
                units = gaap.get(tag, {}).get("units", {})
                for unit, observations in units.items():
                    # Retain actual periods and accession numbers; do not equate YTD with quarters.
                    candidates = [
                        r
                        for r in observations
                        if r.get("form") in {"10-K", "10-Q", "10-K/A", "10-Q/A"}
                    ]
                    candidates.sort(key=lambda r: (r["filed"], r["end"]), reverse=True)
                    for row in candidates[:6]:
                        value = Decimal(str(row["val"]))
                        if not value.is_finite():
                            raise ToolError("INVALID_SEC_NUMBER")
                        normalized.append(
                            {
                                "name": name,
                                "tag": tag,
                                "value": str(value),
                                "unit": unit,
                                "start": row.get("start"),
                                "end": row["end"],
                                "filed": row["filed"],
                                "accession": row["accn"],
                                "form": row["form"],
                            }
                        )
                        found = True
                if found and name != "debt":
                    break
            if not found:
                warnings.append(f"No standard US-GAAP tag for {name}; inspect official filings/IR")
        facts = [
            NumericFact(
                name=f"{r['name']}:{r['tag']}:{r['accession']}:{r['end']}:{r['start'] or 'instant'}",
                value=r["value"],
                unit=r["unit"],
                currency="USD" if r["unit"] == "USD" else None,
                period_start=r["start"],
                period_end=r["end"],
                accounting_basis="GAAP",
            )
            for r in normalized
        ]
        return EvidenceRecord(
            ticker=ticker,
            evidence_type="financials",
            source_name="SEC XBRL",
            source_tier=1,
            source_url=url,
            content_hash=content_hash(data),
            facts=facts,
            payload={"cik": cik, "observations": normalized, "basis": "GAAP"},
            warnings=warnings,
            retrieved_at=utcnow(),
        )
