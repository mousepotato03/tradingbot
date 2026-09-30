import re
import time
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from app.adapters.http import ToolError, Transport
from app.config import Settings
from app.evidence import content_hash
from app.models import EvidenceRecord, NumericFact, utcnow

GAAP_TAGS = {
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
# IFRS filers (20-F/40-F/6-K) report in the ifrs-full taxonomy, often in a non-USD currency.
IFRS_TAGS = {
    "revenue": ["Revenue", "RevenueFromContractsWithCustomers"],
    "operating_income": ["ProfitLossFromOperatingActivities"],
    "net_income": ["ProfitLossAttributableToOwnersOfParent", "ProfitLoss"],
    "operating_cash_flow": ["CashFlowsFromUsedInOperatingActivities"],
    "capital_expenditure": [
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
        "PurchaseOfPropertyPlantAndEquipment",
    ],
    "cash": ["CashAndCashEquivalents"],
    "debt": [
        "ShorttermBorrowings",
        "CurrentPortionOfLongtermBorrowings",
        "LongtermBorrowings",
        "CurrentPortionOfNoncurrentBondsIssued",
        "NoncurrentPortionOfNoncurrentBondsIssued",
    ],
    "shares": [],
}
# Collected in full rather than first-match: debt is a sum of separately tagged components.
MULTI_TAG_METRICS = {"debt"}
FINANCIAL_FORMS = {
    "10-K",
    "10-Q",
    "10-K/A",
    "10-Q/A",
    "20-F",
    "20-F/A",
    "40-F",
    "40-F/A",
    "6-K",
    "6-K/A",
}
ATOM = {"a": "http://www.w3.org/2005/Atom"}
NPORT = {"n": "http://www.sec.gov/edgar/nport"}
NEW_YORK = ZoneInfo("America/New_York")


def currency_of(unit: str) -> str | None:
    return unit if re.fullmatch(r"[A-Z]{3}", unit) else None


class SecAdapter:
    def __init__(self, settings: Settings, transport=None):
        self.settings, self.transport = settings, transport or Transport()
        self._mapping = None
        self._funds = None
        self._next = 0

    def _request(self, url):
        time.sleep(max(0, self._next - time.monotonic()))
        self._next = time.monotonic() + 0.25
        return self.transport.request(
            "GET",
            url,
            headers={
                "User-Agent": self.settings.sec_user_agent,
                "Accept-Encoding": "gzip, deflate",
            },
        )

    def _get(self, url):
        return self._request(url).json()

    def _get_raw(self, url) -> bytes:
        return self._request(url).content

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
            if form not in FINANCIAL_FORMS:
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
        facts_by_taxonomy = data.get("facts", {})
        normalized, warnings, bases = [], [], []
        for taxonomy, basis, tag_map in (
            ("us-gaap", "GAAP", GAAP_TAGS),
            ("ifrs-full", "IFRS", IFRS_TAGS),
        ):
            source = facts_by_taxonomy.get(taxonomy)
            if not source:
                continue
            bases.append(basis)
            for name, tags in tag_map.items():
                found = False
                for tag in tags:
                    rows = self._observations(source, tag, name, basis, f"{taxonomy}:{tag}")
                    normalized += rows
                    found = found or bool(rows)
                    if found and name not in MULTI_TAG_METRICS:
                        break
                if name == "shares" and not found:
                    rows = self._observations(
                        facts_by_taxonomy.get("dei", {}),
                        "EntityCommonStockSharesOutstanding",
                        name,
                        None,
                        "dei:EntityCommonStockSharesOutstanding",
                    )
                    normalized += rows
                    found = bool(rows)
                if not found:
                    warnings.append(
                        f"No standard {taxonomy} tag for {name}; inspect official filings/IR"
                    )
        if not bases:
            warnings.append("No us-gaap or ifrs-full facts; custom XBRL requires filing review")
        if len({r["unit"] for r in normalized if currency_of(r["unit"])}) > 1:
            warnings.append(
                "Facts are reported in more than one currency; do not mix units without FX evidence"
            )
        facts = [
            NumericFact(
                name=f"{r['name']}:{r['tag']}:{r['accession']}:{r['end']}:{r['start'] or 'instant'}"
                + (f":{r['unit']}" if r["unit"] not in {"USD", "shares"} else ""),
                value=r["value"],
                unit=r["unit"],
                currency=currency_of(r["unit"]),
                period_start=r["start"],
                period_end=r["end"],
                accounting_basis=r["basis"],
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
            payload={"cik": cik, "observations": normalized, "basis": "+".join(bases) or None},
            warnings=warnings,
            retrieved_at=utcnow(),
        )

    @staticmethod
    def _observations(source, tag, name, basis, label):
        rows = []
        for unit, observations in source.get(tag, {}).get("units", {}).items():
            # Retain actual periods and accession numbers; do not equate YTD with quarters.
            candidates = [r for r in observations if r.get("form") in FINANCIAL_FORMS]
            candidates.sort(key=lambda r: (r["filed"], r["end"]), reverse=True)
            for row in candidates[:6]:
                value = Decimal(str(row["val"]))
                if not value.is_finite():
                    raise ToolError("INVALID_SEC_NUMBER")
                rows.append(
                    {
                        "name": name,
                        "tag": label,
                        "value": str(value),
                        "unit": unit,
                        "start": row.get("start"),
                        "end": row["end"],
                        "filed": row["filed"],
                        "accession": row["accn"],
                        "form": row["form"],
                        "basis": basis,
                    }
                )
        return rows

    def fund_series(self, ticker):
        if self._funds is None:
            data = self._get("https://www.sec.gov/files/company_tickers_mf.json")
            index = {field: i for i, field in enumerate(data["fields"])}
            self._funds = {
                str(row[index["symbol"]]).upper(): (
                    str(row[index["cik"]]).zfill(10),
                    row[index["seriesId"]],
                    row[index["classId"]],
                )
                for row in data["data"]
            }
        if ticker not in self._funds:
            # Unit investment trusts (e.g. SPY) are not registered fund series.
            raise ToolError("FUND_SERIES_UNAVAILABLE")
        return self._funds[ticker]

    def _series_filings(self, series_id, form, count=5):
        url = (
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&CIK={series_id}&type={form}&dateb=&owner=include&count={count}&output=atom"
        )
        try:
            root = ElementTree.fromstring(self._get_raw(url))
        except ElementTree.ParseError:
            raise ToolError("SEC_FEED_INVALID") from None
        rows = []
        for entry in root.findall("a:entry", ATOM):
            content = entry.find("a:content", ATOM)
            if content is None:
                continue

            def text(name, node=content):
                child = node.find(f"a:{name}", ATOM)
                return child.text.strip() if child is not None and child.text else None

            rows.append(
                {
                    "form": text("filing-type"),
                    "filed": text("filing-date"),
                    "accession": text("accession-number"),
                    "index_url": text("filing-href"),
                }
            )
        # `type=` is a prefix filter; keep the exact form and its amendments.
        rows = [r for r in rows if r["form"] in {form, form + "/A"} and r["index_url"]]
        return sorted(rows, key=lambda r: r["filed"] or "", reverse=True)

    def fund_holdings(self, ticker):
        cik, series_id, class_id = self.fund_series(ticker)
        filings = self._series_filings(series_id, "NPORT-P")
        if not filings:
            raise ToolError("FUND_HOLDINGS_UNAVAILABLE")
        latest = filings[0]
        url = latest["index_url"].rsplit("/", 1)[0] + "/primary_doc.xml"
        raw = self._get_raw(url)
        try:
            root = ElementTree.fromstring(raw)
        except ElementTree.ParseError:
            raise ToolError("NPORT_INVALID") from None
        info = root.find("n:formData/n:genInfo", NPORT)
        fund = root.find("n:formData/n:fundInfo", NPORT)
        if info is None or fund is None or info.findtext("n:seriesId", None, NPORT) != series_id:
            raise ToolError("NPORT_SERIES_MISMATCH")

        def number(node, name):
            value = node.findtext(f"n:{name}", None, NPORT)
            try:
                result = Decimal(value) if value is not None else None
            except InvalidOperation:
                raise ToolError("NPORT_INVALID_NUMBER") from None
            if result is not None and not result.is_finite():
                raise ToolError("NPORT_INVALID_NUMBER")
            return result

        holdings = []
        for item in root.findall("n:formData/n:invstOrSecs/n:invstOrSec", NPORT):
            isin = item.find("n:identifiers/n:isin", NPORT)
            holdings.append(
                {
                    "name": item.findtext("n:name", "", NPORT),
                    "title": item.findtext("n:title", "", NPORT),
                    "cusip": item.findtext("n:cusip", "", NPORT),
                    "isin": isin.get("value") if isin is not None else None,
                    "pct": number(item, "pctVal"),
                    "value_usd": number(item, "valUSD"),
                    "asset_category": item.findtext("n:assetCat", None, NPORT),
                    "issuer_category": item.findtext("n:issuerCat", None, NPORT),
                    "country": item.findtext("n:invCountry", None, NPORT),
                    "payoff": item.findtext("n:payoffProfile", None, NPORT),
                }
            )
        net_assets, total_assets = number(fund, "netAssets"), number(fund, "totAssets")
        if net_assets is None or not holdings:
            raise ToolError("FUND_HOLDINGS_UNAVAILABLE")
        ranked = sorted(holdings, key=lambda h: h["pct"] or Decimal(0), reverse=True)
        categories, countries = defaultdict(Decimal), defaultdict(Decimal)
        for holding in holdings:
            categories[holding["asset_category"] or "UNKNOWN"] += holding["pct"] or Decimal(0)
            countries[holding["country"] or "UNKNOWN"] += holding["pct"] or Decimal(0)
        # pctVal is expressed in percent of net assets (0.1008 means 0.1008%).
        facts = [
            NumericFact(name="net_assets", value=net_assets, unit="USD", currency="USD"),
            NumericFact(name="holdings_count", value=Decimal(len(holdings)), unit="count"),
            NumericFact(
                name="top10_weight",
                value=sum((h["pct"] or Decimal(0) for h in ranked[:10]), Decimal(0)),
                unit="percent",
            ),
        ]
        if total_assets is not None:
            facts.append(
                NumericFact(name="total_assets", value=total_assets, unit="USD", currency="USD")
            )
        facts += [
            NumericFact(
                name=f"holding_weight:{h['cusip'] or h['isin'] or h['name']}",
                value=h["pct"],
                unit="percent",
            )
            for h in ranked[:25]
            if h["pct"] is not None
        ]
        facts += [
            NumericFact(name=f"asset_category_weight:{name}", value=value, unit="percent")
            for name, value in sorted(categories.items())
        ]
        facts += [
            NumericFact(name=f"country_weight:{name}", value=value, unit="percent")
            for name, value in sorted(countries.items(), key=lambda item: -item[1])[:15]
        ]
        report_date = info.findtext("n:repPdDate", None, NPORT)
        effective = None
        if report_date:
            effective = datetime.combine(
                date.fromisoformat(report_date), datetime.min.time().replace(hour=16), NEW_YORK
            )
            if effective > utcnow():
                effective = None
        prospectus = next(iter(self._series_filings(series_id, "497K", 3)), None)
        warnings = [
            "N-PORT public holdings lag the report date; confirm current exposures with the "
            "issuer before relying on weights"
        ]
        if prospectus is None:
            warnings.append("No summary prospectus (497K) found for the series")
        return EvidenceRecord(
            ticker=ticker,
            evidence_type="fund_holdings",
            source_name="SEC N-PORT",
            source_tier=1,
            source_url=url,
            effective_at=effective,
            content_hash=content_hash(raw),
            facts=facts,
            payload={
                "cik": cik,
                "series_id": series_id,
                "class_id": class_id,
                "series_name": info.findtext("n:seriesName", None, NPORT),
                "report_date": report_date,
                "fiscal_period_end": info.findtext("n:repPdEnd", None, NPORT),
                "filed": latest["filed"],
                "accession": latest["accession"],
                "holdings": [
                    {k: str(v) if isinstance(v, Decimal) else v for k, v in h.items()}
                    for h in ranked[:50]
                ],
                "summary_prospectus": prospectus,
            },
            warnings=warnings,
        )
