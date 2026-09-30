from datetime import timedelta
from decimal import Decimal

from app.evidence import content_hash, evidence_is_fresh
from app.models import CandidateState as State
from app.models import EvidenceRecord, ResearchReport

ALLOWED = {
    State.UNIVERSE: {State.RESEARCH, State.ACTIVE},
    State.RESEARCH: {State.WATCH, State.ENTRY, State.ACTIVE, State.INVALIDATED},
    State.WATCH: {State.RESEARCH, State.ENTRY, State.ACTIVE, State.INVALIDATED},
    State.ENTRY: {State.WATCH, State.RESEARCH, State.ACTIVE, State.INVALIDATED},
    State.ACTIVE: {State.EXITED},
    State.EXITED: {State.RESEARCH, State.WATCH, State.ENTRY, State.ACTIVE, State.INVALIDATED},
    State.INVALIDATED: {State.RESEARCH, State.ACTIVE},
}


def transition(old: State, new: State) -> State:
    if old != new and new not in ALLOWED[old]:
        raise ValueError(f"Invalid candidate transition: {old} -> {new}")
    return new


def candidate_state(
    report: ResearchReport, records: list[EvidenceRecord], previous: ResearchReport | None
):
    portfolios = [
        r
        for r in records
        if r.evidence_type == "portfolio" and evidence_is_fresh(r, report.created_at)
    ]
    if portfolios:
        portfolio = portfolios[-1].payload
        held = any(
            h["ticker"] == report.ticker and Decimal(str(h["quantity"])) > 0
            for h in portfolio.get("holdings", [])
        )
        if held:
            return State.ACTIVE
        if previous and previous.candidate_state == State.ACTIVE:
            return State.EXITED if portfolio.get("holdings_complete") else State.ACTIVE
    elif previous and previous.candidate_state == State.ACTIVE:
        return State.ACTIVE
    if report.decision.thesis_state == "INVALIDATED":
        return State.INVALIDATED
    if report.decision.rating.value == "판단 보류":
        return (
            State.WATCH
            if previous and previous.candidate_state in {State.WATCH, State.ENTRY}
            else State.RESEARCH
        )
    plan = report.decision.trade_plan
    quotes = [
        r
        for r in records
        if r.evidence_type == "quote"
        and r.ticker == report.ticker
        and evidence_is_fresh(r, report.created_at)
    ]
    # Free-text entry conditions need a new model review; never infer they were satisfied.
    if (
        plan
        and report.validation.valid
        and not plan.conditions
        and quotes
        and quotes[-1].payload.get("market_state") == "REGULAR"
    ):
        price = Decimal(str(quotes[-1].payload["price"]))
        if plan.entry_low.value <= price <= plan.entry_high.value:
            return State.ENTRY
    return State.WATCH


def changes(
    previous: ResearchReport | None,
    current: ResearchReport,
    records: list[EvidenceRecord] | None = None,
    previous_records: list[EvidenceRecord] | None = None,
) -> list[dict]:
    differences = []
    if previous is None:
        differences.append(("INITIAL_RESEARCH", "첫 리서치", current.decision.rating.value))
    else:
        for name, label, old, new in [
            ("RATING", "등급", previous.decision.rating.value, current.decision.rating.value),
            ("THESIS", "투자 논지", previous.decision.thesis_state, current.decision.thesis_state),
            (
                "CANDIDATE",
                "후보 상태",
                previous.candidate_state.value,
                current.candidate_state.value,
            ),
        ]:
            if old != new:
                differences.append((name, label, f"{old} → {new}"))
        old_gaps, new_gaps = (
            set(previous.decision.material_gaps),
            set(current.decision.material_gaps),
        )
        if old_gaps != new_gaps:
            differences.append(
                ("DATA_QUALITY", "핵심 자료 부족", "; ".join(sorted(new_gaps)) or "해소")
            )
        for claim in current.decision.material_changes:
            old_text = {c.text for c in previous.decision.material_changes}
            new_evidence = True
            if records is not None and previous_records is not None:

                def signature(record):
                    return content_hash(
                        {
                            "type": record.evidence_type,
                            "ticker": record.ticker,
                            "source": record.source_url,
                            "data": [f.model_dump(mode="json") for f in record.facts]
                            if record.facts
                            else record.content_hash,
                        }
                    )

                old_signatures = {signature(r) for r in previous_records}
                new_evidence = any(
                    r.evidence_id in claim.evidence_ids and signature(r) not in old_signatures
                    for r in records
                )
            if claim.text not in old_text and new_evidence:
                differences.append(("MATERIAL_EVIDENCE", "새 근거", claim.text))
    return [
        {
            "id": content_hash(
                {
                    "previous": current.previous_report_id,
                    "current": current.run_id,
                    "kind": kind,
                    "value": value,
                }
            ),
            "run_id": current.run_id,
            "fixture": current.fixture,
            "expires_at": (current.created_at + timedelta(hours=24)).isoformat(),
            "ticker": current.ticker,
            "kind": kind,
            "content": f"{'[FIXTURE] ' if current.fixture else ''}{current.ticker} · {label}: {value}\n"
            f"신규: {current.decision.new_entry_action}\n보유: {current.decision.holder_action}\n"
            f"{current.decision.executive_summary}",
        }
        for kind, label, value in differences
    ]
