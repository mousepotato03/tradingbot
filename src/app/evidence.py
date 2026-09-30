import hashlib
import json
import re
from datetime import datetime

from app.models import Claim, EvidenceRecord, ValidationIssue


def content_hash(value: bytes | str | dict | list) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def validate_claims(claims: list[Claim], records: list[EvidenceRecord], as_of: datetime):
    ledger = {record.evidence_id: record for record in records}
    issues: list[ValidationIssue] = []
    for i, claim in enumerate(claims):
        for evidence_id in claim.evidence_ids:
            record = ledger.get(evidence_id)
            reason = None
            if record is None:
                reason = "Unknown evidence ID"
            elif record.retrieved_at > as_of:
                reason = "Evidence was not available at the analysis cutoff"
            elif claim.classification == "FACT" and not record.usable_as_fact:
                reason = "Search snippets cannot support material facts"
            if reason:
                issues.append(
                    ValidationIssue(code="UNSUPPORTED_CLAIM", field=str(i), message=reason)
                )
        for number in claim.numeric_references:
            record = ledger.get(number.evidence_id)
            matches = (
                []
                if record is None
                else [
                    fact
                    for fact in record.facts
                    if fact.name == number.fact_name
                    and fact.value == number.value
                    and fact.unit == number.unit
                ]
            )
            if not matches or number.evidence_id not in claim.evidence_ids:
                issues.append(
                    ValidationIssue(
                        code="UNSUPPORTED_NUMBER",
                        field=str(i),
                        message="Numeric fact does not match evidence",
                    )
                )
        # Prose containing material numbers must have structured numeric references.
        # Dates and periods are also facts; keeping them structured makes audit possible.
        if (
            claim.classification != "ASSUMPTION"
            and re.search(r"\d", claim.text)
            and not claim.numeric_references
        ):
            issues.append(
                ValidationIssue(
                    code="UNSTRUCTURED_NUMBER",
                    field=str(i),
                    message="FACT with numbers requires numeric_references",
                )
            )
    return issues


def evidence_is_fresh(record: EvidenceRecord, at: datetime) -> bool:
    if record.retrieved_at > at:
        return False
    if record.stale_after_seconds is None:
        return True
    effective = record.effective_at or record.retrieved_at
    return 0 <= (at - effective).total_seconds() <= record.stale_after_seconds


def persistent_record(record: EvidenceRecord) -> EvidenceRecord:
    """Keep full originals only for approved hosts; retain provenance and bounded snippets otherwise."""
    if record.evidence_type not in {"document", "browser"} or record.payload.get(
        "retain_full_text"
    ):
        return record
    data = record.model_dump(mode="json")
    data["text"] = record.text[:1200]
    payload = data["payload"]
    for page in payload.get("pages", []):
        page["text"] = ""
    for download in payload.get("downloads", []):
        download["text"] = download.get("text", "")[:500]
        for page in download.get("pages", []):
            page["text"] = ""
    payload["retention"] = "metadata_hash_and_snippet"
    data["warnings"].append(
        "Original body retained only as a snippet; reopen source for omitted text"
    )
    return EvidenceRecord.model_validate(data)
