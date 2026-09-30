from decimal import Decimal
from typing import Callable, Literal

from pydantic import BaseModel, Field

from app.adapters.http import ToolError
from app.analytics import technical_snapshot
from app.evidence import content_hash
from app.models import EvidenceRecord, Model, NumericFact, Security


class SymbolInput(Model):
    ticker: str


class CandleInput(SymbolInput):
    count: int = Field(default=300, ge=2, le=1000)


class SearchInput(Model):
    query: str = Field(min_length=1, max_length=1000)
    count: int = Field(default=10, ge=1, le=20)


class UrlInput(Model):
    url: str


class BrowserAction(Model):
    action: Literal["click", "type", "scroll", "screenshot", "back", "tab", "close"]
    ref: str | None = None
    text: str | None = None
    x: int | None = None
    y: int | None = None
    amount: int | None = None


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
    value: Decimal
    unit: Literal["USD", "USD/share", "shares", "ratio", "percent"]
    scale: Literal[1, 1000, 1000000, 1000000000] = 1
    period_start: str | None = None
    period_end: str | None = None
    accounting_basis: Literal["GAAP", "non-GAAP"] | None = None


class EvidenceReadInput(Model):
    evidence_id: str
    query: str | None = None
    start: int = Field(default=0, ge=0)
    characters: int = Field(default=12000, ge=100, le=24000)


