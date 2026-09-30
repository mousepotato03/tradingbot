from app.models import EvidenceRecord, ResearchReport


def markdown(report: ResearchReport, records: list[EvidenceRecord]) -> str:
    def claim_text(claim):
        refs = ", ".join(claim.evidence_ids) or "명시적 가정"
        numbers = "; ".join(f"{n.fact_name}={n.value} {n.unit}" for n in claim.numeric_references)
        return f"- [{claim.classification}] {claim.text} ({refs})" + (
            f" · {numbers}" if numbers else ""
        )

    decision = report.decision
    lines = [
        f"# {report.ticker} 리서치",
        "",
        "**합성 fixture 실행 — 실제 시세·투자 판단이 아닙니다.**" if report.fixture else "",
        "# 1) 최종 결론 요약",
        "",
        f"- 기준 시각: {report.as_of.isoformat()}",
        f"- 등급: {decision.rating.value}",
        f"- 신규 진입자: {decision.new_entry_action}",
        f"- 기존 보유자: {decision.holder_action}",
        f"- 확신도: {decision.confidence}",
        f"- 검증 결과: {'통과' if report.validation.valid else '거절'}",
        "",
        decision.executive_summary,
        "",
        "# 2) 데이터 품질 및 출처",
        "",
        "| 자료 | 기준 시각 | 조회 시각 | 수치·단위 | 출처 | 주의사항 |",
        "|---|---|---|---|---|---|",
    ]
    for record in records:
        warning = "; ".join(record.warnings).replace("|", "/")
        numbers = "; ".join(
            f"{fact.name}: {fact.value} {fact.unit}"
            + (f" ({fact.period_end})" if fact.period_end else "")
            for fact in record.facts[:12]
        ).replace("|", "/")
        if len(record.facts) > 12:
            numbers += f"; 그 외 {len(record.facts) - 12}개는 JSON 증거 원장 참조"
        if record.evidence_type == "portfolio":
            numbers = f"보유 {len(record.payload.get('holdings', []))}개; 계좌 완전성 {record.payload.get('holdings_complete', False)}; NAV 입력 별도"
        lines.append(
            f"| {record.evidence_type} | {record.effective_at or record.published_at or '미확인'} | "
            f"{record.retrieved_at.isoformat()} | {numbers or '정형 수치 없음'} | [{record.source_name}]({record.source_url}) | {warning} |"
        )
    lines += ["", "# 3) 증거 원장", ""]
    for record in records:
        lines.append(
            f"- `{record.evidence_id}` · {record.evidence_type} · SHA256 `{record.content_hash}`"
        )
    lines += [claim_text(c) for c in decision.thesis]
    lines += ["", "# 4) 영역별 분석", ""]
    for section in report.research.sections:
        lines += [f"## {section.name}", ""] + [claim_text(c) for c in section.claims]
        lines += [f"- 한계: {item}" for item in section.limitations]
    lines += ["", "# 5) 강세 대 약세 논쟁", ""]
    for role in ("bull", "bear", "bull_rebuttal", "bear_rebuttal", "research_manager"):
        review = report.reviews.get(role)
        if review:
            lines += [f"## {role}", ""] + [claim_text(c) for c in review.claims]
            lines += [f"- {item}" for item in review.arguments + review.rebuttals]
    lines += ["", "# 6) 시나리오 분석", ""]
    scenarios = [s for s in report.research.sections if "시나리오" in s.name]
    lines += [claim_text(c) for s in scenarios for c in s.claims] or [
        "근거가 있는 시나리오만 제시합니다. 추가 확인이 필요합니다."
    ]
    lines += ["", "# 7) 거래 및 포트폴리오 계획", ""]
    plan = decision.trade_plan
    if plan and report.validation.valid:
        lines += [
            f"- 통화: {plan.currency}",
            f"- 진입 범위: {plan.entry_low.value} ~ {plan.entry_high.value}",
            f"- 손절: {plan.stop.value}",
            f"- 목표: {', '.join(str(t.value) for t in plan.targets)}",
            f"- 수량: {plan.quantity if plan.quantity is not None else '계산 불가: 입력 부족 또는 수량 제안 없음'}",
            f"- 기간: {plan.horizon}",
        ]
        lines += [f"- 조건: {c}" for c in plan.conditions + plan.no_trade_conditions]
        lines += [f"- 계산 {key}: {value}" for key, value in report.validation.calculations.items()]
    else:
        lines.append("검증된 정밀 거래 계획 없음. 부족한 입력이나 검증 오류를 먼저 확인합니다.")
    lines += [f"- 무효화: {c}" for c in decision.invalidation_conditions]
    lines += ["", "# 8) 리스크 위원회", ""]
    for role in ("aggressive_risk", "neutral_risk", "conservative_risk", "premortem"):
        if role in report.reviews:
            review = report.reviews[role]
            lines += [f"## {role}", ""] + [claim_text(c) for c in review.claims]
            lines += [f"- {item}" for item in review.arguments + review.early_warnings]
    lines += [
        "",
        "# 9) 최종 포트폴리오 매니저 결정",
        "",
        f"**Rating: {decision.rating.value}**",
        "",
    ]
    lines += [claim_text(c) for c in decision.thesis]
    lines += [f"- 모니터링: {item}" for item in decision.monitoring_checklist]
    lines += ["", "# 10) 한계 및 추가 필요 자료", ""]
    lines += [
        f"- {item}"
        for item in report.limitations
        + decision.material_gaps
        + report.research.unresolved_questions
    ]
    lines += [f"- 검증 오류 {i.code}: {i.message}" for i in report.validation.issues]
    lines += [
        "",
        "이 분석은 정보 제공을 위한 리서치이며 개인 맞춤 투자 자문이나 수익 보장이 아닙니다.",
        "",
    ]
    return "\n".join(lines)
