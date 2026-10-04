# Investment Policy

This document defines the system-level investment research behavior.

The detailed reusable stock research standard is stored at:

`docs/reference/stock_research_standard_ko.md`

## 1. Required analytical perspectives

Research should integrate, where relevant:

- technical analysis
- fundamental analysis
- news/catalysts
- macro/industry
- market sentiment
- bull research
- bear research
- risk management
- portfolio management

The purpose is not to produce a bullish conclusion. The system must evaluate buy, wait, reduce, and sell possibilities using the same evidence discipline.

## 2. Time integrity

Every research run has an `as_of` timestamp.

The system must record:

- quote timestamp
- market session state
- source publication date
- filing period
- financial statement period
- retrieval timestamp

Historical analysis must not use future evidence.

## 3. Fact / interpretation / assumption

All material claims are classified.

### FACT

Directly supported by reliable evidence.

### INTERPRETATION

Analytical inference derived from facts.

### ASSUMPTION

Unverified or forward-looking premise.

Forward revenue, future earnings, market share, and target prices are never treated as facts.

## 4. Technical analysis

Use verified OHLCV.

Possible inputs include:

- EMA10
- SMA50
- SMA200
- MACD
- RSI
- Bollinger Bands
- ATR
- volume
- support/resistance
- gap structure
- trend structure

Do not conclude a reversal from one oscillator.

Support/resistance claims must be tied to actual price history.

## 5. Fundamental analysis

Where relevant:

- revenue growth
- operating income
- net income
- gross margin
- operating margin
- free cash flow
- cash/debt/liquidity
- working capital
- dilution/buybacks
- capital allocation
- valuation
- historical valuation range
- peer comparison
- segment drivers
- competitive advantage
- customer/supplier concentration
- regulation
- FX/commodity sensitivity
- industry KPIs

Adjusted and GAAP numbers must not be mixed without labeling.

## 6. News / macro / catalysts

Separate:

- confirmed event
- company/regulator statement
- media report
- analyst interpretation
- system interpretation

For each catalyst consider:

- expected timing
- direction
- transmission mechanism
- whether the market may already have priced it in

## 7. Sentiment

Sentiment is a secondary signal.

Possible inputs:

- news tone
- institutional commentary
- flows
- options
- social/community data

Always consider sample size and bias.

Conflicting sentiment sources should be marked mixed.

## 8. Debate

The system must formulate the strongest evidence-based:

- bull case
- bear case

Then each side addresses the other's strongest argument.

The research manager/portfolio manager evaluates evidence quality rather than counting arguments.

## 9. Premortem

Before the final decision answer:

> If this investment idea fails, what are the most likely reasons?

Connect each failure mode to early-warning evidence.

## 10. Final rating

Exactly one:

- Buy
- Overweight
- Hold
- Underweight
- Sell
- 판단 보류

Definitions:

### Buy

Evidence is strong and risk-adjusted expected return supports new entry or meaningful expansion.

### Overweight

Outlook is favorable but uncertainty supports staged expansion.

### Hold

Evidence is genuinely balanced or the current price produces low action value.

For a non-holder: wait / do not initiate now.

### Underweight

Risk-adjusted expected return is unattractive.

For holders: partial reduction may be appropriate.
For non-holders: avoid new entry.

### Sell

Thesis is materially impaired or downside risk is excessive.

This does not imply initiating a short position.

### 판단 보류

Material evidence is missing/conflicting/stale enough that a tradeable rating is not justified.

### Decision contract

The rating, both actions, the thesis state and the trade plan must describe one decision. The deterministic validator (`app/validation.py`) rejects any other combination; it does not repair it.

| Rating | New entry action | Existing holder action | Thesis state |
|---|---|---|---|
| Buy | 지금 진입 `ENTER_NOW`, 조건부 진입 `CONDITIONAL_ENTRY`, 분할 진입 `STAGED_ENTRY` | 추가 매수 `ADD`, 유지 `HOLD` | `ACTIVE` |
| Overweight | 분할 진입, 조건부 진입 | 추가 매수, 유지 | `ACTIVE` |
| Hold | 관망 `WAIT`, 조건부 진입 | 유지, 이익 보호 `PROTECT_PROFIT` | `ACTIVE`, `WEAKENED` |
| Underweight | 진입 회피 `AVOID` | 일부 축소 `TRIM`, 이익 보호 | `ACTIVE`, `WEAKENED` |
| Sell | 진입 회피 | 청산 `EXIT`, 일부 축소 | `ACTIVE`, `WEAKENED`, `INVALIDATED` |
| 판단 보류 | 판단 보류 `DEFER` | 판단 보류 | any |

Actions come from the research standard's lists (section 4). Free-text notes may qualify an action but never replace it.

Additional rules:

- Every rating other than 판단 보류 needs at least one thesis claim that cites evidence and is not an ASSUMPTION. Hold is not special: Buy and Sell need the same grounding.
- A long `TradePlan` exists only for an entry action (`ENTER_NOW`, `CONDITIONAL_ENTRY`, `STAGED_ENTRY`) or a holder `ADD`. Underweight, Sell and 판단 보류 carry no long entry plan.
- `ENTER_NOW` and `STAGED_ENTRY` execute now, so a fresh quote must lie inside the entry range. A good company whose price is outside the range stays a candidate through `CONDITIONAL_ENTRY` (Buy/Overweight/Hold), not through a forced Hold.
- 판단 보류 must name at least one blocking gap. An unexplained 판단 보류 is as unacceptable as a default Hold.

