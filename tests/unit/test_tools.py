from decimal import Decimal

import pytest

from app.adapters.fixture import FixtureAdapters
from app.adapters.http import ToolError
from app.tools import ToolRegistry, add_market_tools


def test_peer_research_preserves_actual_security_identity():
    tools = ToolRegistry("TEST")
    add_market_tools(tools, FixtureAdapters())
    record = tools.execute("market_quote", {"ticker": "OTHER"})
    assert record.ticker == "OTHER"


def test_deterministic_calculation_preserves_input_provenance():
    tools = ToolRegistry("TEST")
    quote = FixtureAdapters().quote("TEST")
    tools.records.append(quote)
    record = tools.calculate(
        operation="multiply",
        evidence_id=quote.evidence_id,
        fact_name="last_price",
        assumption=Decimal("1.2"),
        result_name="scenario_target",
    )
    assert record.facts[0].value == 120
    assert record.payload["input_evidence_ids"] == [quote.evidence_id]
    assert record.payload["assumption"] == "1.2"


def test_exact_document_extraction_rejects_invented_numbers():
    tools = ToolRegistry("TEST")
    document = FixtureAdapters().read("TEST", "https://fixture.example/doc")
    document.text = "Revenue was $123 million for the year."
    tools.records.append(document)
    inputs = {
        "evidence_id": document.evidence_id,
        "fact_name": "revenue",
        "quoted_text": document.text,
        "value": 123000000,
        "unit": "USD",
        "scale": 1000000,
    }
    assert tools.extract_fact(**inputs).facts[0].value == 123000000
    inputs["value"] = 124000000
    with pytest.raises(ToolError, match="EXTRACTION_NUMBER_MISMATCH"):
        tools.extract_fact(**inputs)


def test_model_facing_schemas_have_no_regex_lookaround(store, settings):
    """OpenAI strict mode rejects lookaround; pydantic's Decimal pattern contains one."""
    import json
    import re

    from app.engine import ResearchEngine
    from app.models import (
        PortfolioDecision,
        ResearchManagerReview,
        ResearchRequest,
        ResearchSummary,
        StageReview,
        TriageDecision,
    )
    from app.tools import DECIMAL_PATTERN, strict_schema

    responses = (
        ResearchSummary,
        StageReview,
        ResearchManagerReview,
        PortfolioDecision,
        TriageDecision,
    )
    registry = ResearchEngine(settings, store).registry("run", ResearchRequest(ticker="TEST"))
    schemas = [strict_schema(m) for m in responses]
    schemas += [contract["parameters"] for contract in registry.contracts()]
    text = json.dumps(schemas)
    assert not re.search(r"\(\?<?[=!]", text)
    assert json.dumps(DECIMAL_PATTERN)[1:-1] in text  # Decimal strings stay constrained


def test_extraction_tolerates_table_line_breaks_and_facts_search_any_term():
    tools = ToolRegistry("TEST")
    document = FixtureAdapters().read("TEST", "https://fixture.example/doc")
    document.text = "Revenue\n$\n46,743\nmillion for the quarter"
    tools.records.append(document)
    record = tools.extract_fact(
        evidence_id=document.evidence_id,
        fact_name="revenue",
        quoted_text="Revenue $ 46,743 million",
        value=46743000000,
        unit="USD",
        scale=1000000,
    )
    assert record.facts[0].value == 46743000000
    financials = FixtureAdapters().financials("TEST")
    financials.facts = financials.facts + [
        financials.facts[0].model_copy(update={"name": "cash:us-gaap:Cash"}),
        financials.facts[0].model_copy(update={"name": "debt:us-gaap:Debt"}),
    ]
    tools.records.append(financials)
    view = tools.read_evidence(financials.evidence_id, part="facts", query="cash debt")
    assert [f["name"] for f in view["facts"]] == ["cash:us-gaap:Cash", "debt:us-gaap:Debt"]


def test_extraction_reads_scale_from_the_table_header_and_as_written_numbers():
    tools = ToolRegistry("TEST")
    document = FixtureAdapters().read("TEST", "https://fixture.example/10q")
    document.text = (
        "CONDENSED CONSOLIDATED STATEMENTS OF INCOME (In millions, except per share data)\n"
        "Revenue\n$\n96,221\n$\n81,615\nOutlook: revenue is expected to be $108.0 billion."
    )
    tools.records.append(document)
    row = tools.extract_fact(
        evidence_id=document.evidence_id,
        fact_name="revenue",
        quoted_text="Revenue $ 96,221 $ 81,615",
        value=96221,
        unit="USD",
        scale=1000000,
    )
    assert row.facts[0].value == 96221000000 and "in millions" in row.payload["scale_source"]
    outlook = tools.extract_fact(
        evidence_id=document.evidence_id,
        fact_name="revenue_outlook",
        quoted_text="revenue is expected to be $108.0 billion",
        value="108.0",
        unit="USD",
        scale=1000000000,
    )
    assert outlook.facts[0].value == 108000000000 and outlook.payload["scale_source"] == "quote"
    with pytest.raises(ToolError, match="EXTRACTION_SCALE_UNSUPPORTED"):
        tools.extract_fact(
            evidence_id=document.evidence_id,
            fact_name="revenue",
            quoted_text="Revenue $ 96,221",
            value=96221,
            unit="USD",
            scale=1000,
        )


def test_evidence_ids_are_short_and_typed():
    fixture = FixtureAdapters()
    assert fixture.quote("TEST").evidence_id.startswith("qt-")
    assert fixture.financials("TEST").evidence_id.startswith("fin-")
    assert len(fixture.ohlcv("TEST").evidence_id) == len("px-") + 10