def strict_schema(model: type[BaseModel]) -> dict:
    schema = model.model_json_schema()

    def visit(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            node.pop("default", None)
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
        self.images: list[str] = []

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

    def execute(self, name: str, arguments: dict) -> EvidenceRecord:
        if name not in self.definitions:
            raise ToolError("UNKNOWN_TOOL")
        inputs, handler, _ = self.definitions[name]
        data = inputs.model_validate(arguments)
        if isinstance(data, SymbolInput):
            data.ticker = Security.ticker_valid(data.ticker)
        record = handler(**data.model_dump())
        if isinstance(data, SymbolInput) and record.ticker != data.ticker:
            raise ToolError("EVIDENCE_IDENTITY_MISMATCH")
        self.records.append(record)
        return record

    def read_evidence(self, evidence_id, query=None, start=0, characters=12000):
        source = next((r for r in self.records if r.evidence_id == evidence_id), None)
        if source is None:
            raise ToolError("EVIDENCE_UNAVAILABLE")
        if query:
            match = source.text.lower().find(query.lower(), start)
            if match < 0:
                raise ToolError("TEXT_NOT_FOUND")
            start = max(0, match - 500)
        excerpt = source.text[start : start + characters]
        return EvidenceRecord(
            ticker=source.ticker,
            evidence_type="document_excerpt",
            source_name=source.source_name,
            source_tier=source.source_tier,
            source_url=source.source_url,
            published_at=source.published_at,
            content_hash=content_hash(excerpt),
            text=excerpt,
            usable_as_fact=source.usable_as_fact,
            payload={
                "input_evidence_ids": [source.evidence_id],
                "start": start,
                "end": start + len(excerpt),
                "total_characters": len(source.text),
            },
        )

    def calculate(self, **kwargs):
        data = CalculateInput.model_validate(kwargs)

        def fact(evidence_id, name):
            for record in self.records:
                if record.evidence_id == evidence_id and record.usable_as_fact:
                    for item in record.facts:
                        if item.name == name:
                            return item
            raise ToolError("CALCULATION_INPUT_UNAVAILABLE")

        first = fact(data.evidence_id, data.fact_name)
        second = (
            fact(data.other_evidence_id, data.other_fact_name) if data.other_evidence_id else None
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
                elif first.unit == "USD" and second.unit == "shares":
                    unit = "USD/share"
                elif second.unit == "ratio":
                    unit = first.unit
                else:
                    raise ToolError("CALCULATION_UNIT_MISMATCH")
            else:
                unit = first.unit
        elif data.operation == "multiply":
            if second and second.unit != "ratio":
                raise ToolError("CALCULATION_UNIT_MISMATCH")
            value, unit = first.value * other, first.unit
        elif data.operation == "add":
            value, unit = first.value + other, first.unit
        elif data.operation == "subtract":
            value, unit = first.value - other, first.unit
        else:
            if second is None or data.assumption is None:
                raise ToolError("CALCULATION_INPUT_UNAVAILABLE")
            value, unit = first.value + second.value * data.assumption, first.unit
        return EvidenceRecord(
            ticker=self.ticker,
            evidence_type="calculation",
            source_name="Deterministic calculator",
            source_tier=2,
            source_url="calculation://registered-functions/v1",
            content_hash=content_hash(kwargs),
            facts=[NumericFact(name=data.result_name, value=value, unit=unit)],
            payload={
                "operation": data.operation,
                "input_evidence_ids": [data.evidence_id]
                + ([data.other_evidence_id] if second else []),
                "assumption": str(data.assumption) if data.assumption is not None else None,
                "result": str(value),
                "version": "1",
            },
        )

    def extract_fact(self, **kwargs):
        import re

        data = ExtractFactInput.model_validate(kwargs)
        source = next(
            (r for r in self.records if r.evidence_id == data.evidence_id and r.usable_as_fact),
            None,
        )
        if source is None or data.quoted_text not in source.text:
            raise ToolError("EXTRACTION_QUOTE_MISMATCH")
        text = data.quoted_text
        raw_values = re.findall(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?", text)
        if not any(Decimal(v.replace(",", "")) * data.scale == data.value for v in raw_values):
            raise ToolError("EXTRACTION_NUMBER_MISMATCH")
        labels = {1000: "thousand", 1000000: "million", 1000000000: "billion"}
        if data.scale != 1 and labels[data.scale] not in text.lower():
            raise ToolError("EXTRACTION_SCALE_UNSUPPORTED")
        if data.unit.startswith("USD") and not any(
            marker in text.lower() for marker in ["usd", "$", "dollar"]
        ):
            raise ToolError("EXTRACTION_CURRENCY_UNSUPPORTED")
        if data.unit == "percent" and "%" not in text and "percent" not in text.lower():
            raise ToolError("EXTRACTION_UNIT_UNSUPPORTED")
        return EvidenceRecord(
            ticker=self.ticker,
            evidence_type="extracted_fact",
            source_name=source.source_name,
            source_tier=source.source_tier,
            source_url=source.source_url,
            published_at=source.published_at,
            content_hash=content_hash(kwargs),
            text=text,
            facts=[
                NumericFact(
                    name=data.fact_name,
                    value=data.value,
                    unit=data.unit,
                    currency="USD" if data.unit.startswith("USD") else None,
                    period_start=data.period_start,
                    period_end=data.period_end,
                    accounting_basis=data.accounting_basis,
                )
            ],
            payload={
                "input_evidence_ids": [source.evidence_id],
                "extraction_method": "exact-quote-number-match",
                "warnings": ["Label/period/accounting interpretation requires source review."],
            },
        )


def add_market_tools(registry, market):
    registry.add(
        "market_identity",
        SymbolInput,
        market.identity,
        "Verify ticker, exchange, asset and currency.",
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
            value=Decimal(str(value)),
            unit="USD/share" if key in prices else "ratio",
            currency="USD" if key in prices else None,
        )
        for key, value in metrics.items()
        if isinstance(value, (int, float)) and key != "bars"
    ]
    for key in ("recent_low", "recent_high"):
        field = "low" if key == "recent_low" else "high"
        facts.append(
            NumericFact(name=key, value=metrics[key][field], unit="USD/share", currency="USD")
        )
    return EvidenceRecord(
        ticker=registry.ticker,
        evidence_type="technical",
        source_name="Verified OHLCV calculations",
        source_tier=2,
        source_url="calculation://technical/v1",
        content_hash=content_hash(metrics),
        facts=facts,
        payload={
            "metrics": metrics,
            "input_evidence_ids": [source.evidence_id],
            "version": "1",
            "adjusted": source.payload["adjusted"],
        },
    )
