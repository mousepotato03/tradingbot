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
