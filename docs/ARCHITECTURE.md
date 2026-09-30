# Architecture

## 1. Goal

Build an always-on evidence-based investment research platform whose research behavior is closer to a strong web-enabled ChatGPT workflow than to a deterministic signal scanner.

The core split is:

- **LLM reasoning**: research questions, synthesis, conflicting evidence, bull/bear debate, thesis, portfolio decision
- **Deterministic code**: data collection contracts, indicator calculations, accounting math, freshness checks, risk/reward, position sizing, constraints, state transitions

## 2. Top-level components

```
                ┌──────────────────────┐
                │ Scheduler / Trigger  │
                └──────────┬───────────┘
                           │
                           v
                ┌──────────────────────┐
                │ Research Director    │
                └──────────┬───────────┘
                           │
          ┌────────────────┼────────────────┐
          v                v                v
┌────────────────┐ ┌────────────────┐ ┌────────────────┐
│ Structured Data│ │ Web Research   │ │ Browser Worker │
│ Toss / SEC etc │ │ search / read  │ │ Playwright     │
└───────┬────────┘ └───────┬────────┘ └───────┬────────┘
        └───────────────────┼──────────────────┘
                            v
                   ┌──────────────────┐
                   │ Evidence Ledger  │
                   └────────┬─────────┘
                            v
                   ┌──────────────────┐
                   │ Research Analyst │
                   └────────┬─────────┘
                            v
                        Bull ↔ Bear
                            │
                            v
                   ┌──────────────────┐
                   │ Risk Committee   │
                   └────────┬─────────┘
                            v
                   ┌──────────────────┐
                   │ Portfolio Mgr    │
                   └────────┬─────────┘
                            v
                   ┌──────────────────┐
                   │ Trade Validator  │
                   └────────┬─────────┘
                            v
                   ┌──────────────────┐
                   │ State + Discord  │
                   └──────────────────┘
```

## 3. Component boundaries

v0.1 keeps these boundaries in small typed modules rather than creating empty package layers. The current mapping is:

| Boundary | Implementation |
|---|---|
| Schemas / evidence validation | `app/models.py`, `app/evidence.py` |
| Provider adapters | `app/adapters/{toss,sec,web,browser,http}.py` |
| Tool contracts / calculations | `app/tools.py`, `app/analytics.py` |
| Model port / orchestration | `app/llm.py`, `app/engine.py` |
| Numerical / trade validation | `app/validation.py` |
| Persistence / migrations | `app/storage.py`, `migrations/` |
| Candidates / monitoring | `app/lifecycle.py`, `app/discovery.py`, `app/monitoring.py` |
| Notifications / evaluation | `app/notifications.py`, `app/evaluation.py` |
| API / CLI / worker | `app/api.py`, `app/cli.py`, `app/worker.py` |
| Isolated Chromium | `worker_browser/server.py` |

The larger layout below is a possible expansion path; it is not a requirement to import another agent framework.

```
src/
  app/
    orchestration/
      research_director.py
      research_loop.py
      portfolio_manager.py

    data/
      market/
      fundamentals/
      filings/
      macro/
      portfolio/

    tools/
      registry.py
      contracts.py
      web_search.py
      web_reader.py
      browser.py
      python_calc.py

    evidence/
      models.py
      ledger.py
      validator.py
      freshness.py
      provenance.py

    analysis/
      technical.py
      fundamental.py
      valuation.py
      catalysts.py
      sentiment.py

    agents/
      analyst.py
      bull.py
      bear.py
      risk.py
      portfolio_manager.py

    decisions/
      schemas.py
      trade_plan.py
      sizing.py
      consistency.py

    candidates/
      lifecycle.py
      scanner.py
      ranking.py

    monitoring/
      events.py
      diff.py
      scheduler.py
      alerts.py

    integrations/
      toss/
      sec/
      discord/
      storage/

    persistence/
      models.py
      repository.py

  worker_browser/
    server.py
    playwright_runner.py

  api/
    main.py

tests/
  unit/
  integration/
  fixtures/
```

Names may change, but boundaries should remain.

## 4. Service boundaries on Oracle VM

Recommended deployment:

```
docker compose
  research-api
  research-worker
  migrate
  browser-worker
  postgres
  egress-proxy
```

### research-api

Owns:

- request validation and durable queue submission
- read-only status, reports and evidence endpoints
- local-only host binding

It does not receive broker/model/search credentials.

### research-worker

Owns:

- orchestration
- LLM calls
- evidence ledger
- portfolio manager
- deterministic validation
- storage
- background monitoring, discovery, notification outbox and evaluation

