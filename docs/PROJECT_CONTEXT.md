# Project Context

## 1. Why this repository exists

The previous trading bot produced technically valid monitoring output but failed at the actual goal: high-quality investment research and decision support.

A repeated symptom was that materially different situations were rendered as effectively the same output: `관찰`.

The root problem was architectural, not cosmetic.

The old monitor often followed this pattern:

```
Python logic
-> decide which actions are permitted
-> give the LLM a small packet and a short list of allowed actions
-> LLM chooses an action, with Hold strongly favored
```

That architecture made the LLM a constrained selector instead of a research analyst.

The replacement system should emulate the useful behavior of a capable web-enabled ChatGPT session:

```
identify missing information
-> search for it
-> open primary sources
-> cross-check facts
-> calculate required metrics
-> distinguish fact / interpretation / assumption
-> construct bull and bear cases
-> inspect portfolio context
-> make a decision
-> validate the numerical trade plan
```

The purpose of this repository is to make that workflow repeatable and auditable.

## 2. Target product definition

A concise definition:

> An always-on, evidence-based autonomous investment research system that can investigate securities using structured data and the open web, remember previous theses and candidate states, make explicit portfolio decisions, validate risk deterministically, and notify only when something meaningful changes.

This is not an autonomous brokerage execution system in v1.

The initial production scope is:

- research
- portfolio analysis
- candidate discovery
- entry/exit condition monitoring
- Discord reporting
- outcome tracking

Actual order placement remains outside the research decision loop unless added later as a separately permissioned subsystem.

## 3. Production environment

The intended runtime is an Oracle Cloud free-tier instance already used for the current bot.

The system should therefore support:

- long-running services
- scheduled jobs
- event-driven re-analysis
- a single long-running worker process
- persistent database
- Discord output
- secure secret injection

The architecture must not assume that the user's desktop is online.

## 4. What "web ChatGPT quality" means here

It does **not** mean copying a chat UI.

It means preserving the research behavior:

- freely identify missing evidence
- perform additional searches when the first result is insufficient
- prefer primary sources
- open the source instead of trusting search snippets
- compare different dates carefully
- report a source as unavailable when direct reading is insufficient
- examine filings, IR, news, macro, sector, technicals, and portfolio context together
- stop and say `판단 보류` when data quality is not good enough
- avoid inventing precise numbers merely to complete a template

The tool loop is part of the intelligence of the system.

## 5. Previous system findings that motivated the rebuild

The prior implementation had several structural problems.

### 5.1 Holdings monitoring could bypass AI entirely

The monitor had a watch-only path that rendered one status line per holding and explicitly produced no proposal/levels/AI review.

Therefore repeated `관찰` messages were not necessarily the result of repeated AI judgment.

### 5.2 Even the advice path was action-gated

Deterministic code built a limited list of possible actions first.

The LLM then selected among supplied actions or Hold.

This meant the code could prevent the model from considering a valid action before research occurred.

### 5.3 Prompt bias favored Hold

The selector prompt explicitly treated Hold as the default for maintained core holdings.

This created a structural conservative bias unrelated to the actual strength of the evidence.

### 5.4 Rich investment instructions were disconnected from runtime

The detailed evidence-based investment methodology existed separately from the monitor's compact selector prompt.

The production monitor therefore did not actually execute the same research standard used in manual ChatGPT analysis.

### 5.5 Candidate discovery discarded useful watch ideas

The swing scanner mixed two questions:

1. Is this an attractive research/setup candidate?
2. Is the current ask inside the entry range right now?

A candidate outside the current entry band was rejected rather than preserved as a conditional watch candidate.

### 5.6 Long-horizon discovery was not the primary active path

The old design favored a narrow set of deterministic screens and did not make broad autonomous research the central capability.

## 6. New design principles

### Principle A — Evidence before conclusion

Do not decide Buy/Sell first and search for justification later.

### Principle B — Research agent autonomy

The research agent can request more evidence until a defined evidence threshold or research budget is reached.

### Principle C — Deterministic computation where computation is appropriate

Indicators, accounting ratios, risk/reward, position sizing, freshness, and state transitions should be code-controlled.

### Principle D — LLM judgment where synthesis is appropriate

Business quality, conflicting signals, catalyst importance, bull/bear synthesis, thesis quality, and final portfolio decision require model reasoning over evidence.

### Principle E — Candidate memory

A good idea remains a candidate even when it is not currently actionable.

### Principle F — Change-based monitoring

Alerts should describe meaningful state changes rather than repeat unchanged status.

### Principle G — Auditability

A future reader must be able to answer:

- what did the system know?
- when did it know it?
- where did the fact come from?
- what was interpretation vs fact?
- why did the rating change?
- what numerical rules were applied?

## 7. Canonical high-level flow

```
scheduler / user request / event
            |
            v
research director
            |
            v
data & tool acquisition
            |
            v
evidence ledger
            |
            v
research analyst
            |
            v
bull <-> bear
            |
            v
risk committee
            |
            v
portfolio manager
            |
            v
deterministic validator
            |
            v
state store + Discord
```

## 8. Initial non-goals

The first implementation should not prioritize:

- automatic order execution
- high-frequency trading
- sub-second market data
- complex options execution
- reinforcement learning that changes live trading policy without review
- arbitrary shell/computer control by the research agent
- maximizing alert volume
- maximizing trade frequency

The goal is research quality first.
