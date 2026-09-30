from datetime import timedelta
from decimal import Decimal

import pytest

from app.adapters.fixture import FixtureAdapters
from app.evidence import content_hash
from app.models import EvidenceRecord, NumericFact, PortfolioDecision, ResearchRequest, utcnow
from app.validation import validate_decision


def setup_plan(quantity=None):
    fixture = FixtureAdapters()
    quote = fixture.quote("TEST")
    levels = EvidenceRecord(
        ticker="TEST",
        evidence_type="calculation",
        source_name="test-calculation",
        source_tier=2,
        source_url="fixture://prices",
        content_hash=content_hash("levels"),
        facts=[
            NumericFact(name=n, value=v, unit="USD/share", currency="USD")
            for n, v in [("entry", 100), ("stop", 90), ("target", 120)]
        ],
    )

    def level(value):
        return {
            "value": value,
            "evidence_ids": [levels.evidence_id],
            "basis": "Verified fixture level",
            "classification": "INTERPRETATION",
        }

    decision = PortfolioDecision(
        rating="Buy",
        confidence="높음",
        new_entry_action="ENTER_NOW",
        new_entry_note="검증된 진입 범위 안",
        holder_action="ADD",
        holder_note="추가 매수 검토",
        executive_summary="Verified scenario",
        thesis=[
            {
                "classification": "INTERPRETATION",
                "text": "Verified levels support the setup",
                "evidence_ids": [levels.evidence_id],
                "confidence": "중간",
            }
        ],
        thesis_state="ACTIVE",
        invalidation_conditions=[],
        monitoring_checklist=[],
        material_changes=[],
        material_gaps=[],
        trade_plan={
            "currency": "USD",
            "entry_low": level(100),
            "entry_high": level(100),
            "stop": level(90),
            "targets": [level(120)],
            "conditions": [],
            "invalidation": [],
            "horizon": "medium",
            "quantity": quantity,
            "reward_risk": 2,
            "no_trade_conditions": [],
        },
    )
    return decision, [quote, levels, fixture.portfolio("TEST"), fixture.fees("TEST")]


def test_validated_plan_and_rr():
    decision, records = setup_plan()
    result = validate_decision(decision, records, ResearchRequest(ticker="TEST"), utcnow())
    assert result.valid and result.calculations["reward_risk_0"] == "2"


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda p: setattr(p.stop, "value", Decimal(110)), "GEOMETRY"),
        (lambda p: setattr(p.targets[0], "value", Decimal(95)), "GEOMETRY"),
        (lambda p: setattr(p, "currency", "KRW"), "CURRENCY"),
        (lambda p: setattr(p, "reward_risk", Decimal(99)), "RR_MISMATCH"),
        (lambda p: setattr(p.entry_low, "value", Decimal(99)), "UNSUPPORTED_LEVEL"),
    ],
)
def test_invalid_numeric_plan_is_rejected_without_silent_fix(mutation, code):
    decision, records = setup_plan()
    mutation(decision.trade_plan)
    original = decision.model_dump_json()
    result = validate_decision(decision, records, ResearchRequest(ticker="TEST"), utcnow())
    assert not result.valid and code in [i.code for i in result.issues]
    assert decision.model_dump_json() == original


def test_stale_quote_rejected():
    decision, records = setup_plan()
    assert not validate_decision(
        decision, records, ResearchRequest(ticker="TEST"), utcnow() + timedelta(minutes=5)
    ).valid


def test_quantity_without_risk_rejected():
    decision, records = setup_plan(1)
    result = validate_decision(decision, records, ResearchRequest(ticker="TEST"), utcnow())
    assert "SIZING_INPUTS" in [i.code for i in result.issues]


def test_concentration_and_sector_unknown_are_rejected():
    decision, records = setup_plan(20)
    request = ResearchRequest(
        ticker="TEST",
        risk={
            "portfolio_value": 10000,
            "currency": "USD",
            "max_loss_fraction": ".01",
            "max_position_fraction": ".2",
            "max_sector_fraction": ".3",
            "slippage_fraction": ".001",
            "tax_fraction": 0,
        },
    )
    result = validate_decision(decision, records, request, utcnow())
    assert {"SIZE_LIMIT", "UNKNOWN_SECTOR"}.issubset({i.code for i in result.issues})