One worker owns the Toss token and serializes token issuance. This avoids invalidating another process's token. Requests are paced per Toss rate-limit group (for example `MARKET_DATA`, `MARKET_DATA_CHART`, `ACCOUNT`, `ORDER_INFO`) by token buckets seeded from the documented limits and updated from `X-RateLimit-*` headers, so a chart or account call does not delay quotes. A scheduler thread runs condition checks alongside research; a separate maintenance thread runs discovery triage and outcome refresh so slow model or history calls never delay entry/stop checks. Persistent jobs/checkpoints survive process restart; startup recovery assumes the previous sole worker has stopped.

### browser-worker (optional)

Off by default: it starts only with the Compose `browser` profile and `TRADINGBOT_BROWSER_ENABLED=true`. Without it, the model is not offered browser tools and research uses structured sources, search and the direct document reader.

Owns:

- Chromium
- Playwright
- page rendering
- clicking/typing
- screenshots
- downloads

Security:

- no broker credentials
- no SSH keys
- no root socket
- no unrestricted host filesystem
- strict network/time/resource limits where practical
- private destinations rejected by both URL validation and an egress proxy
- read-only non-root container; no core database network membership
- read-only browsing: only GET/HEAD leave the page. POST is blocked unless the exact hostname is in `BROWSER_POST_ALLOWED_HOSTS` (empty by default); PUT/PATCH/DELETE and WebSockets are always blocked. Blocked requests are reported in each observation so prompt-injected form submissions are visible and inert.

### database

Production Compose uses PostgreSQL 18 and Alembic. SQLite is used for offline fixtures and unit tests. Report/state/outbox updates are committed atomically. Existing legacy tables are not migrated into the new schema.

## 5. Domain entities

### Security

Identity of the traded asset.

Fields include:

- canonical ticker
- exchange
- country
- currency
- asset type
- issuer/company identity

### EvidenceRecord

One auditable unit of information.

Recommended fields:

- evidence_id
- ticker
- evidence_type
- source_name
- source_tier
- source_url
- published_at
- effective_at
- retrieved_at
- raw_reference or content hash
- normalized facts
- confidence
- stale_after
- parser/version metadata

### Claim

A statement used in analysis.

- claim_id
- classification: FACT / INTERPRETATION / ASSUMPTION
- text
- evidence_ids
- confidence

FACT must have evidence.

### ResearchReport

- as_of
- ticker
- market snapshot
- technical analysis
- fundamental analysis
- catalysts
- sentiment
- valuation
- bull case
- bear case
- rebuttals
- premortem
- evidence quality
- unresolved questions

### PortfolioDecision

- rating
- confidence
- new_entry_action (enum) and qualitative note
- holder_action (enum) and qualitative note
- thesis
- thesis_state
- invalidation_conditions
- monitoring_checklist
- material_gaps with `blocking` / `non_blocking` severity
- position_guard for a held position: evidence-backed stop and take-profit levels the monitor watches
- evidence_ids

