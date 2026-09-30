# Legacy Reuse Plan

## 1. Purpose

The previous bot contains useful infrastructure that should not be reimplemented blindly.

The rule is:

> Reuse adapters and verified calculations; rebuild investment reasoning and orchestration.

## 2. Reuse candidates

When the previous repository/code snapshot is available, inspect and migrate selectively.

Priority reuse targets:

- Toss authentication/client wrapper
- account balance
- holdings
- buying power
- market quote
- bid/ask/orderbook
- OHLCV/candles
- commission/fee calculations
- Discord message delivery
- durable storage helpers that are not tightly coupled to old domain models
- indicator implementations with tests

Each migrated module must receive:

- adapter boundary
- tests
- documented source
- error handling
- timeout/retry behavior

## 3. Do not port old decision architecture

Do not restore these concepts as core architecture:

- holdings_mode = watch/advise
- watch path that bypasses research
- hard-coded Hold default
- AI selector limited to pre-approved actions
- financial-style classifier that blocks actions before research
- swing scanner that rejects a candidate solely because the current ask is outside its entry band
- TradingAgents graph as the system's central abstraction

Legacy code may be used as reference, not authority.

## 4. Migration shape

Prefer:

```
legacy Toss code
      ↓
TossMarketAdapter
TossPortfolioAdapter
TossFeeAdapter
```

rather than importing legacy monitor classes.

Likewise:

```
legacy Discord sender
      ↓
NotificationPort
      ↓
DiscordNotifier
```

## 5. Validation before reuse

For each reused piece answer:

- Is it still correct?
- Does it have hidden state?
- Does it assume old database tables?
- Does it contain investment policy?
- Does it mix retrieval and decision logic?
- Does it handle API failures?
- Does it expose secrets in logs?
- Does it have unit tests?

If decision logic is mixed into an adapter, extract only the data-access portion.

## 6. Data migration

If legacy state is imported, distinguish:

- account snapshots
- holdings history
- quote history
- past recommendations
- old model decisions

Old recommendations should be marked as legacy methodology and must not be treated as if produced by the new research standard.

## 7. Compatibility

The first version does not need to preserve old module paths.

Clean architecture is more important than backward compatibility with monitor internals.

## 8. v0.1 inspected sources and disposition

The adjacent `TradingAgents` Git snapshot `bf186fd8cac8defee75650d33da9630cb514ef1d` was available even though its working files had been removed. Infrastructure references inspected via `git show` were `tradingagents/monitor/toss.py` and `tradingagents/monitor/discord.py`. Its environment secrets were not loaded or copied.

The new read-only Toss adapter was implemented against the [official OpenAPI specification](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json), checked against legacy endpoint/authentication experience, and tested with offline fixtures. No legacy monitor classes, prompt/action gates, tables, graph or recommendation policy were imported. Discord delivery uses a new persistent outbox and disables mentions.

Migration impact: new database and configuration namespace (`TRADINGBOT_`), no compatibility layer for old Python module paths, no automatic import of old decisions, and no concurrent Toss token owner. Validated numerical helpers are new independent functions with known-value tests.
