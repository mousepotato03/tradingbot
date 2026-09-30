# Research System

## 1. Objective

The research system gives the agent enough freedom to investigate like a capable web research assistant while preserving source quality, reproducibility, and security.

The model should be able to discover that it lacks a fact and obtain that fact using tools.

## 2. Tool hierarchy

Use the cheapest and most reliable source first.

### Tier A — Structured / official tools

Examples:

- broker/account data
- market quotes
- OHLCV
- SEC submissions
- SEC XBRL company facts
- company IR feeds
- official macro/statistical APIs
- exchange data where available

Best for:

- current/official numbers
- standardized fields
- reproducible calculations

### Tier B — Direct document/web reader

Use when a known URL can be fetched directly.

Capabilities:

- GET page/document
- parse readable text
- extract headings/tables
- find text
- download supported files
- PDF text extraction

Best for:

- IR pages
- press releases
- filings
- official notices
- known news URLs

### Tier C — Web search

Use for discovery and breadth.

Search runs behind a provider adapter. Tavily is the default: its free plan (1,000 basic-search credits a month, no card) blocks at the limit instead of billing. Brave remains selectable with `TRADINGBOT_SEARCH_PROVIDER=brave`; it requires a card and bills beyond its monthly credit. Live searches are also capped locally per rolling 30 days (`TRADINGBOT_SEARCH_MONTHLY_LIMIT`, default 900). At the cap, or when the provider reports exhausted credits, the tool returns `SEARCH_MONTHLY_LIMIT` / `SEARCH_QUOTA_EXHAUSTED` and research continues with official tools and known source URLs. Search is the default discovery mechanism; Chromium is a fallback. Search snippets remain unusable as FACT evidence. OpenAI Responses native function calling chooses queries and follow-up sources autonomously.

The research agent may formulate new searches based on previous findings.

Rules:

- search snippets are discovery aids, not final evidence for material claims
- open the underlying source for important facts
- prefer recent sources for recent claims
- prefer primary sources when available
- search both confirming and disconfirming evidence

### Tier D — Playwright / Chromium

Optional and off by default (`TRADINGBOT_BROWSER_ENABLED`, Compose profile `browser`). Use only when direct reading is inadequate.

Examples:

- client-rendered pages
- tabs/buttons
- on-site search
- infinite scroll
- interactive tables
- JS-only content
- download buttons

### Tier E — Computer-use style interaction

Reserve for sites that genuinely require visual/UI interaction.

It must not become the default browsing mechanism.

## 3. Tool contracts

All tools should return typed results.

Example:

```json
{
  "tool": "web.search",
  "query": "NVDA latest earnings guidance",
  "retrieved_at": "...",
  "results": [
    {
      "title": "...",
      "url": "...",
      "published_at": "...",
      "source": "..."
    }
  ]
}
```

A page reader should return:

- url
- title
- source
- published_at if available
- retrieved_at
- main text or structured blocks
- extraction method
- content hash
- warnings

A financial tool should return:

- metric name
- value
- unit
- currency
- period
- GAAP/non-GAAP
- source
- published_at
- retrieved_at

## 4. Evidence sufficiency

The director should evaluate research completeness across dimensions.

Suggested checklist:

- identity verified
- current price verified
- price freshness known
- technical dataset sufficient
- latest annual filing obtained
- latest quarterly filing obtained
- recent earnings release obtained
- guidance/management commentary obtained
- major recent company-specific events checked
- macro/industry relevance checked
- valuation inputs verified
- upcoming event risk checked
- portfolio exposure known if required
- contradictory facts reconciled or explicitly unresolved

Not every stock needs every field, but skipped areas need a reason.

## 5. Source provenance

Each material claim must link back to evidence.

Example:

```
Claim:
  "Operating margin expanded year over year."

Classification:
  FACT

Evidence:
  EV-0012
  EV-0015

Interpretation:
  "The margin expansion supports operating leverage."
```

Do not collapse the fact and the interpretation into one opaque sentence.

Every FACT is grounded structurally, not just by an ID:

