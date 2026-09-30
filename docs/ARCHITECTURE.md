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

## 3. Suggested code layout

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
  scheduler
  browser-worker
  database
  discord-worker (optional separate process)
```

### research-api

Owns:

- orchestration
- LLM calls
- evidence ledger
- portfolio manager
- deterministic validation
- storage

### browser-worker

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

### database

Start with PostgreSQL if operationally convenient. SQLite is acceptable for an early prototype, but schemas must remain migration-friendly.

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
- new_entry_action
- holder_action
- thesis
- invalidation_conditions
- monitoring_checklist
- evidence_ids

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

Use deep/critical for:

- new large position
- thesis break
- major earnings
- major regulatory event
- large drawdown
- conflicting sources

## 8. Model routing

Do not use the most expensive model for every symbol.

Example:

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
