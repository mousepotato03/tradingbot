import hashlib
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from app.models import Claim, EvidenceRecord, QuoteReference, ValidationIssue

# A quoted span shorter than this proves little; payload values may match exactly instead.
MIN_QUOTE_CHARACTERS = 12


def content_hash(value: bytes | str | dict | list) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def normalize_span(text: str) -> str:
    """Whitespace-insensitive comparison; case and punctuation stay exact."""
    return " ".join(text.split())


def _payload_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _payload_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _payload_strings(item)


def payload_json(record: EvidenceRecord) -> str:
    """The payload as the model sees it in context (key order kept, non-ASCII unescaped)."""
    return json.dumps(record.payload, ensure_ascii=False, default=str)


QUOTED_PAIR = re.compile(
    r'"([^"]+)"\s*:\s*("(?:[^"\\]|\\.)*"|-?[\d.]+(?:[eE][-+]?\d+)?|true|false|null)'
)


UNQUOTED_KEY = re.compile(r"[A-Za-z_][\w.\-]*")


def _structured_pairs(text: str) -> list[tuple[str, str]]:
    """Key/value pairs from JSON-like ("key": "v") or plain (key: v; key2: v2) quotes."""
    pairs = QUOTED_PAIR.findall(text)
    if pairs:
        return pairs
    # Commas inside numbers (1,000) are not separators.
    segments = [s.strip() for s in re.split(r";|,(?!\d)", text) if s.strip()]
    parsed = []
    for segment in segments:
        key, colon, value = segment.partition(":")
        key = key.strip().strip('"')
        if not colon or not UNQUOTED_KEY.fullmatch(key) or not value.strip():
            return []
        parsed.append((key, value.strip()))
    return parsed


def _canonical(value) -> str:
    """Compare recorded scalars regardless of JSON quoting or trailing zeros."""
    text = str(value).strip().strip('"')
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return text.lower() if text.lower() in {"true", "false", "null", "none"} else text
    return str(number.normalize()) if number.is_finite() else text


def _recorded_pairs(record: EvidenceRecord) -> set[tuple[str, str]]:
    """Every (key, scalar) in the payload at any depth, plus (fact name, fact value)."""
    pairs: set[tuple[str, str]] = set()

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, (dict, list)):
                    walk(item)
                else:
                    pairs.add((str(key), _canonical("null" if item is None else item)))
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(record.payload)
    pairs |= {(fact.name, _canonical(fact.value)) for fact in record.facts}
    return pairs


def quote_in_record(quote: str, record: EvidenceRecord) -> bool:
    needle = normalize_span(quote)
    if not needle:
        return False
    if len(needle) >= MIN_QUOTE_CHARACTERS and (
        needle in normalize_span(record.text) or needle in normalize_span(payload_json(record))
    ):
        return True
    pairs = _structured_pairs(needle)
    if pairs:
        # Structured quotes (key: value, ...) must match recorded fields exactly, in any order.
        recorded = _recorded_pairs(record)
        return all(
            (key, _canonical(value)) in recorded
            or (key, _canonical(value.split()[0] if value.split() else value)) in recorded
            for key, value in pairs
        )
    # A bare recorded value (field text or number) also counts, since it is an exact field.
    values = {value for _, value in _recorded_pairs(record)}
    if _canonical(needle) in values:
        return True
    for value in _payload_strings(record.payload):
        if len(needle) >= MIN_QUOTE_CHARACTERS and needle in normalize_span(value):
            return True
    return False


def quote_supported(quote: QuoteReference, ledger: dict[str, EvidenceRecord]) -> bool:
    source = ledger.get(quote.evidence_id)
    if source is not None and quote_in_record(quote.text, source):
        return True
    # Bodies from non-retained hosts persist only as snippets; verified spans persist separately.
    needle = normalize_span(quote.text)
    return any(
        record.evidence_type == "quotation"
        and record.payload.get("source_evidence_id") == quote.evidence_id
        and record.text == needle
        for record in ledger.values()
    )


