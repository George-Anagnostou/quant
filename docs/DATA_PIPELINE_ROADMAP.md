# Data Pipeline Roadmap

## Product Direction

Quant is a local-first, multi-user US equity research and portfolio intelligence
platform. Its near-term purpose is to collect reliable market and portfolio
history, analyze companies and the broader market, and explain how a portfolio
is behaving within that context.

The long-term goal is an evidence-backed research assistant that continuously
surfaces investment and trade ideas. Every future idea should identify its data,
observation time, reasoning, risks, and historical evidence. Temporal
correctness and provenance therefore take priority over adding more indicators.

## Architecture

SQLite is the authoritative store for users, portfolios, watchlists, market
data, and universe membership. Polars remains the analysis engine. FastAPI is
the primary product contract for the web UI, CLI, and custom consumers. Durable
ingestion state will be added with the synchronization pipeline rather than
speculatively stored before that pipeline exists.

```text
yfinance / future providers
          |
Provider adapter
          |
Sync planner and validator
          |
       SQLite
          |
Repository and analysis services
          |
       FastAPI
       /     \
    Web UI   CLI / custom clients
```

Durable data-provider requests should not occur unexpectedly while serving
analytical API requests. The current missing-history request bridge is temporary.
Explicit research endpoints may query their provider through the bounded
in-memory cache, but do not persist those responses.

## Initial Decisions

- Keep repositories user-scoped and add Clerk authentication before exposing
  the application beyond loopback.
- Permanently store end-of-day market data only.
- Use January 1, 2025 as the current operational history horizon. Keep the
  synchronization design horizon-configurable so it can expand without a
  schema change.
- Continue using yfinance behind a replaceable provider adapter.
- Use one SQLite database at `data/quant.db` for application and market data.
- Keep intraday data, fundamentals, analyst ratings, options, and news transient.
  Expose them through an injectable provider and bounded request cache without
  adding persistence or a synchronization pipeline prematurely.

## Current Analysis Conventions

- Returns are simple returns represented as fractions.
- Annualized volatility, alpha, Sharpe, and Sortino use 252 trading sessions.
- Sharpe and Sortino currently use a zero risk-free rate.
- Benchmark alpha and beta use only intersecting asset and benchmark dates.
- Historical portfolio values assume current shares were held throughout the
  selected period and retain only complete common-price dates.
- Endpoint contributions exactly attribute the fixed-share return between the
  first and last common dates.
- Variance-risk contributions are a static-weight covariance approximation,
  using latest common-date weights and ignoring weight drift and rebalancing.
- The EOD screener maps momentum, return, risk, and trend inputs to documented
  0-100 buckets. Its composite is penalized by the square root of input coverage.

Research cache entries are bounded by LRU capacity and use per-category TTLs.
Concurrent requests for the same key share one provider call. Expired entries
may be returned only when a provider operation fails; validation errors are not
masked. This cache is process-local and is not a source of truth.

## Market Data Pipeline

Each synchronization cycle follows the same deterministic stages:

1. Determine the tracked universe from all portfolios, watchlists, configured
   benchmarks, and the current S&P 500 membership.
2. Inspect local coverage and identify missing symbols, date ranges, and trading
   sessions.
3. Plan provider requests for backfills, new sessions, and a recent correction
   overlap.
4. Download outside any database transaction.
5. Normalize provider symbols, dates, columns, nulls, and numeric types into the
   canonical Quant schema.
6. Validate structure, row relationships, and expected session coverage.
7. Upsert manageable batches transactionally and record the ingestion result.
8. Verify stored coverage and record unresolved gaps or suspicious data.
9. Serve stored data through repositories and analysis services.

### Backfill Policy

A configured-horizon backfill is one logical job executed in batches of
approximately 25 to 100 symbols. The current horizon starts on January 1, 2025.
Each batch requests the full date range, validates independently, and commits
separately. Failed batches are split before retrying. Progress is persisted so
interrupted jobs can resume.

Historical data should not be fetched one day or month at a time unless a
provider requires it.

### Incremental Policy

Run synchronization once per trading day after provider data has settled,
approximately 6 to 8 PM Eastern. Each run begins around ten trading sessions
before the latest stored session so provider corrections are captured. The
operation must be idempotent and catch up automatically after downtime.

The future synchronization pipeline will enqueue durable work when a previously
untracked security is added. Historical downloads will then happen outside the
API request.

### Retention Policy

- Retain acquired daily OHLCV and adjusted prices indefinitely.
- Retain portfolio and cash-flow records indefinitely.
- Retain universe membership observations indefinitely.
- Retain future ingestion audit records and unresolved data-quality issues.
- Do not persist intraday quotes or bars initially.

## SQLite Data Model

| Table | Purpose |
| --- | --- |
| `users` | Application identities |
| `positions` | Current portfolio entries owned by a user |
| `watchlist` | User-owned tracked securities |
| `securities` | Stable internal security identities |
| `daily_bars` | Provider-attributed daily OHLCV observations |
| `universes` | Configured market universes |
| `universe_memberships` | Observed universe membership history |

The daily-bar identity is `security_id + session_date + provider`. Provider is
part of the key so a new source cannot silently overwrite existing history.
Company metadata belongs to `securities`, not every bar. Latest price is derived
from the latest raw close. Provider-symbol mappings, ingestion runs, issues, and
sync requests should be introduced with Phase 3, when code will consume them.

## Validation

Reject batches containing unsupported symbols, duplicate canonical keys, invalid
dates, nonpositive OHLC prices, negative volume, impossible OHLC relationships,
or missing required fields.

