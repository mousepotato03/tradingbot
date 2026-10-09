import json
import re
from decimal import Decimal
from typing import Callable, Literal

from pydantic import BaseModel, Field

from app.adapters.http import ToolError
from app.analytics import technical_snapshot
from app.evidence import content_hash, inherit_freshness, normalize_span
from app.models import EvidenceRecord, Model, NumericFact, Security

CURRENCY_UNIT = re.compile(r"[A-Z]{3}")
TECHNICAL_PRECISION = Decimal("0.0001")
# How far before a quoted table row to look for its "in millions" header.
SCALE_CONTEXT = 6000
PER_SHARE_UNIT = re.compile(r"([A-Z]{3})/share")
CURRENCY_MARKERS = {
    "USD": ("$", "dollar"),
    "EUR": ("€", "euro"),
    "GBP": ("£", "pound", "sterling"),
    "JPY": ("¥", "yen"),
    "KRW": ("₩", "won"),
    "TWD": ("nt$", "ntd"),
    "CNY": ("rmb", "yuan", "renminbi"),
}


def unit_currency(unit: str) -> str | None:
    if CURRENCY_UNIT.fullmatch(unit):
        return unit
    match = PER_SHARE_UNIT.fullmatch(unit)
    return match.group(1) if match else None


class SymbolInput(Model):
    ticker: str


class CandleInput(SymbolInput):
    count: int = Field(default=300, ge=2, le=1000)


class SearchInput(Model):
    query: str = Field(min_length=1, max_length=1000)
    count: int = Field(default=10, ge=1, le=20)
    freshness: str | None = Field(
        default=None,
        pattern=r"^(pd|pw|pm|py|\d{4}-\d{2}-\d{2}to\d{4}-\d{2}-\d{2})$",
        description="pd/pw/pm/py or an explicit YYYY-MM-DDtoYYYY-MM-DD publication range",
    )
    domain: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$",
        description="Restrict results to one host, e.g. sec.gov or investor.example.com",
    )
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    search_lang: str | None = Field(default=None, pattern=r"^[a-z]{2}(-[a-z]{2,4})?$")
    offset: int = Field(default=0, ge=0, le=9)
    topic: Literal["general", "news", "finance"] | None = Field(
        default=None, description="Provider topic filter where supported (Tavily)"
    )


class UrlInput(Model):
    url: str


class CalculateInput(Model):
    operation: Literal["ratio", "multiply", "add", "subtract", "atr_offset"]
    evidence_id: str
    fact_name: str
    other_evidence_id: str | None = None
    other_fact_name: str | None = None
    assumption: Decimal | None = None
    result_name: str


class ExtractFactInput(Model):
    evidence_id: str
    fact_name: str
    quoted_text: str = Field(min_length=3, max_length=500)
    value: Decimal = Field(
        description="The number as written in quoted_text without commas (e.g. 96221 or 89.0)"
    )
    unit: str = Field(pattern=r"^([A-Z]{3}(/share)?|shares|ratio|percent)$")
    scale: Literal[1, 1000, 1000000, 1000000000] = Field(
        default=1,
        description="Multiplier stated in the quote or in the table header above it "
        "(thousands/millions/billions); the stored fact is value x scale",
    )
    period_start: str | None = None
    period_end: str | None = None
    accounting_basis: Literal["GAAP", "IFRS", "non-GAAP"] | None = None


class EvidenceReadInput(Model):
    evidence_id: str
    part: Literal["text", "facts", "payload"] = "text"
    query: str | None = None
    start: int = Field(default=0, ge=0)
    characters: int = Field(default=6000, ge=100, le=12000)


# OpenAI strict schemas reject regex lookaround ("regex lookaround is not supported").
LOOKAROUND = re.compile(r"\(\?<?[=!]")
# Pydantic's Decimal string pattern uses a lookahead to refuse sign/dot-only strings; this
# accepts the same strings without one.
DECIMAL_PATTERN = r"^[+-]?0*(\d+\.?\d*|\.\d+)$"
PYDANTIC_DECIMAL_PATTERN = r"^(?!^[-+.]*$)[+-]?0*\d*\.?\d*$"