### Material gaps

Every stage reports gaps with a proposed severity: `blocking` (a tradeable rating is not justified) or `non_blocking` (a limitation to disclose).

Only two roles make a gap binding:

- the **research manager**, after the debate, through its blocking gaps and its explicit `evidence_sufficient` judgment;
- the **portfolio manager**, through the gaps it keeps in its decision.

Missing personal inputs (portfolio value, maximum loss, horizon, look-through exposure via funds) prevent a quantity, not a rating: they are non-blocking and the plan omits `quantity`.

A binding blocking gap requires 판단 보류. Gaps raised by the director, debaters, risk perspectives or premortem are shown to the portfolio manager as open gaps and remain in the report, but one reviewer can no longer force 판단 보류 alone. The director's early insufficiency is a research to-do list for later stages, not a verdict.

## 11. Trading plan rules

Only produce precise levels when supported by verified data.

### Entry

Use:

- current price
- support/resistance
- volatility
- valuation
- catalysts

Conditional/staged entry is preferred when uncertainty is material.

### Stop and invalidation

Separate:

- price stop
- thesis invalidation

A long stop must be below the relevant long entry.

Gap risk can exceed the intended stop loss.

### Targets

Tie targets to at least one of:

- technical resistance
- valuation
- earnings assumptions
- explicit catalyst

Avoid fake precision.

### Position size

Do not output exact size without sufficient portfolio/risk inputs.

Reference formula when valid:

```
allowed_loss = portfolio_value * max_loss_pct
theoretical_shares = allowed_loss / abs(entry - stop)
```

Then apply:

- concentration limits
- liquidity limits
- correlated exposure
- fees
- slippage
- tax
- FX
- gap risk

Quantities follow the broker's tradable step. The Toss Open API accepts whole-share quantities for limit and quantity orders. A fractional U.S. buy is a dollar-amount market order (`orderAmount`) during the regular session, filled to six decimal places. A plan therefore declares `sizing_unit`:

- `whole_share` (default): quantity floored to whole shares; the entry range behaves like a limit.
- `fractional_amount`: quantity floored to 0.000001 share; the fill is a market price, so slippage assumptions apply and no limit is implied.

A small account may therefore show zero whole shares while a fractional amount is still valid. The validator floors; it never rounds up.

### Risk/reward

For a long trade:

```
RR = (target - entry) / (entry - stop)
```

Only calculate when all inputs are validated and comparable.

### Holding guard

An existing holding needs levels the monitor can watch, not only a narrative. For a position the account shows as held, the portfolio manager provides `position_guard` unless the holder action is `EXIT`:

- a stop below the current price (for example the structural low that breaks the thesis, or a loss limit) and optional take-profit prices above it, where a trim should be reviewed;
- each level equals an observed or deterministically calculated price fact, like trade-plan levels.

The validator checks evidence, lineage freshness and position relative to the fresh price. A guard may accompany 판단 보류: it protects the existing holding and is not a new trade. Guard levels are judged only on regular-session prices, so thin pre-market or after-hours trades do not trigger them.

A guard, once validated, stays in force for a held position until a newer guard validates. When a report for a position the account still holds validates no guard of its own (판단 보류 without a guard, a rejected decision, or levels that cannot be validated), the report records the previous effective guard as `inherited_guard` with the report that validated it, and the monitor keeps watching it. The decision itself is not changed; the alert and report mark the stop as carried over. The PM sees the guard in force in the previous-report summary and keeps or replaces it. Only a position the account no longer holds ends stop monitoring.

## 12. New entrant vs holder

Always produce both:

- new-entry action
- existing-holder action

The same rating may translate differently. Both are enumerated values (see the decision contract), so a monitor or reviewer can compare them across reports without parsing prose.

## 13. Risk committee

Review the plan through:

- aggressive perspective
- neutral perspective
- conservative perspective

Do not mechanically average them.

The final manager weighs:

- evidence quality
- asymmetry
- time horizon
- user risk limits
- existing exposure

## 14. ETFs

Plain (unleveraged, non-inverse) ETFs are researched on their own terms: index and methodology, expense ratio, holdings concentration, sector/country exposure, liquidity and tracking. Issuer financial statements do not apply. The official structured basis is the fund's latest SEC N-PORT filing (holdings, net assets, asset and country mix), with the summary prospectus for fees and strategy. Weights in N-PORT lag the report date and must be labeled as such.

Leveraged or inverse funds and ETNs are rejected at identity. Funds absent from SEC's fund ticker mapping (`company_tickers_mf.json`), such as UIT-structured SPY without a series ID, have no automated holdings source yet and resolve to 판단 보류 rather than an equity-style analysis.

## 15. No-action is a real decision, not a default

Do not force trades.

But also do not treat Hold as the safe default.

The system must explain why no action has higher expected value when it chooses Hold.
