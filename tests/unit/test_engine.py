from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.adapters.fixture import FixtureAdapters
from app.adapters.http import ToolError
from app.engine import TOKEN_BUDGETS, ResearchEngine
from app.llm import FixtureModel, InvalidModelOutput
from app.models import ResearchRequest, StageReview, utcnow
from app.reporting import markdown
from app.storage import JobRow, OutboxRow, ReportRow, RunRow


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
    assert result.tool_calls == 10  # 7 baseline reads + market cap + search + read
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
    # Elapsed time is a hard limit: research stops in 판단 보류.
    monkeypatch.setitem(__import__("app.engine", fromlist=["BUDGETS"]).BUDGETS, "deep", (80, 0))
    result = run(store, settings)
    assert result.decision.rating.value == "판단 보류"


def test_used_up_tool_budget_removes_tools_but_still_decides(store, settings, monkeypatch):
    offered = []

    class Recording(FixtureModel):
        def complete(self, role, messages, tools, schema):
            offered.append((role, bool(tools)))
            return super().complete(role, messages, tools, schema)

    monkeypatch.setitem(__import__("app.engine", fromlist=["BUDGETS"]).BUDGETS, "deep", (1, 300))
    run_id = store.enqueue(ResearchRequest(ticker="TEST"))
    result = ResearchEngine(settings, store, model=Recording()).run(run_id)
    tools = [t for t in store.traces(run_id) if t["kind"] == "tool"]
    # Baseline collection does not consume the budget; the one model-requested call does.
    assert sum(not t["baseline"] for t in tools) == 1 and sum(t["baseline"] for t in tools) == 8
    assert result.tool_calls == 9
    assert offered[0] == ("research_director", True) and not any(o for _, o in offered[1:])
    assert result.decision.rating.value == "Hold" and result.validation.valid
    assert len(result.reviews) == 9


def test_token_budget_is_recorded_and_stops_further_model_calls(store, settings):
    class Expensive(FixtureModel):
        def complete(self, *args):
            result = super().complete(*args)
            result.usage = {"total_tokens": TOKEN_BUDGETS["deep"]}
            return result

    run_id = store.enqueue(ResearchRequest(ticker="TEST"))
    report = ResearchEngine(settings, store, model=Expensive()).run(run_id)
    assert report.decision.rating.value == "판단 보류"
    traces = [t for t in store.traces(run_id) if t["kind"] == "model"]
    assert len(traces) == 1 and traces[0]["usage"]["total_tokens"] == TOKEN_BUDGETS["deep"]


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


def test_baseline_includes_market_cap_account_facts_and_52_week_range(store, settings):
    result = run(store, settings)
    records = {r.evidence_type: r for r in store.evidence(result.run_id)}
    [cap] = records["calculation"].facts
    assert cap.name == "market_cap" and cap.unit == "USD" and cap.value == 100 * 100000000
    assert records["calculation"].payload["input_evidence_ids"] == [
        records["quote"].evidence_id,
        records["identity"].evidence_id,
    ]
    assert {f.name for f in records["portfolio"].facts} == {"buying_power"}
    assert {"low_52w", "high_52w"} <= {f.name for f in records["technical"].facts}


class RejectsBull(FixtureModel):
    """Returns contract-breaking output for the bull stage `failures` times."""

    def __init__(self, failures):
        super().__init__()
        self.failures = failures

    def complete(self, role, messages, tools, schema):
        if role == "bull" and self.failures:
            self.failures -= 1
            if self.failures % 2:
                raise InvalidModelOutput(
                    [{"loc": ["claims", 0], "msg": "numbers need references", "type": "x"}],
                    {"input_tokens": 100, "output_tokens": 10},
                )
            StageReview.model_validate_json("{}")
        return super().complete(role, messages, tools, schema)


def test_invalid_model_output_is_corrected_once(store, settings):
    run_id = store.enqueue(ResearchRequest(ticker="TEST"))
    report = ResearchEngine(settings, store, model=RejectsBull(1)).run(run_id)
    assert report.validation.valid
    traces = store.traces(run_id)
    rejection = next(t for t in traces if t["kind"] == "output_rejection")
    assert rejection["role"] == "bull" and rejection["errors"][0]["msg"]
    # The rejected call still counts toward the model-call and token budgets.
    assert {"kind": "model", "role": "bull", "usage": {}} == {
        k: v for k, v in traces[traces.index(rejection) - 1].items() if k != "at"
    }


def test_repeated_invalid_model_output_fails_with_a_clear_code(store, settings):
    run_id = store.enqueue(ResearchRequest(ticker="TEST"))
    with pytest.raises(ToolError, match="MODEL_OUTPUT_INVALID"):
        ResearchEngine(settings, store, model=RejectsBull(2)).run(run_id)
    with store.transaction() as session:
        row = session.get(RunRow, run_id)
        assert (row.status, row.error) == ("FAILED", "MODEL_OUTPUT_INVALID")