def find_passage(body: str, query: str, start: int = 0, window: int = 1500) -> int:
    """Offset of the exact phrase, else of the passage holding the most distinct query terms.

    Models often search with keyword lists ("revenue operating income cash"); an exact-phrase
    miss should still land on the relevant section rather than fail.
    """
    lower = body.lower()
    exact = lower.find(query.lower(), start)
    if exact >= 0:
        return exact
    terms = {t for t in re.findall(r"[\w.$%-]{3,}", query.lower())}
    hits = sorted(
        (position, term)
        for term in terms
        for position in (m.start() for m in re.finditer(re.escape(term), lower[start:]))
    )
    best, best_score, left = -1, 0, 0
    for right, (position, _) in enumerate(hits):
        while position - hits[left][0] > window:
            left += 1
        score = len({term for _, term in hits[left : right + 1]})
        if score > best_score:
            best, best_score = hits[left][0], score
    return best + start if best >= 0 else -1


def strict_schema(model: type[BaseModel]) -> dict:
    schema = model.model_json_schema()

    def visit(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            node.pop("default", None)
            pattern = node.get("pattern")
            if isinstance(pattern, str) and LOOKAROUND.search(pattern):
                if pattern != PYDANTIC_DECIMAL_PATTERN:
                    # Pydantic still validates the parsed value; never send an invalid schema.
                    del node["pattern"]
                else:
                    node["pattern"] = DECIMAL_PATTERN
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)
    return schema


