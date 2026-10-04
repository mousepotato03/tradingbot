from decimal import Decimal

import pytest
from sqlalchemy import select

from app.engine import ResearchEngine
from app.lifecycle import changes
from app.llm import FixtureModel
from app.models import Claim, EvidenceRecord, ResearchRequest
from app.monitoring import signal_label
from app.storage import OutboxRow


def alerts_for(store, run_id):
    with store.transaction() as session:
        return [r.body for r in session.scalars(select(OutboxRow)) if r.body["run_id"] == run_id]


def test_a_report_sends_one_alert_listing_every_change(store, settings):
    ResearchEngine(settings, store).run(store.enqueue(ResearchRequest(ticker="TEST")))
    deferring = ResearchEngine(settings, store, model=FixtureModel("missing"))
    second = deferring.run(store.enqueue(ResearchRequest(ticker="TEST")))
    [alert] = alerts_for(store, second.run_id)
    content = alert["content"]
    assert alert["kind"] == "RESEARCH_UPDATE"
    assert {"RATING", "THESIS", "DATA_QUALITY"} <= set(alert["changes"])
    assert "등급 Hold → 판단 보류" in content and "투자 논지 유효 → 미확인" in content
    assert content.count(second.decision.executive_summary) == 1
    assert "판단 보류 사유:\n- 핵심 자료 없음" in content


def test_a_first_deferral_states_its_reason(store, settings):
    engine = ResearchEngine(settings, store, model=FixtureModel("missing"))
    report = engine.run(store.enqueue(ResearchRequest(ticker="TEST")))
    [alert] = alerts_for(store, report.run_id)
    assert alert["content"].startswith("[FIXTURE] TEST · 첫 리서치: 판단 보류")
    assert "판단 보류 사유:\n- 핵심 자료 없음" in alert["content"]


def claim(text, evidence_id):
    return Claim(
        classification="INTERPRETATION", text=text, evidence_ids=[evidence_id], confidence="중간"
    )


def test_snapshot_restatements_are_not_new_evidence(store, settings):
    engine = ResearchEngine(settings, store)
    first = engine.run(store.enqueue(ResearchRequest(ticker="TEST")))
    second = engine.run(store.enqueue(ResearchRequest(ticker="TEST")))
    records, old = store.evidence(second.run_id), store.evidence(first.run_id)
    # Snapshots differ on every read; they must be excluded by type, not by unchanged content.
    portfolio = next(r for r in records if r.evidence_type == "portfolio")
    portfolio.facts[0].value = Decimal("1")
    quote = next(r for r in records if r.evidence_type == "quote")
    quote.facts[0].value = Decimal("123")
    market_cap = next(r for r in records if r.evidence_type == "calculation")
    filing = EvidenceRecord(
        ticker="TEST",
        evidence_type="document",
        source_name="Issuer",
        source_tier=1,
        source_url="https://issuer.example/8-k",
        content_hash="new-filing",
        text="The issuer will not pursue the acquisition.",
    )
    restated = [
        claim("보유 수량과 평균 매입가 확인", portfolio.evidence_id),
        claim("시가총액 재계산", market_cap.evidence_id),
    ]
    second.decision.material_changes = restated
    assert changes(first, second, records + [filing], old) == []
    second.decision.material_changes = restated + [claim("인수 추진 중단 공시", filing.evidence_id)]
    [alert] = changes(first, second, records + [filing], old)
    assert alert["changes"] == ["MATERIAL_EVIDENCE"]
    assert "새 근거 1건" in alert["content"] and "- 인수 추진 중단 공시" in alert["content"]
    assert "보유 수량" not in alert["content"] and "시가총액" not in alert["content"]


@pytest.mark.parametrize(
    "signal,label",
    [
        ("HOLDER_STOP", "보유 손절선 도달"),
        ("HOLDER_TAKE_PROFIT_0", "익절 검토 가격 1 도달"),
        ("TARGET_1", "목표가 2 도달"),
        ("ENTRY_REVIEW_REQUIRED", "진입 범위 도달 · 조건 재검토"),
    ],
)
def test_monitor_alerts_use_readable_labels(signal, label):
    assert signal_label(signal) == label
