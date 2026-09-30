from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.adapters.fixture import FixtureAdapters
from app.adapters.http import ToolError
from app.engine import ResearchEngine
from app.llm import FixtureModel
from app.models import ResearchRequest, utcnow
from app.reporting import markdown
from app.storage import JobRow, OutboxRow, ReportRow


def run(store, settings, scenario="balanced"):
    run_id = store.enqueue(ResearchRequest(ticker="TEST"))
    return ResearchEngine(settings, store, model=FixtureModel(scenario)).run(run_id)


@pytest.mark.parametrize(
    "scenario,rating",
    [("balanced", "Hold"), ("bullish", "Buy"), ("bearish", "Sell"), ("missing", "판단 보류")],
)
def test_complete_offline_workflow_is_sensitive_to_model_decision(
    store, settings, scenario, rating
):
    result = run(store, settings, scenario)
    assert result.decision.rating.value == rating
    assert result.validation.valid
    assert result.fixture
    assert store.get_run(result.run_id)["status"] == "COMPLETED"
    records = store.evidence(result.run_id)
    assert {"search", "document", "technical", "financials"}.issubset(
        {r.evidence_type for r in records}
    )
    assert result.tool_calls == 9
    assert len(result.reviews) == 9
    content = markdown(result, records)
    assert "합성 fixture" in content and "# 10)" in content


def test_rerun_and_unchanged_report_do_not_duplicate_alerts(store, settings):
    initial = run(store, settings)
    engine = ResearchEngine(settings, store)
    assert engine.run(initial.run_id).run_id == initial.run_id
    run(store, settings)
    with store.transaction() as session:
        assert session.scalar(select(func.count()).select_from(OutboxRow)) == 1
        assert session.scalar(select(func.count()).select_from(ReportRow)) == 2


def test_budget_exhaustion_is_deferred_not_hold(store, settings, monkeypatch):
    monkeypatch.setitem(__import__("app.engine", fromlist=["BUDGETS"]).BUDGETS, "deep", (1, 300))
    result = run(store, settings)
    assert result.decision.rating.value == "판단 보류"


def test_token_budget_is_recorded_and_stops_further_model_calls(store, settings):
    class Expensive(FixtureModel):
        def complete(self, *args):
            result = super().complete(*args)
            result.usage = {"total_tokens": 900_000}
            return result

    run_id = store.enqueue(ResearchRequest(ticker="TEST"))
    report = ResearchEngine(settings, store, model=Expensive()).run(run_id)
    assert report.decision.rating.value == "판단 보류"
    traces = [t for t in store.traces(run_id) if t["kind"] == "model"]
    assert len(traces) == 1 and traces[0]["usage"]["total_tokens"] == 900_000


def test_empty_official_financial_payload_is_insufficient(store, settings):
    class MissingFinancials(FixtureAdapters):
        def financials(self, ticker):
            record = super().financials(ticker)
            record.facts = []
            return record

    run_id = store.enqueue(ResearchRequest(ticker="TEST"))
    report = ResearchEngine(settings, store, filings=MissingFinancials()).run(run_id)
    assert report.decision.rating.value == "판단 보류"


def test_restart_resumes_checkpoint_without_reusing_active_run(store, settings):
    run_id = store.enqueue(ResearchRequest(ticker="TEST"))
    store.start_run(run_id, 7200)
    store.checkpoint(run_id, {"execution_started_at": utcnow().isoformat()})
    with pytest.raises(RuntimeError):
        ResearchEngine(settings, store).run(run_id)
    store.recover_single_worker()
    assert ResearchEngine(settings, store).run(run_id).fixture


def test_missing_account_is_not_fabricated_as_empty_holdings(store, settings):
    class FailingAccount(FixtureAdapters):
        def portfolio(self, ticker):
            raise ToolError("ACCOUNT_UNAVAILABLE")

    run_id = store.enqueue(ResearchRequest(ticker="TEST"))
    engine = ResearchEngine(settings, store, market=FailingAccount())
    engine.run(run_id)
    assert not any(r.evidence_type == "portfolio" for r in store.evidence(run_id))
    assert any(t.get("error_class") == "ACCOUNT_UNAVAILABLE" for t in store.traces(run_id))


def test_historical_run_does_not_fetch_future_evidence(store, settings):
    first = run(store, settings)
    run_id = store.enqueue(ResearchRequest(ticker="TEST", as_of=utcnow() - timedelta(days=365)))
    result = ResearchEngine(settings, store).run(run_id)
    assert result.decision.rating.value == "판단 보류"
    assert not store.evidence(run_id)
    assert store.previous("TEST").run_id == first.run_id


def test_lease_reclaims_only_expired_jobs(store):
    first = store.enqueue(ResearchRequest(ticker="TEST"))
    assert store.claim_job(120) == first
    assert store.claim_job(120) is None
    with store.transaction() as session:
        row = session.scalar(select(JobRow).where(JobRow.run_id == first))
        row.lease_until = utcnow() - timedelta(seconds=1)
    assert store.claim_job(120) == first