class ToolRegistry:
    def __init__(self, ticker: str):
        self.ticker = ticker
        self.definitions: dict[str, tuple[type[Model], Callable, str]] = {}
        self.records: list[EvidenceRecord] = []

    def add(self, name: str, inputs: type[Model], handler: Callable, description: str):
        self.definitions[name] = inputs, handler, description

    def contracts(self) -> list[dict]:
        return [
            {
                "type": "function",
                "name": name,
                "description": description,
                "strict": True,
                "parameters": strict_schema(inputs),
            }
            for name, (inputs, _, description) in self.definitions.items()
        ]

    def execute(self, name: str, arguments: dict) -> EvidenceRecord | dict:
        """Run a tool. New evidence joins the ledger; read-only views of it do not."""
        if name not in self.definitions:
            raise ToolError("UNKNOWN_TOOL")
        inputs, handler, _ = self.definitions[name]
        data = inputs.model_validate(arguments)
        if isinstance(data, SymbolInput):
            data.ticker = Security.ticker_valid(data.ticker)
        record = handler(**data.model_dump())
        if not isinstance(record, EvidenceRecord):
            return record
        if isinstance(data, SymbolInput) and record.ticker != data.ticker:
            raise ToolError("EVIDENCE_IDENTITY_MISMATCH")
        self.records.append(record)
        return record

    def find(self, evidence_id) -> EvidenceRecord:
        source = next((r for r in self.records if r.evidence_id == evidence_id), None)
        if source is None:
            raise ToolError("EVIDENCE_UNAVAILABLE")
        return source

    def read_evidence(self, evidence_id, part="text", query=None, start=0, characters=6000):
        """Read-only view of stored evidence; it creates no new evidence and persists nothing."""
        source = self.find(evidence_id)
        view = {"evidence_id": source.evidence_id, "part": part}
        if part == "facts":
            # Any query term may match a fact name ("revenue net_income cash").
            terms = [t for t in re.split(r"[\s,;]+", (query or "").lower()) if t]
            facts = [
                f.model_dump(mode="json")
                for f in source.facts
                if not terms or any(t in f.name.lower() for t in terms)
            ]
            window = facts[start : start + 50]
            return view | {
                "facts": window,
                "start": start,
                "total_facts": len(facts),
                "next_start": start + len(window) if start + len(window) < len(facts) else None,
            }
        body = (
            source.text
            if part == "text"
            else json.dumps(source.payload, ensure_ascii=False, default=str, sort_keys=True)
        )
        if query:
            match = find_passage(body, query, start)
            if match < 0:
                raise ToolError("TEXT_NOT_FOUND")
            start = max(0, match - 500)
        excerpt = body[start : start + characters]
        end = start + len(excerpt)
        return view | {
            "text": excerpt,
            "start": start,
            "end": end,
            "total_characters": len(body),
            "next_start": end if end < len(body) else None,
            "retention": source.payload.get("retention"),
        }

    def calculate(self, **kwargs):
        data = CalculateInput.model_validate(kwargs)

        def fact(evidence_id, name):
            for record in self.records:
                if record.evidence_id == evidence_id and record.usable_as_fact:
                    for item in record.facts:
                        if item.name == name:
                            return record, item
            raise ToolError("CALCULATION_INPUT_UNAVAILABLE")

        first_record, first = fact(data.evidence_id, data.fact_name)
        second_record, second = (
            fact(data.other_evidence_id, data.other_fact_name)
            if data.other_evidence_id
            else (None, None)
        )
        other = second.value if second else data.assumption
        if other is None:
            raise ToolError("CALCULATION_INPUT_UNAVAILABLE")
        if (
            data.operation in {"add", "subtract", "atr_offset"}
            and second
            and first.unit != second.unit
        ):
            raise ToolError("CALCULATION_UNIT_MISMATCH")
        if data.operation == "ratio":
            if other == 0:
                raise ToolError("ZERO_DENOMINATOR")
            value = first.value / other
            if second:
                if second.unit == first.unit:
                    unit = "ratio"
                elif CURRENCY_UNIT.fullmatch(first.unit) and second.unit == "shares":
                    unit = f"{first.unit}/share"
                elif second.unit == "ratio":
                    unit = first.unit
                else:
                    # Different currencies require an explicit FX fact, not a silent ratio.
                    raise ToolError("CALCULATION_UNIT_MISMATCH")
            else:
                unit = first.unit
        elif data.operation == "multiply":
            unit = first.unit
            if second and second.unit != "ratio":
                # Price per share x share count gives a currency amount (e.g. market cap).
                pair = {first.unit, second.unit}
                per_share = next((u for u in pair if PER_SHARE_UNIT.fullmatch(u)), None)
                if per_share is None or pair != {per_share, "shares"}:
                    raise ToolError("CALCULATION_UNIT_MISMATCH")
                unit = PER_SHARE_UNIT.fullmatch(per_share).group(1)
            value = first.value * other
        elif data.operation == "add":
            value, unit = first.value + other, first.unit
        elif data.operation == "subtract":
            value, unit = first.value - other, first.unit
        else:
            if second is None or data.assumption is None:
                raise ToolError("CALCULATION_INPUT_UNAVAILABLE")
            value, unit = first.value + second.value * data.assumption, first.unit
        inputs = [first_record] + ([second_record] if second_record else [])
        effective_at, stale_after = inherit_freshness(inputs)
        return EvidenceRecord(
            ticker=first_record.ticker,
            evidence_type="calculation",
            source_name="Deterministic calculator",
            source_tier=2,
            source_url="calculation://registered-functions/v1",
            content_hash=content_hash(kwargs),
            effective_at=effective_at,
            stale_after_seconds=stale_after,
            facts=[
                NumericFact(
                    name=data.result_name, value=value, unit=unit, currency=unit_currency(unit)
                )
            ],
            payload={
                "operation": data.operation,
                "input_evidence_ids": [r.evidence_id for r in inputs],
                "assumption": str(data.assumption) if data.assumption is not None else None,
                "result": str(value),
                "version": "2",
            },
        )

    def extract_fact(self, **kwargs):
        data = ExtractFactInput.model_validate(kwargs)
        source = next(
            (r for r in self.records if r.evidence_id == data.evidence_id and r.usable_as_fact),
            None,
        )
        # Table cells extract with line breaks; compare whitespace-normalized text.
        if source is None or normalize_span(data.quoted_text) not in normalize_span(source.text):
            raise ToolError("EXTRACTION_QUOTE_MISMATCH")
        text = data.quoted_text
        raw_values = [
            Decimal(v.replace(",", ""))
            for v in re.findall(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?", text)
        ]
        if data.value in raw_values:
            value = data.value * data.scale  # number as written, times its stated scale
        elif any(raw * data.scale == data.value for raw in raw_values):
            value = data.value  # already scaled by the caller
        else:
            raise ToolError("EXTRACTION_NUMBER_MISMATCH")
        # Tables state units once in the header ("In millions, except per share data").
        body = normalize_span(source.text)
        quote_start = body.index(normalize_span(text))
        preceding = body[max(0, quote_start - SCALE_CONTEXT) : quote_start].lower()
        labels = {1000: "thousand", 1000000: "million", 1000000000: "billion"}
        scale_source = "quote"
        if data.scale != 1 and labels[data.scale] not in text.lower():
            at = preceding.rfind(labels[data.scale])
            if at < 0:
                raise ToolError("EXTRACTION_SCALE_UNSUPPORTED")
            scale_source = preceding[max(0, at - 80) : at + 40].strip()
        currency = unit_currency(data.unit)
        markers = (currency.lower(), *CURRENCY_MARKERS.get(currency, ())) if currency else ()
        if currency and not any(m in text.lower() or m in preceding for m in markers):
            raise ToolError("EXTRACTION_CURRENCY_UNSUPPORTED")
        if data.unit == "percent" and "%" not in text and "percent" not in text.lower():
            raise ToolError("EXTRACTION_UNIT_UNSUPPORTED")
        effective_at, stale_after = inherit_freshness([source])
        return EvidenceRecord(
            ticker=source.ticker,
            evidence_type="extracted_fact",
            source_name=source.source_name,
            source_tier=source.source_tier,
            source_url=source.source_url,
            published_at=source.published_at,
            effective_at=effective_at,
            stale_after_seconds=stale_after,
            content_hash=content_hash(kwargs),
            text=text,
            facts=[
                NumericFact(
                    name=data.fact_name,
                    value=value,
                    unit=data.unit,
                    currency=currency,
                    period_start=data.period_start,
                    period_end=data.period_end,
                    accounting_basis=data.accounting_basis,
                )
            ],
            payload={
                "input_evidence_ids": [source.evidence_id],
                "extraction_method": "exact-quote-number-match",
                "stated_value": str(data.value),
                "scale": data.scale,
                "scale_source": scale_source,
                "warnings": ["Label/period/accounting interpretation requires source review."],
            },
        )


def add_market_tools(registry, market):
    registry.add(
        "market_identity",
        SymbolInput,
        market.identity,
        "Verify ticker, exchange, asset type (equity or ETF) and currency.",
    )
    registry.add(
        "market_quote",
        SymbolInput,
        market.quote,
        "Read timestamped current price and session state.",
    )
    registry.add(
        "market_ohlcv", CandleInput, market.ohlcv, "Read verified completed daily adjusted OHLCV."
    )
    registry.add(
        "portfolio_read",
        SymbolInput,
        market.portfolio,
        "Read actual holdings and cash buying power; not account NAV.",
    )
    registry.add("fees_read", SymbolInput, market.fees, "Read account-specific commission rate.")


def add_technical_record(registry, source: EvidenceRecord) -> EvidenceRecord:
    metrics = technical_snapshot(source.payload["candles"])
    currency = source.payload.get("currency", "USD")
    prices = {
        "ema10",
        "sma50",
        "sma200",
        "atr14",
        "macd",
        "signal",
        "histogram",
        "bb_middle",
        "bb_upper",
        "bb_lower",
    }
    facts = [
        NumericFact(
            name=key,
            # Four decimals remove float noise so references can be copied exactly.
            value=Decimal(value)
            if isinstance(value, int)
            else Decimal(str(value)).quantize(TECHNICAL_PRECISION),
            unit=f"{currency}/share" if key in prices else "count" if key == "bars" else "ratio",
            currency=currency if key in prices else None,
        )
        for key, value in metrics.items()
        if isinstance(value, (int, float))
    ]
    for key in ("recent_low", "recent_high", "low_52w", "high_52w"):
        field = "low" if "low" in key else "high"
        facts.append(
            NumericFact(
                name=key, value=metrics[key][field], unit=f"{currency}/share", currency=currency
            )
        )
    effective_at, stale_after = inherit_freshness([source])
    return EvidenceRecord(
        ticker=source.ticker,
        evidence_type="technical",
        source_name="Verified OHLCV calculations",
        source_tier=2,
        source_url="calculation://technical/v1",
        content_hash=content_hash(metrics),
        effective_at=effective_at,
        stale_after_seconds=stale_after,
        facts=facts,
        payload={
            "metrics": metrics,
            "input_evidence_ids": [source.evidence_id],
            "version": "1",
            "adjusted": source.payload["adjusted"],
        },
    )
