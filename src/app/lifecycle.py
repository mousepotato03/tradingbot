from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal

from app.evidence import content_hash, evidence_is_fresh
from app.models import ACTION_LABELS, EvidenceRecord, ResearchReport
from app.models import CandidateState as State

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
    portfolios = sorted(
        (r for r in records if r.evidence_type == "portfolio"), key=lambda r: r.retrieved_at
    )
    latest = portfolios[-1].payload if portfolios else None
    # A run outlasts the account freshness window. A holding seen in this run's account read is
    # still held (the monitor reports quantity changes); concluding an exit needs a fresh read.
    if latest and any(
        h["ticker"] == report.ticker and Decimal(str(h["quantity"])) > 0
        for h in latest.get("holdings", [])
    ):
        return State.ACTIVE
    if previous and previous.candidate_state == State.ACTIVE:
        if latest and evidence_is_fresh(portfolios[-1], report.created_at):
            return State.EXITED if latest.get("holdings_complete") else State.ACTIVE
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


def own_guard(report: ResearchReport):
    """The guard this report's own decision validated, if any."""
    return report.decision.position_guard if report.validation.valid else None


def guard_of(report: ResearchReport):
    """The monitored holding guard: the report's own, else the one it inherited."""
    return own_guard(report) or report.inherited_guard


def inherit_guard(report: ResearchReport, previous: ResearchReport | None) -> None:
    """Keep a held position's last validated guard when this report validated none.

    A deferred or rejected decision, or a quote that cannot validate new levels, must not
    silently end stop monitoring for a position the account still holds.
    """
    if (
        previous is None
        or report.candidate_state != State.ACTIVE
        or own_guard(report) is not None
        or guard_of(previous) is None
    ):
        return
    report.inherited_guard = guard_of(previous)
    report.inherited_guard_from = (
        previous.run_id if own_guard(previous) else previous.inherited_guard_from
    )


def guard_line(report: ResearchReport) -> str:
    guard = guard_of(report)
    if guard is None:
        return ""
    targets = ", ".join(str(level.value) for level in guard.take_profit)
    line = f"손절선: {guard.stop.value} {guard.currency}"
    line += f" · 익절 검토: {targets}" if targets else ""
    return line + (" (이전 보고서 기준 유지)" if own_guard(report) is None else "") + "\n"


# Market and account snapshots change on every read. A claim resting only on them restates the
# price or the position, which the monitor reports itself; it is not new evidence.
SNAPSHOT_TYPES = {"identity", "quote", "ohlcv", "technical", "portfolio", "fees"}
THESIS_LABELS = {"ACTIVE": "유효", "WEAKENED": "약화", "INVALIDATED": "무효", "UNKNOWN": "미확인"}
STATE_LABELS = {
    State.UNIVERSE: "유니버스",
    State.RESEARCH: "조사 후보",
    State.WATCH: "관찰 후보",
    State.ENTRY: "진입 후보",
    State.ACTIVE: "보유 중",
    State.EXITED: "청산",
    State.INVALIDATED: "무효화",
}
# One alert per report; long lists point to the report instead of flooding the channel.
ALERT_ITEMS = 3
ALERT_ITEM_CHARACTERS = 180
# The summary is the one free-form field; Discord cuts messages at 1,900 characters mid-line.
ALERT_SUMMARY_CHARACTERS = 500


def _snapshot_only(record, ledger, seen=frozenset()) -> bool:
    """True when the record is, or derives only from, market/account snapshots."""
    if record.evidence_type in SNAPSHOT_TYPES:
        return True
    inputs = record.payload.get("input_evidence_ids") or []
    if not inputs or record.evidence_id in seen:
        return False
    return all(
        i in ledger and _snapshot_only(ledger[i], ledger, seen | {record.evidence_id})
        for i in inputs
    )


def _signature(record):
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


def _cited(claim) -> list[str]:
    return list(
        dict.fromkeys(
            claim.evidence_ids
            + [number.evidence_id for number in claim.numeric_references]
            + [quote.evidence_id for quote in claim.quotes]
        )
    )


def new_evidence_claims(
    previous: ResearchReport,
    current: ResearchReport,
    records: list[EvidenceRecord] | None,
    previous_records: list[EvidenceRecord] | None,
) -> list[str]:
    """Material changes resting on evidence the previous report did not have."""
    old_text = {c.text for c in previous.decision.material_changes}
    ledger = {r.evidence_id: r for r in records or []}
    old_signatures = {_signature(r) for r in previous_records or []}
    found = []
    for claim in current.decision.material_changes:
        if claim.text in old_text or claim.text in found:
            continue
        if records is not None and previous_records is not None:
            cited = [ledger[i] for i in _cited(claim) if i in ledger]
            if not any(
                not _snapshot_only(r, ledger) and _signature(r) not in old_signatures for r in cited
            ):
                continue
        found.append(claim.text)
    return found


