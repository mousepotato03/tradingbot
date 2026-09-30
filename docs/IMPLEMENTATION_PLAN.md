# Implementation Plan

## Phase 0 — Architecture bootstrap

Goal: make the repository self-describing.

Deliverables:

- AGENTS.md
- project context
- architecture
- research system design
- investment policy
- legacy reuse plan
- stock research standard
- README

Acceptance:

- a coding agent can enter the repository with no chat history and understand what must be built and what must not be rebuilt

## Phase 1 — Core schemas and evidence ledger

Implement:

- Security identity schema
- Money/Price/Quantity types where useful
- EvidenceRecord
- Claim
- Source metadata
- freshness policy
- ResearchRun
- ResearchReport
- PortfolioDecision
- TradePlan
- CandidateState

Implement deterministic validation for:

- evidence timestamp
- currency
- units
- required provenance
- unsupported fact claims

Tests first.

## Phase 2 — Structured data adapters

Build interfaces and initial adapters for:

- portfolio/account
- quote
- OHLCV
- fees
- SEC filings/company facts

Migrate verified Toss code when the legacy source is available.

Requirements:

- retries
- timeouts
- typed errors
- no decision policy inside adapters
- fixtures
- unit/integration test split

## Phase 3 — Deterministic analytics

Implement:

- EMA/SMA
- RSI
- MACD
- Bollinger Bands
- ATR
- volume metrics
- support/resistance primitives
- financial ratio calculations
- risk/reward
- position sizing
- exposure/concentration checks

All numerical formulas must have tests.

## Phase 4 — Web research tools

Implement:

- web search adapter
- direct URL reader
- document parser
- PDF reader
- source metadata extraction
- evidence record conversion

Then add:

- isolated browser worker
- Chromium
- Playwright
- screenshots/download sandbox
- browser audit log

The research model should not receive unrestricted shell access.

## Phase 5 — Research director loop

Implement a tool-calling research loop.

Capabilities:

- inspect current evidence
- identify missing material question
- select tool
- obtain evidence
- reassess evidence sufficiency
- stop on sufficient evidence or budget exhaustion

Support research budgets:

- quick
- normal
- deep
- critical

Persist tool traces.

## Phase 6 — Analyst and debate layer

Implement structured outputs for:

- technical analysis
- fundamental analysis
- valuation
- catalysts/news/macro
- sentiment
- bull case
- bear case
- rebuttals
- premortem

Every material factual statement must reference evidence IDs.

## Phase 7 — Risk committee and portfolio manager

Implement:

- aggressive risk review
- neutral risk review
- conservative risk review
- final portfolio manager

Canonical rating:

- Buy
- Overweight
- Hold
- Underweight
- Sell
- 판단 보류

Output separately:

- new-entry action
- holder action

Hold must never be hard-coded as the default.

## Phase 8 — Deterministic trade validator

Validate LLM-proposed plans.

Reject:

- stale price
- wrong currency
- stop above long entry
- target below long entry
- impossible RR
- missing evidence for precise levels
- position size exceeding limits
- unsupported numeric claims

The validator returns structured errors for model retry or plan downgrade.

## Phase 9 — Candidate discovery lifecycle

Build:

```
UNIVERSE
-> RESEARCH_CANDIDATE
-> WATCH_CANDIDATE
-> ENTRY_CANDIDATE
-> ACTIVE_POSITION
-> EXITED / INVALIDATED
```

Important:

A candidate outside its preferred entry range remains a WATCH_CANDIDATE.

Do not discard it.

## Phase 10 — Monitoring and Discord

Store prior state and emit change-based alerts.

Alert when:

- rating changes
- thesis changes
- important evidence arrives
- entry condition is reached
- invalidation occurs
- target/trim condition occurs
- material portfolio risk changes

Do not send repetitive unchanged `관찰` lines by default.

## Phase 11 — Historical evaluation

Record every recommendation and later outcomes.

Suggested metrics:

- 1d / 5d / 20d / 60d forward returns
- benchmark-relative returns
- MFE
- MAE
- realized R
- entry hit rate
- invalidation hit rate
- rating transition behavior
- alert precision
- stale-data incidents

The evaluation target is decision quality, not trade frequency.

## Phase 12 — Oracle deployment

Build:

- Dockerfiles
- docker-compose
- env templates
- persistent volumes
- migrations
- health checks
- log rotation
- service restart policy
- backup procedure

Recommended services:

- research-api
- scheduler
- browser-worker
- postgres
- discord notifier if separated

## v1 definition of done

A user or scheduler can request research on a U.S. equity and the system can:

1. verify ticker identity
2. fetch current quote and OHLCV
3. retrieve official financial evidence
4. autonomously search/open additional web evidence
5. use browser fallback if necessary
6. construct evidence-backed bull/bear cases
7. output exactly one portfolio rating
8. distinguish new-entry and holder actions
9. produce a trade plan only where numeric evidence permits
10. validate the plan deterministically
11. store the report and evidence
12. compare against the previous report
13. send Discord only when the configured reporting policy requires it

Automatic order placement is not required for v1.
