import itertools
from datetime import timedelta
from decimal import Decimal

import pytest

from app.adapters.fixture import FixtureAdapters
from app.evidence import (
    inherit_freshness,
    lineage_fresh,
    quotation_record,
    validate_claims,
)
from app.models import (
    LEGACY_NOTE_PREFIX,
    Claim,
    EvidenceRecord,
    HolderAction,
    NewEntryAction,
    PortfolioDecision,
    Rating,
    ResearchReport,
    ResearchRequest,
    utcnow,
)
from app.tools import ToolRegistry, add_technical_record
from app.validation import validate_consistency, validate_decision

# Policy table written independently of validation.DECISION_MATRIX (docs/INVESTMENT_POLICY.md).
SPEC = {
    "Buy": ({"ENTER_NOW", "CONDITIONAL_ENTRY", "STAGED_ENTRY"}, {"ADD", "HOLD"}, {"ACTIVE"}),
    "Overweight": ({"STAGED_ENTRY", "CONDITIONAL_ENTRY"}, {"ADD", "HOLD"}, {"ACTIVE"}),
    "Hold": ({"WAIT", "CONDITIONAL_ENTRY"}, {"HOLD", "PROTECT_PROFIT"}, {"ACTIVE", "WEAKENED"}),
    "Underweight": ({"AVOID"}, {"TRIM", "PROTECT_PROFIT"}, {"ACTIVE", "WEAKENED"}),
    "Sell": ({"AVOID"}, {"EXIT", "TRIM"}, {"ACTIVE", "WEAKENED", "INVALIDATED"}),
    "판단 보류": ({"DEFER"}, {"DEFER"}, {"ACTIVE", "WEAKENED", "INVALIDATED", "UNKNOWN"}),
}
PLAN_ENTRIES = {"ENTER_NOW", "CONDITIONAL_ENTRY", "STAGED_ENTRY"}


def thesis(evidence_id="ev-1", classification="INTERPRETATION"):
    return Claim(
        classification=classification,
        text="근거 기반 논지",
        evidence_ids=[evidence_id],
        confidence="중간",
    )


def plan_payload(level_id="ev-1"):
    def level(value):
        return {
            "value": value,
            "evidence_ids": [level_id],
            "basis": "fixture",
            "classification": "INTERPRETATION",
        }

    return {
        "currency": "USD",
        "entry_low": level(100),
        "entry_high": level(100),
        "stop": level(90),
        "targets": [level(120)],
        "conditions": [],
        "invalidation": [],
        "horizon": "medium",
        "no_trade_conditions": [],
    }


def decision(rating, new_entry, holder, state="ACTIVE", plan=None, gaps=(), claims=None):
    return PortfolioDecision(
        rating=rating,
        confidence="중간",
        new_entry_action=new_entry,
        new_entry_note="",
        holder_action=holder,
        holder_note="",
        executive_summary="test",
        thesis=[thesis()] if claims is None else claims,
        thesis_state=state,
        invalidation_conditions=[],
        monitoring_checklist=[],
        material_changes=[],
        material_gaps=list(gaps),
        trade_plan=plan,
    )


CELLS = list(
    itertools.product(
        [r.value for r in Rating],
        [a.value for a in NewEntryAction],
        [a.value for a in HolderAction],
        ["ACTIVE", "WEAKENED", "INVALIDATED", "UNKNOWN"],
        [False, True],
    )
)


@pytest.mark.parametrize("rating,new_entry,holder,state,with_plan", CELLS)
def test_every_rating_action_thesis_plan_cell(rating, new_entry, holder, state, with_plan):
    entries, holders, states = SPEC[rating]
    expected = {
        "ACTION_RATING_MISMATCH": new_entry not in entries or holder not in holders,
        "THESIS_RATING_MISMATCH": state not in states,
        "PLAN_ACTION_MISMATCH": with_plan and not (new_entry in PLAN_ENTRIES or holder == "ADD"),
    }
    result = decision(rating, new_entry, holder, state, plan=plan_payload() if with_plan else None)
    codes = {issue.code for issue in validate_consistency(result)}
    assert {code for code, present in expected.items() if present} == codes - {"MISSING_THESIS"}