- numbers through `numeric_references` that match an evidence fact's name, value and unit;
- qualitative statements through `quotes`, exact spans of the cited evidence (whitespace-normalized, at least 12 characters, or a whole structured field value such as an exchange name).

A FACT with neither is rejected (`UNGROUNDED_FACT`); a quote that is not in the cited source is rejected (`QUOTE_MISMATCH`). Deterministic code still cannot prove that the sentence means what the span says. It proves the span exists, so a reviewer can compare the two directly. Each verified span is persisted as a short `quotation` evidence record, so it stays auditable even when the source body is retained only as a snippet.

## 6. Primary-source preference

Examples for U.S. equities:

- SEC filing > finance portal summary
- issuer earnings release > reposted article
- central bank statement > article quoting it
- regulator order > article describing it

Secondary sources remain useful for:

- industry context
- interviews
- market reaction
- analyst framing
- investigative reporting

## 7. Cross-checking

Cross-check especially:

- current quote when it drives trade levels
- market cap
- revenue/net income
- shares outstanding
- cash/debt
- earnings dates
- analyst consensus used in valuation

If sources conflict:

1. inspect date/period/unit/accounting basis
2. inspect adjusted vs official metrics
3. inspect currency
4. inspect update timestamp
5. prefer higher-tier source
6. preserve unresolved conflict if not explainable

## 8. Web search behavior

The agent should search iteratively.

Bad:

```
get 6 headlines -> summarize -> decide
```

Good:

```
search earnings
-> open earnings release
-> notice export-control exposure
-> search regulator source
-> open official rule
-> search customer concentration
-> open latest filing
-> reassess thesis
```

The agent is allowed to formulate follow-up questions.

## 9. Browser security

All page content is untrusted.

The browser worker:

- does not receive OpenAI keys unless technically unavoidable
- does not receive Toss secrets
- does not receive SSH keys
- does not mount the Docker socket
- does not expose unrestricted filesystem access
- does not run arbitrary shell commands requested by webpages
- restricts downloads to a sandbox directory
- enforces timeouts
- records visited URLs and actions
- is read-only: only GET/HEAD requests leave the page, POST only to explicitly allowlisted hosts, and WebSockets never, so an injected instruction cannot submit a form or send data outward

Prompt injection inside web content must be treated as content, not instruction.

## 10. Python calculation tool

Prefer deterministic Python implementations for:

- indicators
- regressions
- valuation math
- position sizing
- performance metrics
- dataframe transforms

Prefer deterministic library/code implementations for repeatable finance calculations instead of asking the LLM to perform arithmetic in prose.

v0.1 exposes a restricted evidence-based operation set (`ratio`, `multiply`, `add`, `subtract`, ATR offsets) and tested indicator/sizing functions. It does not expose arbitrary Python source, `eval`, a host shell or dataframe/file access. Derived facts link input evidence IDs, operation and explicit assumptions. Recent observed highs/lows are labeled as observations rather than automatically asserted support/resistance.

## 11. PDFs

PDFs may contain filings, presentations, reports, and tables.

The system should preserve:

- original URL/file
- page references where possible
- extraction method
- publication date
- document identity

Important numeric tables should be parsed carefully and validated against document labels/units.

## 12. Research memory

The system should maintain two different memories:

### Factual/evidence history

Historical evidence records and reports.

### Thesis state

What the system previously believed and why.

A new run should be able to answer:

- what changed?
- which previous assumption failed?
- which new evidence caused the rating change?
- which old evidence is now stale?

## 13. Tool observability

Persist per run:

- tool name
- arguments (redacting secrets)
- start/end time
- status
- error class
- source URL
- evidence IDs produced
- retry/fallback path

This allows diagnosis of poor research quality without guessing. Read-only `evidence_read` views are traced with the evidence they viewed and produce no new evidence.

## 14. Implemented contracts and remaining data gaps

`market_identity`, `market_quote`, `market_ohlcv`, `portfolio_read`, `fees_read`, `filings_read`, `financials_read`, `fund_holdings_read`, `web_search`, `web_read`, `browser_open`, `browser_action`, `extract_fact` and `calculate` return typed EvidenceRecords. `evidence_read` returns a read-only view of stored evidence (`part=text` with search/pagination, `facts` filtered by name, or `payload`). Peer tickers are allowed for comparable research; trade validation always checks the primary security.