def quotation_record(quote: QuoteReference, source: EvidenceRecord) -> EvidenceRecord:
    """Persistable proof of a verified span, kept even when the source body is not retained."""
    text = normalize_span(quote.text)
    effective_at, stale_after = inherit_freshness([source])
    return EvidenceRecord(
        ticker=source.ticker,
        evidence_type="quotation",
        source_name=source.source_name,
        source_tier=source.source_tier,
        source_url=source.source_url,
        published_at=source.published_at,
        effective_at=effective_at,
        stale_after_seconds=stale_after,
        content_hash=content_hash({"source": source.content_hash, "quote": text}),
        text=text,
        usable_as_fact=source.usable_as_fact,
        payload={
            "source_evidence_id": source.evidence_id,
            "input_evidence_ids": [source.evidence_id],
            "source_content_hash": source.content_hash,
            "verification": "normalized-exact-span",
        },
    )


def fact_matches(recorded: str, referenced: str) -> bool:
    """Exact name, or the metric prefix of a long SEC name ("revenue" for "revenue:us-gaap:…").

    The value and unit must still match exactly, which pins the period.
    """
    return recorded == referenced or recorded.startswith(referenced + ":")


def _number_problem(number, ledger) -> str | None:
    """Explain a numeric reference mismatch precisely enough for the model to fix it."""
    label = f"{number.fact_name}={number.value} {number.unit} @ {number.evidence_id}"
    record = ledger.get(number.evidence_id)
    if record is None:
        return f"{label}: unknown evidence ID"
    named = [fact for fact in record.facts if fact_matches(fact.name, number.fact_name)]
    if not named:
        elsewhere = [
            r.evidence_id
            for r in ledger.values()
            if any(fact_matches(fact.name, number.fact_name) for fact in r.facts)
        ]
        similar = [f.name for f in record.facts if number.fact_name.lower() in f.name.lower()]
        hint = f"; the fact exists in evidence {', '.join(elsewhere[:3])}" if elsewhere else ""
        if similar:
            hint += f"; similar names here: {', '.join(similar[:3])}"
        return f"{label}: no fact with this name in the cited evidence{hint}"
    if not any(fact.value == number.value and fact.unit == number.unit for fact in named):
        recorded = ", ".join(f"{fact.value} {fact.unit}" for fact in named[:3])
        return f"{label}: recorded value/unit is {recorded}; copy it exactly"
    return None


# Identifiers whose digits are names, not quantities (form types, index names).
def _number_tokens(text: str) -> set[str]:
    """Canonical numbers, preserving signs and exponents but not date separators."""
    return {
        _canonical(token.replace(",", ""))
        for token in re.findall(
            r"(?<![\d.])[+-]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?",
            text.replace("−", "-"),
        )
    }


# ASCII boundaries: Korean particles attach directly ("10-Q와"), so \b would not match.
NAMED_NUMBERS = re.compile(
    r"(?<![A-Za-z0-9])(?:10-K|10-Q|8-K|20-F|40-F|6-K|S-1|S-3|S-4|F-1|F-3|13F|13D|13G|DEF 14A|"
    r"N-PORT|NPORT-P|N-CSRS?|497K|485BPOS)(?:/A)?(?![A-Za-z0-9])"
    r"|S&P 500|Nasdaq-100|Russell [123]000",
    re.IGNORECASE,
)