The allowed rating × action × thesis-state × plan combinations are defined in [Investment Policy](INVESTMENT_POLICY.md#decision-contract) and enforced by the validator.

### TradePlan

Only populated where data supports it.

- entry type / range / conditions
- stop
- invalidation
- targets
- time horizon
- position size
- risk/reward
- no-trade conditions

### CandidateState

States:

- UNIVERSE
- RESEARCH_CANDIDATE
- WATCH_CANDIDATE
- ENTRY_CANDIDATE
- ACTIVE_POSITION
- EXITED
- INVALIDATED

## 6. Research loop

A research run should not receive one fixed packet and immediately answer.

The director repeatedly asks:

1. What do I currently know?
2. What material question is unresolved?
3. Which source/tool is best for that question?
4. Did the new evidence materially change the thesis?
5. Is the evidence sufficient to proceed?
6. Is the research budget exhausted?

Pseudo-flow:

```python
while not state.sufficient and state.tool_calls < budget.max_calls:
    need = director.next_information_need(state)
    tool = tool_router.choose(need)
    result = tool.execute(need)
    evidence.add(result)
    state = director.reassess(evidence)
```

The stopping rule is **evidence sufficiency**, not an arbitrary number of news items.

## 7. Research budgets

Suggested initial modes:

- quick: <= 10 tool calls
- normal: <= 30
- deep: <= 80
- critical: <= 150

These are ceilings, not targets.

Tool budgets count model-requested calls; the fixed baseline collection is recorded but not counted. Using up the tool budget removes tools, and the remaining stages conclude from the evidence already gathered. Elapsed time, cost-weighted tokens (cached input at 10%) and model calls are hard limits: exhausting one becomes 판단 보류, never a default Hold. Checks run at call boundaries; see [runtime limits](RUNTIME.md#6-조사-예산과-근거-한계).

Use deep/critical for:

- new large position
- thesis break
- major earnings
- major regulatory event
- large drawdown
- conflicting sources

## 8. Model routing

Do not use the most expensive model for every symbol.

Future routing example:

```
large universe
  -> deterministic filters

smaller candidate set
  -> cheap/fast model classification

research candidates
  -> full evidence collection

top / held / event-driven names
  -> strong reasoning model

portfolio manager
  -> strongest configured reasoning model
```

The model provider must remain behind an interface so models can change without rewriting orchestration.

The implementation uses three configurable model IDs (research/review, portfolio manager and an optional cheaper triage model that falls back to the research model), each with an optional reasoning-effort setting; unset means the provider default.

Discovery follows the routing above:

1. **Deterministic screen.** The official Toss universe (equity types in `discovery_security_types`; depositary receipts and ETFs excluded by default) is ordered by the one-month market trading-amount ranking, then in stable rotation. For up to `discovery_screen_limit` names, completed daily OHLCV and Toss shares outstanding give price, 20-day dollar volume, market cap, SMA50/200 and 63-session return. Price, liquidity, size and data sufficiency are gates. Trend and relative strength only rank; the current price relative to an entry range is never a screen. Each screen is stored as a `screening` evidence record.
2. **Cheap triage.** One structured model call per ranked candidate returns `deep_research`, `watch_later` or `skip`, citing the screening evidence. It is not a rating.
3. **Deep research.** Only `deep_research` candidates are queued as deep runs, up to the batch. Screen failures and non-deep triage are remembered for `discovery_rescreen_days`.

## 9. State diffing

Each new report is compared with the previous report.

Persist fields that enable meaningful diffs:

- rating
- confidence
- thesis bullets
- thesis state
- material risks
- entry conditions
- stop/invalidation
- target status
- evidence quality
- upcoming catalysts

Discord should report the diff, not merely the latest snapshot.

A completed report's evidence ledger is immutable. The monitor writes quotes, account snapshots and discovery screens to a separate `monitor_observations` table, so a report can always be audited against exactly the evidence it was written from. Each tick reads all due quotes in one `/prices` batch (200 symbols per call, one market calendar) and one shared account snapshot per interval; position changes are attributed only to the ticker whose quantity changed.

Routine re-research runs once per trading day shortly after the US regular open (Toss market calendar; weekends and holidays skipped), when fresh regular-session quotes can validate levels. Price-level checks use regular-session prices only; a closed market is not a data failure, and a breach already signalled is not re-sent at the next open.

USD positions found in the account snapshot are watched automatically. A new holding gets a first research run as `기존 보유` in `holding_research_mode` (default `quick`, because every watch is re-researched daily). The account decides the investor status of every follow-up: a held position is researched as `기존 보유` and a fully sold one as `신규 진입 검토`, unless the request said `일부 매도 검토`.

Re-research inherits the investor's conditions. The request that created a watch (risk inputs, horizon, investor status, question, mode, report policy) is stored with it; monitor-triggered runs reuse it, escalate to `critical` on invalidation, and carry a `trigger` that states what changed since the previous report.

## 10. Failure modes

The system must fail explicitly.

Examples:

- stale quote -> no precise trade plan
- missing filing -> lower confidence
- conflicting financial data -> 판단 보류 if material
- browser failure -> retry via alternate source or mark unavailable
- hallucinated uncited number -> schema/validator rejection
- impossible stop/target geometry -> validator rejection
- model outputs unsupported claim -> remove/retry or downgrade confidence

Never silently fill missing evidence with guessed numbers.

## 11. v0.1 deployment and migration impact

The rationale for the initial service split is token ownership and browser isolation. Tavily is the default search adapter and Brave is selectable; native OpenAI Responses tools provide autonomous research while direct HTML/PDF reads precede Chromium fallback. Screenshot-coordinate operations are ordinary restricted browser tools, without a model-accessible shell.

There is no dependency on TradingAgents or its decision graph. Initial migration `0001` creates a new database. Migration `0002` adds `monitor_observations` and `outcomes.mature`; reports written before the action enums and gap severity load unchanged through model-level coercion (free-text actions become `DEFER` with the original text kept as a marked note; old PM gaps become blocking, old stage gaps advisory). They are not re-validated on load. Old recommendation history must be separately imported and marked as legacy if that work is authorized later. Current fixture reports are clearly synthetic and cannot emit live notifications. See [runtime and operations](RUNTIME.md) for credentials, backup, limits and live acceptance checks.