`web_search` accepts `freshness` (`pd`/`pw`/`pm`/`py` or `YYYY-MM-DDtoYYYY-MM-DD`), one `domain`, `country`, `search_lang`, `offset` (0–9) and `topic` (`general`/`news`/`finance`). Each provider maps what it supports: Tavily uses `time_range` or a date range, `include_domains`, country names (general topic only), `language` and `topic`, and has no offset. Brave uses `site:`, `country`, `search_lang` and `offset`, and has no topic. Options a provider cannot apply are recorded as `unsupported_options` in the evidence, not silently dropped. `web_read` extracts the main content of an HTML page, not the whole page. It removes navigation, headers, footers, consent banners, promos and forms, prefers `main`/`article` or the densest paragraph container, and keeps table rows on one line. Publication time comes from JSON-LD `datePublished`, then article/Dublin Core meta tags, then `<time>`. Only timezone-aware, non-future times are accepted. SEC hosts receive the configured SEC User-Agent; other hosts never do.

The model context is an **evidence index**, not the evidence. For every record it shows the ID, type, source, timestamps, freshness, up to 40 structured facts, a 300-character snippet and a compact payload summary without page text, candles or browser element lists. A tool call returns the index entry plus a 4,000-character preview (browser calls also return element refs and tabs). Older tool outputs in a stage's conversation are condensed to their evidence ID, and only the latest screenshot is kept. The previous report is passed as a summary. Anything relied upon is read on demand with `evidence_read`.

The director produces analysis sections and a first sufficiency assessment. Bull, bear, rebuttals, the research manager, three risk perspectives and the premortem each produce structured findings and can obtain additional evidence. Gaps carry a proposed severity, but only the research manager (blocking gaps plus an explicit `evidence_sufficient`) and the PM make them binding. The PM sees every open gap and produces the six-level rating with enumerated entry and holder actions. Rejected numeric plans and unsupported claims are retained in the audit trail; two correction opportunities precede a deferred decision.

Derived evidence (technical indicators, calculations, extracted facts, quotations, screens) inherits the freshness window of its most time-sensitive input: the oldest input observation and the earliest input deadline. Daily OHLCV is stamped at the last completed session's close and expires after `ohlcv_max_age_seconds` (5 days). A precise plan level is valid only if its record and every recorded input are fresh at the decision cutoff (`LEVEL_STALE` otherwise), so recalculating an old indicator does not make it current.

The implementation reads the canonical Korean research standard into its policy prompt. External content remains untrusted data. Important conflicting facts and uncertain document labels must be resolved by source review or reported as material gaps. No consensus/forecast/earnings-calendar provider is fabricated; the researcher seeks original sources or reports unavailability.

Official XBRL facts preserve metric tags, units, accounting periods and accession references for both `us-gaap` and `ifrs-full` filers (20-F/40-F/6-K). IFRS facts keep their reporting currency (for example TWD), and a currency-mismatched calculation is refused rather than silently converted. Shares outstanding fall back to `dei:EntityCommonStockSharesOutstanding`. Company-specific (custom) XBRL tags are not in SEC company facts and still require filing review. Depositary receipts carry a warning that per-share comparisons need the ADS ratio.

ETFs follow a separate path: `fund_holdings_read` maps the ticker to its SEC series (`company_tickers_mf.json`) and parses the series' latest N-PORT filing (net assets, holdings count, top-10 and largest-holding weights, asset-category and country weights; `pctVal` is already a percent) with the summary prospectus (497K) link. Issuer financial statements are not required for an ETF; the N-PORT holdings are its official structured basis.

Image-only PDFs remain a limitation. HTML/PDF body retention is restricted by approved host; other sources preserve hashes, metadata, page numbers and snippets. Screenshots can be used by the model without keeping image files. See [retention and replay](RUNTIME.md#7-보존과-replay).
