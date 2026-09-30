import json
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.adapters.fixture import FixtureAdapters
from app.adapters.http import ToolError, Transport
from app.adapters.web import TavilySearch
from app.config import Settings
from app.engine import ResearchEngine
from app.models import ResearchRequest, utcnow
from app.monitoring import Monitor
from app.storage import RunRow, SearchUsageRow, WatchRow


def tavily(settings, handler):
    settings.tavily_api_key = SecretStr("tvly-fixture")
    return TavilySearch(settings, Transport(httpx.Client(transport=httpx.MockTransport(handler))))


def test_tavily_maps_search_options_to_its_contract(settings):
    sent = {}

    def handler(request):
        sent["auth"] = request.headers["Authorization"]
        sent["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "request_id": "req-1",
                "results": [
                    {
                        "title": "10-Q",
                        "url": "https://www.sec.gov/x",
                        "content": "snippet",
                        "score": 0.9,
                        "published_date": "2026-08-01",
                    }
                ],
            },
        )

    record = tavily(settings, handler).search(
        "TEST",
        "gross margin guidance",
        count=5,
        freshness="pw",
        domain="sec.gov",
        country="US",
        search_lang="en",
        offset=3,
    )
    body = sent["body"]
    assert sent["auth"] == "Bearer tvly-fixture"
    assert body["search_depth"] == "basic" and body["max_results"] == 5  # one credit
    assert body["time_range"] == "week" and body["include_domains"] == ["sec.gov"]
    assert body["country"] == "united states" and body["language"] == "en"
    assert not body["include_raw_content"] and not body["include_answer"]
    assert record.payload["unsupported_options"] == ["offset"]
    assert not record.usable_as_fact and record.source_name == "Tavily Search"
    assert record.payload["results"][0]["published_at"] == "2026-08-01"


def test_tavily_date_range_news_topic_and_country_limits(settings):
    sent = {}

    def handler(request):
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"results": []})

    record = tavily(settings, handler).search(
        "TEST", "earnings", freshness="2026-01-01to2026-03-31", country="US", topic="news"
    )
    assert (sent["start_date"], sent["end_date"]) == ("2026-01-01", "2026-03-31")
    assert sent["topic"] == "news" and "country" not in sent  # country boosts general only
    assert record.payload["unsupported_options"] == ["country"]


@pytest.mark.parametrize("status", [432, 433])
def test_tavily_credit_exhaustion_is_a_named_tool_error(settings, status):
    search = tavily(settings, lambda request: httpx.Response(status, json={}))
    with pytest.raises(ToolError, match="SEARCH_QUOTA_EXHAUSTED"):
        search.search("TEST", "anything")


def test_rolling_search_cap_counts_only_the_last_30_days(store):
    with store.transaction() as session:
        session.add(SearchUsageRow(provider="tavily", used_at=utcnow() - timedelta(days=31)))
    assert store.consume_search("tavily", None, 2)
    assert store.consume_search("tavily", None, 2)
    assert not store.consume_search("tavily", None, 2)
    assert store.consume_search("tavily", None, None)  # no cap configured


def test_live_search_tool_stops_at_the_local_cap(store, settings):
    engine = ResearchEngine(settings, store)
    engine.settings = settings.model_copy(update={"mode": "live", "search_monthly_limit": 1})
    run_id = store.enqueue(ResearchRequest(ticker="TEST"))
    tools, browser = engine.registry(run_id, ResearchRequest(ticker="TEST"))
    try:
        assert tools.execute("web_search", {"query": "first"}).evidence_type == "search"
        with pytest.raises(ToolError, match="SEARCH_MONTHLY_LIMIT"):
            tools.execute("web_search", {"query": "second"})
    finally:
        browser.close()


@pytest.mark.parametrize(
    "provider,missing", [("tavily", "TAVILY_API_KEY"), ("brave", "BRAVE_API_KEY")]
)
def test_live_configuration_requires_the_selected_search_key(provider, missing):
    config = Settings(
        _env_file=None,
        mode="live",
        search_provider=provider,
        openai_api_key="k",
        research_model="m",
        pm_model="m",
        toss_client_id="i",
        toss_client_secret="s",
        sec_user_agent="Bot ops@example.org",
    )
    with pytest.raises(ValueError, match=missing):
        config.require_live()


class Account(FixtureAdapters):
    def __init__(self, holdings):
        self.holdings = holdings

    def account_snapshot(self):
        record = super().account_snapshot()
        record.payload["holdings"] = self.holdings
        return record


def holding(ticker, quantity, currency="USD"):
    return {
        "ticker": ticker,
        "currency": currency,
        "quantity": quantity,
        "market_value": "100",
        "average_price": "90",
        "sector": None,
    }


def test_account_holdings_are_watched_and_researched_automatically(store, settings):
    market = Account([holding("AAA", "3"), holding("005930", "10", "KRW"), holding("ZERO", "0")])
    # Interval scheduling researches a new holding at once regardless of the wall clock; the
    # market-open schedule is covered in test_guard_and_schedule.
    settings = settings.model_copy(update={"research_schedule": "interval"})
    engine = ResearchEngine(settings, store, market=market)
    Monitor(engine).tick()
    with store.transaction() as session:
        watches = {row.ticker: dict(row.body) for row in session.scalars(select(WatchRow))}
        runs = [ResearchRequest.model_validate(r.request) for r in session.scalars(select(RunRow))]
    assert set(watches) == {"AAA"}
    assert watches["AAA"]["origin"] == "holding" and watches["AAA"]["quantity"] == "3"
    [request] = runs
    assert request.ticker == "AAA" and request.investor_status == "기존 보유"
    assert request.mode == settings.holding_research_mode == "quick"
    assert "계좌 보유 종목" in request.trigger
    Monitor(engine).tick()  # the same holding is not registered or queued twice
    with store.transaction() as session:
        assert session.scalar(select(RunRow.id).where(RunRow.ticker == "AAA"))
        assert len(list(session.scalars(select(RunRow)))) == 1


def test_holdings_auto_watch_can_be_disabled(store, settings):
    engine = ResearchEngine(
        settings.model_copy(update={"holdings_auto_watch": False}),
        store,
        market=Account([holding("AAA", "3")]),
    )
    Monitor(engine).tick()
    with store.transaction() as session:
        assert not list(session.scalars(select(WatchRow)))


@pytest.mark.parametrize(
    "requested,quantity,expected",
    [
        ("신규 진입 검토", "5", "기존 보유"),
        ("기존 보유", "0", "신규 진입 검토"),
        ("일부 매도 검토", "5", "일부 매도 검토"),
        ("기존 보유", None, "기존 보유"),
    ],
)
def test_follow_up_investor_status_follows_the_account(requested, quantity, expected):
    body = {"base_request": {"ticker": "AAA", "investor_status": requested}}
    if quantity is not None:
        body["quantity"] = quantity
    request = Monitor._follow_up("AAA", body, set(), {})
    assert request.investor_status == expected
