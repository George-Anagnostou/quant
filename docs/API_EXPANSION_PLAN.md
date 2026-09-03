# Agent-Oriented API Expansion Plan

## Outcome

Expand `/api/v1` into a predictable financial-data interface that an agent can
discover, query, validate, and compose without reading implementation code.
The API remains local, stored-first, typed, and side-effect-free for reads.
Operator ingestion stays outside request-time analytics.

## Contract Rules

Every endpoint must follow these rules before new resource coverage is added:

- Return the existing `data`, `meta`, and `warnings` envelope. Errors retain a
  stable machine code, human message, HTTP status, and structured field errors.
- Identify observation time, generation time, provider, price basis, units,
  freshness, and partial-data conditions. Never silently interpolate bars.
- Use ISO 8601 dates and UTC timestamps. Financial rates and returns are
  fractions; ranking scores explicitly remain on a 0-100 scale.
- Reject unknown and repeated parameters. Normalize symbols once at the API
  boundary and preserve deterministic request order where it is meaningful.
- Bound symbols, windows, date ranges, rows, response bytes, and computation.
  Large collections use stable sorting and opaque cursor pagination.
- A read endpoint never contacts a provider or writes SQLite. Live research
  endpoints are explicitly labeled as transient and report cache/provider state.
- OpenAPI models, operation IDs, parameter descriptions, examples, null
  semantics, warning codes, and error responses are part of the contract.
- Add contract tests for empty, partial, stale, malformed, oversized, and
  unavailable-data cases before exposing each endpoint through `quant-api`.

## Delivery Sequence

### 1. Data discoverability and quality

These endpoints make later analytics explainable and should ship first.

```text
GET /capabilities
GET /calendars/{calendar}/sessions
GET /universes
GET /universes/{universe}/members
GET /universes/{universe}/history
GET /data/status
GET /data/gaps
GET /data/ingestion-runs/{id}
```

`/capabilities` returns supported providers, price bases, periods, indicators,
calendars, universes, limits, and optional features. `/data/status` gains
coverage ratios and latest completed session. `/data/gaps` distinguishes:

- incomplete OHLCV observations;
- internal missing sessions inside a security's observed lifetime;
- unavailable pre-listing or post-delisting history;
- provider omissions after retry;
- incomplete universe cross-sections.

Gap records include symbol, expected session, reason, first/last detection,
retry count, and resolution status. This requires durable ingestion-run and
data-issue tables in Phase 3; inferred gap results can be read-only until then.

### 2. Efficient market-data retrieval

```text
GET /securities/{symbol}
GET /securities/{symbol}/bars
GET /market/quotes
GET /market/bars
GET /market/returns
```

Add split/dividend metadata and canonical provider-symbol mappings to the
security resource. Preserve the single-symbol bars endpoint and add a bounded
multi-symbol endpoint with `start`, `end`, `priceBasis`, `fields`, `limit`, and
`cursor`. Default projections stay small. A `latestOnly` or `tail` option avoids
forcing agents to download a full technical series for one current value.

Bulk history may later support NDJSON, but JSON remains the required baseline.
CSV is an export format, not the canonical agent contract.

### 3. Composable analytics

```text
GET /securities/{symbol}/technicals
GET /market/technicals
GET /market/risk
GET /market/correlations
GET /market/screener
GET /market/breadth
GET /market/movers
GET /market/overview
```

Extend technicals through a registry rather than adding one endpoint per
indicator. Initial additions are Bollinger Bands, exponential averages, RSI,
ATR, realized volatility, drawdown, relative strength, and volume z-score.
Requests specify indicator parameters explicitly; responses echo the normalized
specification and computation convention.

Separate correlations from risk so agents can request a compact matrix or
long-form pairs. Screeners accept explicit sort, direction, filters, and limit,
and return the scoring methodology version. Breadth and movers always identify
the universe snapshot used.

### 4. Portfolio resources after authentication

```text
GET    /me
GET    /portfolio/positions
POST   /portfolio/positions
DELETE /portfolio/positions/{id}
GET    /portfolio/summary
GET    /portfolio/history
GET    /portfolio/risk
GET    /portfolio/exposures
GET    /portfolio/scenarios
```

Do not add write routes until authentication selects the user. Writes use
idempotency keys, optimistic concurrency, finite numeric validation, and an
audit trail. Portfolio history must distinguish actual transactions from the
existing current-holdings backcast. Scenario inputs are bounded and never
persisted unless explicitly saved through a separate resource.

### 5. Explicit transient research

```text
GET /research/{symbol}/profile
GET /research/{symbol}/fundamentals
GET /research/{symbol}/estimates
GET /research/{symbol}/options
GET /research/{symbol}/news
```

These routes may contact a provider. Their metadata must state `source=live` or
`source=cache`, retrieval time, cache age, stale-fallback use, and provider
errors. They are excluded from stored-only batch analytics.

## CLI Shape

Mirror API resources without inventing CLI-only calculations:

```text
quant-api capabilities
quant-api calendar sessions XNYS --start ... --end ...
quant-api universe members sp500 --as-of ...
quant-api gaps --universe sp500 --reason provider_omission
quant-api bars AAPL MSFT --start ... --fields date,adjustedClose
quant-api technical AAPL --indicator bollinger:window=20,stddev=2 --tail 5
quant-api correlations AAPL MSFT NVDA --period 1y --format matrix
```

Compact JSON remains the default. `--pretty` is presentation-only. Future
`--select` support may project response fields but must not change server-side
financial semantics. Exit codes remain stable across commands.

## Compatibility and observability

- Add a contract snapshot test for OpenAPI and explicit compatibility review
  for schema changes. New optional fields are additive; renames and unit changes
  require a new API version.
- Add request IDs and structured server logs without putting paths, credentials,
  or raw provider errors into client responses.
- Record endpoint latency, response size, rows returned, freshness, warnings,
  and error code. Do not record portfolio payloads or other user data.
- Support `ETag` and conditional GET for immutable historical ranges and
  universe snapshots after response semantics stabilize.

## Near-term implementation slice

1. Add durable ingestion runs and structured data issues.
2. Implement `/capabilities`, calendar sessions, universe membership, and
   `/data/gaps`.
3. Add cursor/page primitives shared by bars, issues, and universe history.
4. Add multi-symbol bars with field projection and strict byte/row limits.
5. Add an indicator registry and Bollinger Bands with `tail` support.
6. Expose each resource through `quant-api` and add end-to-end contract tests.

This slice directly removes the ad hoc SQL and `jq` calculations currently
needed for coverage audits, rolling volume analysis, and Bollinger Bands.
