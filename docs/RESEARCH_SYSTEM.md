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

v0.1 implements Brave Search API behind a provider adapter. It is the default discovery mechanism; Chromium is a fallback. Search snippets remain unusable as FACT evidence. OpenAI Responses native function calling chooses queries and follow-up sources autonomously.

The research agent may formulate new searches based on previous findings.

Rules:

- search snippets are discovery aids, not final evidence for material claims
- open the underlying source for important facts
- prefer recent sources for recent claims
- prefer primary sources when available
- search both confirming and disconfirming evidence

### Tier D — Playwright / Chromium

Use only when direct reading is inadequate.

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

This allows diagnosis of poor research quality without guessing.

## 14. Implemented contracts and remaining data gaps

`market_identity`, `market_quote`, `market_ohlcv`, `portfolio_read`, `fees_read`, `filings_read`, `financials_read`, `web_search`, `web_read`, `browser_open`, `browser_action`, `evidence_read`, `extract_fact` and `calculate` return typed EvidenceRecords. Peer tickers are allowed for comparable research; trade validation always checks the primary security.

The director produces analysis sections and explicit sufficiency/gaps. Bull, bear, rebuttals, research manager, three risk perspectives and premortem each produce structured findings and can obtain additional evidence. The PM produces the six-level rating and separate entry/holder actions. Rejected numeric plans and unsupported claims are retained in the audit trail; two correction opportunities precede a deferred decision.

The implementation reads the canonical Korean research standard into its policy prompt. External content remains untrusted data. Citation/numeric checks do not prove semantic entailment of every qualitative statement. Important conflicting facts and uncertain document labels must be resolved by source review or reported as material gaps. No consensus/forecast/earnings-calendar provider is fabricated; the researcher seeks original sources or reports unavailability.

Official XBRL facts preserve metric tags, units, accounting periods and accession references. Custom/IFRS tags and image-only PDFs remain limitations. HTML/PDF body retention is restricted by approved host; other sources preserve hashes, metadata, page numbers and snippets. Screenshots can be used by the model without keeping image files. See [retention and replay](RUNTIME.md#7-보존과-replay).