@pytest.mark.parametrize(
    "rating,new_entry,holder,state",
    [
        ("Sell", "ENTER_NOW", "ADD", "INVALIDATED"),
        ("Buy", "ENTER_NOW", "ADD", "INVALIDATED"),
        ("Hold", "WAIT", "ADD", "ACTIVE"),
    ],
)
def test_contradictions_named_in_review_are_rejected(rating, new_entry, holder, state):
    assert validate_consistency(decision(rating, new_entry, holder, state))


def test_sell_cannot_carry_a_long_entry_plan():
    codes = {
        i.code
        for i in validate_consistency(
            decision("Sell", "AVOID", "EXIT", "INVALIDATED", plan=plan_payload())
        )
    }
    assert "PLAN_ACTION_MISMATCH" in codes


@pytest.mark.parametrize(
    "rating,new_entry,holder,state",
    [
        ("Buy", "ENTER_NOW", "ADD", "ACTIVE"),
        ("Sell", "AVOID", "EXIT", "INVALIDATED"),
        ("Underweight", "AVOID", "TRIM", "WEAKENED"),
    ],
)
def test_every_tradeable_rating_requires_evidence_backed_thesis(rating, new_entry, holder, state):
    assert "MISSING_THESIS" in {
        i.code for i in validate_consistency(decision(rating, new_entry, holder, state, claims=[]))
    }
    assumption_only = [
        Claim(classification="ASSUMPTION", text="가정", evidence_ids=[], confidence="낮음")
    ]
    assert "MISSING_THESIS" in {
        i.code
        for i in validate_consistency(
            decision(rating, new_entry, holder, state, claims=assumption_only)
        )
    }
    assert not validate_consistency(decision(rating, new_entry, holder, state))


def records():
    fixture = FixtureAdapters()
    quote = fixture.quote("TEST")
    levels = EvidenceRecord(
        evidence_id="ev-1",
        ticker="TEST",
        evidence_type="calculation",
        source_name="test",
        source_tier=2,
        source_url="fixture://levels",
        content_hash="levels",
        facts=[
            {"name": n, "value": v, "unit": "USD/share", "currency": "USD"}
            for n, v in [("entry", 100), ("stop", 90), ("target", 120), ("low", 95)]
        ],
    )
    return [quote, levels]


def test_non_blocking_gaps_do_not_force_deferral_but_blocking_gaps_do():
    request = ResearchRequest(ticker="TEST")
    advisory = [{"description": "동종사 비교 자료 부족", "severity": "non_blocking"}]
    assert validate_decision(
        decision("Hold", "WAIT", "HOLD", gaps=advisory), records(), request, utcnow()
    ).valid
    blocking = [{"description": "최근 분기 실적 확인 불가", "severity": "blocking"}]
    result = validate_decision(
        decision("Hold", "WAIT", "HOLD", gaps=blocking), records(), request, utcnow()
    )
    assert "BLOCKING_GAP" in {i.code for i in result.issues}
    result = validate_decision(
        decision("Hold", "WAIT", "HOLD"),
        records(),
        request,
        utcnow(),
        blocking_gaps=["research manager: 공시 충돌 미해결"],
    )
    assert "BLOCKING_GAP" in {i.code for i in result.issues}


def test_deferral_must_name_a_blocking_gap():
    request = ResearchRequest(ticker="TEST")
    unexplained = decision("판단 보류", "DEFER", "DEFER", "UNKNOWN", claims=[])
    assert "UNEXPLAINED_DEFER" in {
        i.code for i in validate_decision(unexplained, records(), request, utcnow()).issues
    }
    explained = decision(
        "판단 보류",
        "DEFER",
        "DEFER",
        "UNKNOWN",
        claims=[],
        gaps=[{"description": "시세 확인 불가", "severity": "blocking"}],
    )
    assert validate_decision(explained, records(), request, utcnow()).valid


