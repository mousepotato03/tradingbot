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

### Risk/reward

For a long trade:

```
RR = (target - entry) / (entry - stop)
```

Only calculate when all inputs are validated and comparable.

## 12. New entrant vs holder

Always produce both:

- new-entry action
- existing-holder action

The same rating may translate differently.

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

## 14. No-action is a real decision, not a default

Do not force trades.

But also do not treat Hold as the safe default.

The system must explain why no action has higher expected value when it chooses Hold.