Record warnings for extreme returns, unusual volume, adjustment discontinuities,
missing sessions, and material differences between providers. Statistical
anomalies are not automatically rejected because corporate actions can produce
legitimate discontinuities.

Never interpolate missing OHLCV observations. Missing data remains visible to
analysis and API consumers.

## Provider Changes

Provider changes use a controlled comparison:

1. Add a provider adapter and symbol mappings.
2. Backfill an overlapping period under the new provider identity.
3. Compare session coverage, OHLCV, adjustments, and corporate actions.
4. Investigate differences outside configured tolerances.
5. Select the active provider explicitly.
6. Preserve the old provider observations.

Analysis must not silently blend providers in one return series.

## SQLite Operations

Use WAL mode, foreign keys, a busy timeout, short transactions, batched writes,
and indexes matching security/date access patterns. Provider calls occur before
opening write transactions. Run `ANALYZE` and `PRAGMA optimize` after major
backfills. Use SQLite's online backup API and regularly verify restoration and
database integrity.

Do not place the database on a network filesystem or run several ingestion
workers against it.

Monitor database size, row counts, API latency, query duration, ingestion
duration, provider failures, missing sessions, lock errors, WAL growth, backup
duration, and freshness.

### PostgreSQL Triggers

Consider PostgreSQL when Quant needs multiple API instances, frequent concurrent
writes, a database on another host, high availability, or database-level user
isolation. Row count by itself is not a migration trigger.

### Parquet Or DuckDB Triggers

Consider a columnar analytical layer when repeated full-universe scans involve
tens of millions of rows, database-to-Polars transfer dominates execution, or
old immutable history becomes expensive to query. SQLite can remain authoritative
for users, metadata, jobs, and recent data in a future hybrid design.

## Authentication

The current API uses the bootstrap `local-admin` identity and must remain bound
to loopback. Clerk authentication is planned before network exposure. FastAPI
will derive repository ownership from the authenticated request rather than
accepting user identity in request bodies.

## API Contract

The initial stored-only read contract and machine-readable `quant query` client
are implemented. The next contract increments are specified in
[`API_EXPANSION_PLAN.md`](API_EXPANSION_PLAN.md).

Evolve the versioned `/api/alpha` contract before encouraging custom consumers.
Core resources include:

```text
GET    /api/alpha/me
GET    /api/alpha/watchlist
POST   /api/alpha/watchlist
DELETE /api/alpha/watchlist/{symbol}

GET    /api/alpha/portfolio/positions
POST   /api/alpha/portfolio/positions
DELETE /api/alpha/portfolio/positions/{id}
GET    /api/alpha/portfolio/summary
GET    /api/alpha/portfolio/history

GET    /api/alpha/securities/{symbol}
GET    /api/alpha/securities/{symbol}/bars
GET    /api/alpha/securities/{symbol}/technicals

GET    /api/alpha/market/overview
GET    /api/alpha/market/breadth
GET    /api/alpha/data/status
```

Responses identify their observation date, generation time, provider, price
basis, freshness, and warnings. Pydantic models define request and response
contracts. Authenticated user identity is never accepted from a request body.

The user-facing CLI and web application are API consumers. Server-local operator
commands own schema migration, synchronization, backup, and user administration.

## Delivery Roadmap

Phases 1 and 2 are implemented on the SQLite data-foundation branch. Missing
history still uses a temporary request-time download bridge; Phase 3 replaces
that bridge with explicit synchronization and validation.

### Phase 1: SQLite Foundation

1. Introduce shared SQLite connections and fresh-schema initialization.
2. Add a bootstrap development user and user ownership to positions and
   watchlists.
3. Add securities, provider-separated daily bars, and dated universes.
4. Add a SQLite market-data repository returning Polars DataFrames.
5. Test initialization and concurrency with temporary databases.

### Phase 2: Replace Parquet

1. Replace Parquet operations with SQLite repositories.
2. Replace separate cache paths with database/repository dependencies.
3. Remove cache-specific CLI options and constants.
4. Replace cache resolution with explicit SQLite coverage checks.
5. Delete the local Parquet files without importing them.
6. Update tests and project documentation.

### Phase 3: Provider And Synchronization (In Progress)

1. Define a provider protocol returning canonical Polars frames.
2. Place yfinance behind a Yahoo provider implementation.
3. Replace the manual January 2025 backfill with resumable, configurable-horizon
   backfills and incremental EOD synchronization.
4. Add validation, audit records, and resumable sync requests.
5. Add `quant data sync`, `quant data status`, and `quant data check`.

### Phase 4: API alpha (Read Contract Implemented)

1. Add Clerk authentication to the existing user-scoped repositories.
2. Extend the typed, versioned endpoints using the API expansion plan.
3. Continue serving daily history and technical analysis only from SQLite.
4. Preserve freshness, provenance, warnings, and deterministic errors.
5. Convert the web UI to the alpha contract; keep `quant query` as the automation
   client and `quant data` as the local operator surface.

### Phase 5: Background Operation

1. Run FastAPI and one small synchronization worker as supervised services.
2. Use SQLite synchronization requests instead of Redis or Celery.
3. Schedule EOD and weekly-universe jobs.
4. Add freshness health checks, automated backups, and restore verification.

### Phase 6: Core Analysis

1. Add a portfolio transaction ledger and derive positions.
2. Add historical portfolio valuation and cash-flow-adjusted returns.
3. Add benchmark comparison, drawdown, contribution, volatility,
   concentration, and correlation.
4. Add market breadth, regime, and relative-strength analysis.
