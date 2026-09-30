from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.adapters.fixture import FixtureAdapters
from app.evidence import evidence_is_fresh, validate_claims
from app.models import Claim, utcnow


def test_fact_requires_citation():
    with pytest.raises(ValidationError):
        Claim(classification="FACT", text="Revenue rose", evidence_ids=[], confidence="높음")


def test_numeric_reference_must_match_actual_fact_and_unit():
    record = FixtureAdapters().quote("TEST")
    claim = Claim(
        classification="FACT",
        text="Verified price",
        evidence_ids=[record.evidence_id],
        confidence="높음",
        numeric_references=[
            {
                "value": "101",
                "unit": "USD/share",
                "evidence_id": record.evidence_id,
                "fact_name": "last_price",
            }
        ],
    )
    assert validate_claims([claim], [record], utcnow())[0].code == "UNSUPPORTED_NUMBER"
    claim.numeric_references[0].value = 100
    assert not validate_claims([claim], [record], utcnow())


def test_search_snippets_and_future_evidence_cannot_substantiate_facts():
    record = FixtureAdapters().search("TEST", "test")
    claim = Claim(
        classification="FACT", text="Claim", evidence_ids=[record.evidence_id], confidence="높음"
    )
    assert validate_claims([claim], [record], utcnow())
    assert validate_claims([claim], [record], record.retrieved_at - timedelta(seconds=1))


def test_effective_timestamp_drives_freshness():
    record = FixtureAdapters().quote("TEST")
    assert evidence_is_fresh(record, utcnow())
    assert not evidence_is_fresh(record, utcnow() + timedelta(seconds=100))
    assert not evidence_is_fresh(record, record.retrieved_at - timedelta(seconds=1))


def test_future_published_timestamp_rejected():
    record = FixtureAdapters().quote("TEST")
    with pytest.raises(ValidationError):
        record.published_at = utcnow() + timedelta(days=1)


def test_document_retention_keeps_hash_and_snippet_without_altering_live_text(store):
    from app.models import ResearchRequest

    run_id = store.enqueue(ResearchRequest(ticker="TEST"))
    record = FixtureAdapters().read("TEST", "https://fixture.example/release")
    record.text = "Original text " * 500
    record.payload["pages"] = [{"page": 1, "text": record.text}]
    store.add_evidence(run_id, record)
    persisted = store.evidence(run_id)[0]
    assert len(persisted.text) == 1200 and len(record.text) > 1200
    assert persisted.content_hash == record.content_hash
    assert persisted.payload["pages"] == [{"page": 1, "text": ""}]
    record.evidence_id = "retained-test-document"
    record.payload["retain_full_text"] = True
    store.add_evidence(run_id, record)
    assert len(store.evidence(run_id)[1].text) == len(record.text)