def changes(
    previous: ResearchReport | None,
    current: ResearchReport,
    records: list[EvidenceRecord] | None = None,
    previous_records: list[EvidenceRecord] | None = None,
) -> list[dict]:
    """At most one alert for a report, listing everything that changed since the previous one."""
    if previous is None:
        return [report_alert(current, [("INITIAL_RESEARCH", "")])]
    differences = []
    old, new = previous.decision, current.decision
    if old.rating != new.rating:
        differences.append(("RATING", f"등급 {old.rating.value} → {new.rating.value}"))
    if old.thesis_state != new.thesis_state:
        differences.append(
            (
                "THESIS",
                f"투자 논지 {THESIS_LABELS[old.thesis_state]} → {THESIS_LABELS[new.thesis_state]}",
            )
        )
    if previous.candidate_state != current.candidate_state:
        differences.append(
            (
                "CANDIDATE",
                f"후보 상태 {STATE_LABELS[previous.candidate_state]} → "
                f"{STATE_LABELS[current.candidate_state]}",
            )
        )
    # A holding gaining or losing its monitored stop matters; daily level drift does not.
    old_guard, new_guard = guard_of(previous), guard_of(current)
    if (old_guard is None) != (new_guard is None):
        differences.append(
            (
                "POSITION_GUARD",
                f"보유 손절선 {new_guard.stop.value} {new_guard.currency} 감시 시작"
                if new_guard
                else "보유 손절선 감시 해제",
            )
        )
    # Only binding gaps change what the system can decide; advisory gaps are report detail.
    old_gaps = {gap.description for gap in old.blocking_gaps()}
    new_gaps = {gap.description for gap in new.blocking_gaps()}
    if old_gaps != new_gaps:
        differences.append(
            ("DATA_QUALITY", "핵심 자료 부족 변경" if new_gaps else "핵심 자료 부족 해소")
        )
    evidence = new_evidence_claims(previous, current, records, previous_records)
    if evidence:
        differences.append(("MATERIAL_EVIDENCE", f"새 근거 {len(evidence)}건"))
    return [report_alert(current, differences, evidence)] if differences else []


def _clip(text: str, limit: int = ALERT_ITEM_CHARACTERS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _items(title: str, texts: list[str]) -> list[str]:
    if not texts:
        return []
    lines = [title] + [f"- {_clip(text)}" for text in texts[:ALERT_ITEMS]]
    if len(texts) > ALERT_ITEMS:
        lines.append(f"- 외 {len(texts) - ALERT_ITEMS}건은 보고서 참조")
    return lines


def report_alert(
    current: ResearchReport,
    differences: list[tuple[str, str]],
    evidence: Sequence[str] = (),
) -> dict:
    """The single alert for a report: what changed, the decision, why it defers, what is new."""
    decision = current.decision
    kinds = [kind for kind, _ in differences]
    if "INITIAL_RESEARCH" in kinds:
        header = f"{current.ticker} · 첫 리서치: {decision.rating.value}"
    elif differences:
        header = f"{current.ticker} · 리서치 갱신"
    else:
        header = f"{current.ticker} · 리서치 결과: {decision.rating.value} (변화 없음)"
    lines = [("[FIXTURE] " if current.fixture else "") + header]
    changed = [text for _, text in differences if text]
    if changed:
        lines.append("변경: " + " · ".join(changed))
    lines.append(
        f"신규: {ACTION_LABELS[decision.new_entry_action]} · "
        f"보유: {ACTION_LABELS[decision.holder_action]}"
    )
    guard = guard_line(current).strip()
    if guard:
        lines.append(guard)
    lines.append(_clip(decision.executive_summary, ALERT_SUMMARY_CHARACTERS))
    lines += _items("판단 보류 사유:", [gap.description for gap in decision.blocking_gaps()])
    lines += _items("새 근거:", list(evidence))
    return {
        "id": content_hash(
            {"previous": current.previous_report_id, "current": current.run_id, "kinds": kinds}
        ),
        "run_id": current.run_id,
        "fixture": current.fixture,
        "expires_at": (current.created_at + timedelta(hours=24)).isoformat(),
        "ticker": current.ticker,
        "kind": "INITIAL_RESEARCH" if "INITIAL_RESEARCH" in kinds else "RESEARCH_UPDATE",
        "changes": kinds,
        "content": "\n".join(lines),
    }