def test_immediate_entry_requires_price_inside_range():
    request = ResearchRequest(ticker="TEST")
    plan = plan_payload()
    plan["entry_low"]["value"] = plan["entry_high"]["value"] = 95
    plan["entry_low"]["evidence_ids"] = plan["entry_high"]["evidence_ids"] = ["ev-1"]
    for field in ("entry_low", "entry_high"):
        plan[field]["value"] = 95
    immediate = decision("Buy", "ENTER_NOW", "ADD", plan=plan)
    codes = {i.code for i in validate_decision(immediate, records(), request, utcnow()).issues}
    assert "ENTRY_ACTION_PRICE" in codes
    conditional = decision("Buy", "CONDITIONAL_ENTRY", "HOLD", plan=plan)
    assert validate_decision(conditional, records(), request, utcnow()).valid


def test_legacy_report_shapes_still_load():
    fixture = FixtureAdapters()
    legacy = {
        "run_id": "legacy",
        "ticker": "TEST",
        "as_of": utcnow().isoformat(),
        "created_at": utcnow().isoformat(),
        "fixture": True,
        "security": fixture.identity("TEST").payload | {"asset_type": "equity"},
        "research": {
            "sections": [],
            "unresolved_questions": [],
            "material_gaps": ["old director gap"],
            "evidence_sufficient": False,
        },
        "reviews": {
            "bull": {
                "claims": [],
                "arguments": [],
                "rebuttals": [],
                "early_warnings": [],
                "material_gaps": ["old stage gap"],
            },
            "research_manager": {
                "claims": [],
                "arguments": [],
                "rebuttals": [],
                "early_warnings": [],
                "material_gaps": [],
            },
        },
        "decision": {
            "rating": "판단 보류",
            "confidence": "낮음",
            "new_entry_action": "핵심 근거 확인까지 판단 보류",
            "holder_action": "기존 보유 상태를 확인",
            "executive_summary": "old",
            "thesis": [],
            "thesis_state": "UNKNOWN",
            "invalidation_conditions": [],
            "monitoring_checklist": [],
            "material_changes": [],
            "material_gaps": ["old pm gap"],
            "trade_plan": None,
        },
        "validation": {"valid": True, "issues": [], "calculations": {}},
        "rejected_decisions": [],
        "evidence_ids": [],
        "candidate_state": "RESEARCH_CANDIDATE",
        "previous_report_id": None,
        "tool_calls": 0,
        "limitations": [],
    }
    report = ResearchReport.model_validate(legacy)
    assert report.decision.new_entry_action == NewEntryAction.DEFER
    assert report.decision.new_entry_note.endswith("핵심 근거 확인까지 판단 보류")
    assert report.decision.new_entry_note.startswith(LEGACY_NOTE_PREFIX)
    assert report.decision.material_gaps[0].severity == "blocking"
    assert report.research.material_gaps[0].severity == "non_blocking"
    assert report.reviews["bull"].material_gaps[0].description == "old stage gap"
    # Round-trips through the current schema.
    assert ResearchReport.model_validate(report.model_dump(mode="json")) == report


def document(text):
    record = FixtureAdapters().read("TEST", "https://example.com/release")
    record.text = text
    return record


def test_qualitative_fact_requires_exact_source_span():
    source = document("Management said the new plant began commercial production in the quarter.")
    base = {"classification": "FACT", "evidence_ids": [source.evidence_id], "confidence": "높음"}
    ungrounded = Claim(text="신규 공장이 가동을 시작했다", **base)
    assert [i.code for i in validate_claims([ungrounded], [source], utcnow())] == [
        "UNGROUNDED_FACT"
    ]
    grounded = Claim(
        text="신규 공장이 가동을 시작했다",
        quotes=[
            {
                "evidence_id": source.evidence_id,
                "text": "the new plant began   commercial production",
            }
        ],
        **base,
    )
    assert not validate_claims([grounded], [source], utcnow())

    def with_quote(text):
        return Claim.model_validate(
            grounded.model_dump() | {"quotes": [{"evidence_id": source.evidence_id, "text": text}]}
        )

    invented = with_quote("the new plant was shut down")
    assert "QUOTE_MISMATCH" in {i.code for i in validate_claims([invented], [source], utcnow())}
    short = with_quote("plant")
    assert "QUOTE_MISMATCH" in {i.code for i in validate_claims([short], [source], utcnow())}