def validate_claims(claims: list[Claim], records: list[EvidenceRecord], as_of: datetime):
    ledger = {record.evidence_id: record for record in records}
    issues: list[ValidationIssue] = []
    for i, claim in enumerate(claims):
        # Evidence named by a numeric reference or a quote is cited by that reference.
        cited = dict.fromkeys(
            claim.evidence_ids
            + [number.evidence_id for number in claim.numeric_references]
            + [quote.evidence_id for quote in claim.quotes]
        )
        for evidence_id in cited:
            record = ledger.get(evidence_id)
            reason = None
            if record is None:
                reason = f"Unknown evidence ID {evidence_id}"
            elif record.retrieved_at > as_of:
                reason = f"Evidence {evidence_id} was not available at the analysis cutoff"
            elif claim.classification == "FACT" and not record.usable_as_fact:
                reason = (
                    f"Evidence {evidence_id} is a search snippet and cannot support a FACT; "
                    "open the source or classify the claim as INTERPRETATION"
                )
            if reason:
                issues.append(
                    ValidationIssue(code="UNSUPPORTED_CLAIM", field=str(i), message=reason)
                )
        referenced = set()
        for number in claim.numeric_references:
            problem = _number_problem(number, ledger)
            if problem:
                issues.append(
                    ValidationIssue(code="UNSUPPORTED_NUMBER", field=str(i), message=problem)
                )
            else:
                referenced.add(_canonical(number.value))
        for quote in claim.quotes:
            if not quote_supported(quote, ledger):
                issues.append(
                    ValidationIssue(
                        code="QUOTE_MISMATCH",
                        field=str(i),
                        message=f"Quote {quote.text[:80]!r} is not an exact span of evidence "
                        f"{quote.evidence_id}; copy it verbatim (use evidence_read) or drop it",
                    )
                )
        # A qualitative FACT must point at the exact source span; numbers at structured facts.
        if claim.classification == "FACT" and not claim.numeric_references and not claim.quotes:
            issues.append(
                ValidationIssue(
                    code="UNGROUNDED_FACT",
                    field=str(i),
                    message="FACT requires numeric_references or an exact source quote",
                )
            )
        # Prose containing material numbers must have structured numeric references, or every
        # number must come from a verified quote ("2026년 8월 26일" from "filed": "2026-08-26").
        # Dates and periods are also facts; keeping them structured makes audit possible.
        numbers = _number_tokens(NAMED_NUMBERS.sub("", claim.text))
        quoted = set().union(
            *(_number_tokens(q.text) for q in claim.quotes if quote_supported(q, ledger))
        )
        uncovered = numbers - referenced - quoted
        if claim.classification != "ASSUMPTION" and uncovered:
            issues.append(
                ValidationIssue(
                    code="UNSTRUCTURED_NUMBER",
                    field=str(i),
                    message="Claim text contains numbers or dates not covered by verified "
                    f"references or quotes: {', '.join(sorted(uncovered))}; "
                    "reference each exact value or keep the text qualitative",
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


def inherit_freshness(inputs: list[EvidenceRecord]) -> tuple[datetime | None, int | None]:
    """Derived evidence expires with its most time-sensitive input.

    Returns (effective_at, stale_after_seconds): the oldest time-sensitive input observation and
    a window ending at the earliest input deadline. Inputs without expiry (filings) impose none.
    """
    windows = [
        (record.effective_at or record.retrieved_at, record.stale_after_seconds)
        for record in inputs
        if record.stale_after_seconds is not None
    ]
    if not windows:
        return None, None
    effective = min(start for start, _ in windows)
    deadline = min(start + timedelta(seconds=seconds) for start, seconds in windows)
    return effective, max(0, int((deadline - effective).total_seconds()))


def lineage_fresh(
    record: EvidenceRecord,
    ledger: dict[str, EvidenceRecord],
    at: datetime,
    seen: frozenset[str] = frozenset(),
) -> bool:
    """A record is usable only if it and every recorded input are fresh at `at`."""
    if record.evidence_id in seen or not evidence_is_fresh(record, at):
        return False
    for input_id in record.payload.get("input_evidence_ids", []):
        parent = ledger.get(input_id)
        if parent is None or not lineage_fresh(parent, ledger, at, seen | {record.evidence_id}):
            return False
    return True


def lineage_identity(
    record: EvidenceRecord,
    ledger: dict[str, EvidenceRecord],
    seen: frozenset[str] = frozenset(),
) -> bool:
    """Derived values belong to their first input's security, including legacy records.

    A calculation may use a peer ratio as its second input, but cannot relabel its first
    input's price. Checking parents also prevents a later calculation laundering a bad label.
    """
    if record.evidence_id in seen:
        return False
    inputs = record.payload.get("input_evidence_ids", [])
    if inputs and record.evidence_type in {
        "technical",
        "calculation",
        "extracted_fact",
        "quotation",
    }:
        first = ledger.get(inputs[0])
        if first is None or first.ticker != record.ticker:
            return False
    return all(
        (parent := ledger.get(input_id)) is not None
        and lineage_identity(parent, ledger, seen | {record.evidence_id})
        for input_id in inputs
    )


def persistent_record(record: EvidenceRecord) -> EvidenceRecord:
    """Keep full originals only for approved hosts; retain provenance and bounded snippets otherwise."""
    if record.evidence_type != "document" or record.payload.get("retain_full_text"):
        return record
    data = record.model_dump(mode="json")
    data["text"] = record.text[:1200]
    payload = data["payload"]
    for page in payload.get("pages", []):
        page["text"] = ""
    payload["retention"] = "metadata_hash_and_snippet"
    data["warnings"].append(
        "Original body retained only as a snippet; reopen source for omitted text"
    )
    return EvidenceRecord.model_validate(data)