def test_structured_record_field_value_can_ground_a_fact():
    identity = FixtureAdapters().identity("TEST")
    claim = Claim(
        classification="FACT",
        text="상장 거래소가 확인됐다",
        evidence_ids=[identity.evidence_id],
        quotes=[{"evidence_id": identity.evidence_id, "text": "TEST"}],
        confidence="높음",
    )
    assert not validate_claims([claim], [identity], utcnow())


def test_verified_span_survives_snippet_retention(store):
    run_id = store.enqueue(ResearchRequest(ticker="TEST"))
    source = document("Preamble. " * 200 + "The regulator approved the merger without conditions.")
    store.add_evidence(run_id, source)
    claim = Claim(
        classification="FACT",
        text="규제기관이 합병을 승인했다",
        evidence_ids=[source.evidence_id],
        quotes=[
            {
                "evidence_id": source.evidence_id,
                "text": "The regulator approved the merger without conditions.",
            }
        ],
        confidence="높음",
    )
    assert not validate_claims([claim], [source], utcnow())
    persisted = store.evidence(run_id)
    assert validate_claims([claim], persisted, utcnow())  # body beyond the retained snippet
    store.add_evidence(run_id, quotation_record(claim.quotes[0], source))
    assert not validate_claims([claim], store.evidence(run_id), utcnow())


def test_derived_evidence_inherits_the_earliest_input_deadline():
    tools = ToolRegistry("TEST")
    quote = FixtureAdapters().quote("TEST")
    tools.records.append(quote)
    derived = tools.calculate(
        operation="multiply",
        evidence_id=quote.evidence_id,
        fact_name="last_price",
        assumption=Decimal("0.97"),
        result_name="entry",
    )
    assert derived.stale_after_seconds == quote.stale_after_seconds
    assert derived.effective_at == quote.effective_at
    ledger = {r.evidence_id: r for r in [quote, derived]}
    assert lineage_fresh(derived, ledger, utcnow())
    assert not lineage_fresh(derived, ledger, utcnow() + timedelta(seconds=120))
    filing = FixtureAdapters().financials("TEST")
    assert inherit_freshness([filing]) == (None, None)


def test_stale_technical_levels_are_rejected_even_after_recalculation():
    fixture = FixtureAdapters()
    ohlcv = fixture.ohlcv("TEST")
    old = ohlcv.model_copy(
        update={"effective_at": utcnow() - timedelta(days=6), "evidence_id": "old-ohlcv"}
    )
    tools = ToolRegistry("TEST")
    tools.records.append(old)
    technical = add_technical_record(tools, old)
    tools.records.append(technical)
    level = tools.calculate(
        operation="multiply",
        evidence_id=technical.evidence_id,
        fact_name="sma50",
        assumption=Decimal(1),
        result_name="entry",
    )
    assert level.stale_after_seconds is not None
    ledger = {r.evidence_id: r for r in [old, technical, level]}
    assert not lineage_fresh(level, ledger, utcnow())
    # A recorded input that is missing from the ledger also fails lineage.
    assert not lineage_fresh(level, {level.evidence_id: level}, utcnow())


def test_level_citing_stale_lineage_is_rejected_by_validator():
    quote, levels = records()
    stale_source = (
        FixtureAdapters()
        .ohlcv("TEST")
        .model_copy(update={"effective_at": utcnow() - timedelta(days=6), "evidence_id": "old"})
    )
    levels = levels.model_copy(
        update={
            "payload": {"input_evidence_ids": ["old"]},
        }
    )
    request = ResearchRequest(ticker="TEST")
    result = validate_decision(
        decision("Buy", "ENTER_NOW", "ADD", plan=plan_payload()),
        [quote, levels, stale_source],
        request,
        utcnow(),
    )
    assert "LEVEL_STALE" in {i.code for i in result.issues}


def test_quantity_step_follows_sizing_unit():
    request = ResearchRequest(
        ticker="TEST",
        risk={
            "portfolio_value": 1000,
            "currency": "USD",
            "max_loss_fraction": ".01",
            "max_position_fraction": ".5",
            "slippage_fraction": 0,
            "tax_fraction": 0,
        },
    )
    fixture = FixtureAdapters()
    base = records() + [fixture.portfolio("TEST"), fixture.fees("TEST")]
    fractional = plan_payload() | {"quantity": "0.5", "sizing_unit": "whole_share"}
    result = validate_decision(
        decision("Buy", "ENTER_NOW", "ADD", plan=fractional), base, request, utcnow()
    )
    assert "QUANTITY_STEP" in {i.code for i in result.issues}
    fractional["sizing_unit"] = "fractional_amount"
    result = validate_decision(
        decision("Buy", "ENTER_NOW", "ADD", plan=fractional), base, request, utcnow()
    )
    assert result.valid, result.issues
    # 1000 * 1% risk over a (100.1 - 89.91) per-share loss, floored to 0.000001 shares.
    assert Decimal(result.calculations["max_quantity"]) == Decimal("0.981354")


def test_structured_evidence_can_be_quoted_as_shown_in_context():
    quote = FixtureAdapters().quote("TEST")
    claim = Claim(
        classification="FACT",
        text="정규장 시세가 확인됐다",
        evidence_ids=[quote.evidence_id],
        quotes=[
            {
                "evidence_id": quote.evidence_id,
                "text": '"currency": "USD", "market_state": "REGULAR"',
            }
        ],
        confidence="높음",
    )
    assert not validate_claims([claim], [quote], utcnow())


def test_numeric_errors_name_the_evidence_that_holds_the_fact():
    fixture = FixtureAdapters()
    quote, ohlcv = fixture.quote("TEST"), fixture.ohlcv("TEST")
    tools = ToolRegistry("TEST")
    tools.records.append(ohlcv)
    technical = add_technical_record(tools, ohlcv)
    upper = next(f for f in technical.facts if f.name == "bb_upper")
    claim = Claim(
        classification="INTERPRETATION",
        text="가격이 볼린저 상단 근처다",
        evidence_ids=[quote.evidence_id],
        numeric_references=[
            {
                "evidence_id": quote.evidence_id,
                "fact_name": "bb_upper",
                "value": upper.value,
                "unit": upper.unit,
            }
        ],
        confidence="중간",
    )
    [issue] = validate_claims([claim], [quote, ohlcv, technical], utcnow())
    assert issue.code == "UNSUPPORTED_NUMBER" and technical.evidence_id in issue.message
    assert any(f.name == "bars" and f.unit == "count" for f in technical.facts)


@pytest.mark.parametrize(
    "text,flagged",
    [
        ("최근 10-Q와 8-K 공시를 확보했다", False),
        ("S&P 500 대비 상대 강세다", False),
        ("매출이 2026년 2분기에 증가했다", True),
        ("20-F/A 정정 공시가 있다", False),
    ],
)
def test_named_identifiers_are_not_unstructured_numbers(text, flagged):
    source = FixtureAdapters().filings("TEST")
    claim = Claim(
        classification="INTERPRETATION",
        text=text,
        evidence_ids=[source.evidence_id],
        confidence="중간",
    )
    codes = {i.code for i in validate_claims([claim], [source], utcnow())}
    assert ("UNSTRUCTURED_NUMBER" in codes) == flagged


def test_evidence_read_finds_keyword_lists_not_only_exact_phrases():
    from app.tools import find_passage

    body = (
        "cover page " * 400
        + "CONSOLIDATED STATEMENTS OF INCOME Revenue 10 Operating income 5 Net income 4"
    )
    position = find_passage(
        body, "CONSOLIDATED STATEMENTS OF INCOME Revenue Net income Operating income"
    )
    assert body[position:].startswith("CONSOLIDATED STATEMENTS OF INCOME")
    assert find_passage(body, "absent phrase qqq") == -1


@pytest.mark.parametrize(
    "text,accepted",
    [
        ('"market_state": "REGULAR", "price": "100"', True),  # any order, number quoting ignored
        ('"price": 100.0', True),
        ('"last_price": "100"', True),  # fact name/value pair
        ("100", True),  # a bare recorded value
        ('"price": "101"', False),
        ('"market_state": "CLOSED"', False),
        ('"invented_key": "100"', False),
    ],
)
def test_structured_quotes_match_recorded_fields_not_formatting(text, accepted):
    quote = FixtureAdapters().quote("TEST")
    claim = Claim(
        classification="FACT",
        text="시세 상태를 확인했다",
        evidence_ids=[quote.evidence_id],
        quotes=[{"evidence_id": quote.evidence_id, "text": text}],
        confidence="높음",
    )
    assert (not validate_claims([claim], [quote], utcnow())) == accepted


def test_references_cite_their_evidence_but_snippets_still_cannot_support_facts():
    fixture = FixtureAdapters()
    quote, search = fixture.quote("TEST"), fixture.search("TEST", "q")
    number = {
        "evidence_id": quote.evidence_id,
        "fact_name": "last_price",
        "value": "100",
        "unit": "USD/share",
    }
    implicit = Claim(
        classification="INTERPRETATION",
        text="가격 수준을 확인했다",
        evidence_ids=[],
        numeric_references=[number],
        confidence="중간",
    )
    assert not validate_claims([implicit], [quote], utcnow())
    snippet_fact = Claim(
        classification="FACT",
        text="검색 결과가 있다",
        evidence_ids=[quote.evidence_id],
        quotes=[
            {"evidence_id": search.evidence_id, "text": "https://fixture.example/issuer-release"}
        ],
        confidence="높음",
    )
    assert "UNSUPPORTED_CLAIM" in {
        i.code for i in validate_claims([snippet_fact], [quote, search], utcnow())
    }


def test_numbers_in_text_may_come_from_a_verified_quote():
    filings = FixtureAdapters().filings("TEST")
    filings.payload["filings"][0]["filed"] = "2026-08-26"
    quote = {"evidence_id": filings.evidence_id, "text": '"filed": "2026-08-26"'}
    base = {"classification": "FACT", "evidence_ids": [filings.evidence_id], "confidence": "높음"}
    covered = Claim(text="최신 공시는 2026년 8월 26일 제출됐다", quotes=[quote], **base)
    assert not validate_claims([covered], [filings], utcnow())
    uncovered = Claim(text="최신 공시는 2026년 9월 1일 제출됐다", quotes=[quote], **base)
    codes = {i.code for i in validate_claims([uncovered], [filings], utcnow())}
    assert codes == {"UNSTRUCTURED_NUMBER"}


@pytest.mark.parametrize(
    "text,accepted",
    [
        ("price: 100", True),
        ("market_state: REGULAR; last_price: 100 USD/share", True),
        ("price: 101", False),
        ("last_price", False),  # a field name alone proves nothing
    ],
)
def test_plain_key_value_quotes_are_checked_against_fields(text, accepted):
    quote = FixtureAdapters().quote("TEST")
    claim = Claim(
        classification="FACT",
        text="시세를 확인했다",
        evidence_ids=[quote.evidence_id],
        quotes=[{"evidence_id": quote.evidence_id, "text": text}],
        confidence="높음",
    )
    assert (not validate_claims([claim], [quote], utcnow())) == accepted


def test_metric_prefix_with_exact_value_identifies_a_long_sec_fact():
    financials = FixtureAdapters().financials("TEST")
    financials.facts = [
        financials.facts[0].model_copy(update={"name": "revenue:us-gaap:Revenues:acc-1:2025-12-31"})
    ]

    def claim(value):
        return Claim(
            classification="INTERPRETATION",
            text="매출 규모를 확인했다",
            evidence_ids=[financials.evidence_id],
            numeric_references=[
                {
                    "evidence_id": financials.evidence_id,
                    "fact_name": "revenue",
                    "value": value,
                    "unit": "USD",
                }
            ],
            confidence="중간",
        )

    assert not validate_claims([claim("100000000")], [financials], utcnow())
    [issue] = validate_claims([claim("100000001")], [financials], utcnow())
    assert issue.code == "UNSUPPORTED_NUMBER" and "recorded value" in issue.message
