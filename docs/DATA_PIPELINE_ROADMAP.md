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
data, universe membership, and ingestion history. Polars remains the analysis
engine. FastAPI is the primary product contract for the web UI, CLI, and custom
consumers.

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

Data-provider requests must not occur unexpectedly while serving analytical API
requests. Ingestion and analysis are separate operations.

## Initial Decisions

- Support multiple users while keeping initial authentication simple.
- Permanently store end-of-day market data only.
- Backfill ten years of daily history for tracked equities.
- Continue using yfinance behind a replaceable provider adapter.
- Use one SQLite database at `data/quant.db` for application and market data.
- Defer persistent intraday data, fundamentals, analyst ratings, options, and
  news until the portfolio and technical core is reliable.

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

A ten-year backfill is one logical job executed in batches of approximately 25
to 100 symbols. Each batch requests the full date range, validates independently,
and commits separately. Failed batches are split before retrying. Progress is
persisted so interrupted jobs can resume.

Historical data should not be fetched one day or month at a time unless a
provider requires it.

### Incremental Policy

Run synchronization once per trading day after provider data has settled,
approximately 6 to 8 PM Eastern. Each run begins around ten trading sessions
before the latest stored session so provider corrections are captured. The
operation must be idempotent and catch up automatically after downtime.

Adding a previously untracked security stores the user action immediately and
creates a durable synchronization request. Historical downloads happen outside
the API request.

### Retention Policy

- Retain acquired daily OHLCV and adjusted prices indefinitely.
- Retain portfolio and cash-flow records indefinitely.
- Retain universe membership observations indefinitely.
- Retain ingestion audit records and unresolved data-quality issues.
- Do not persist intraday quotes or bars initially.

## SQLite Data Model

| Table | Purpose |
| --- | --- |
| `users` | Application identities |
| `api_tokens` | Hashed personal access tokens |
| `positions` | Current portfolio entries owned by a user |
| `watchlist` | User-owned tracked securities |
| `securities` | Stable internal security identities |
| `provider_symbols` | Provider-specific symbol mappings |
| `daily_bars` | Provider-attributed daily OHLCV observations |
| `universes` | Configured market universes |
| `universe_memberships` | Observed universe membership history |
| `ingestion_runs` | Synchronization audit records |
| `ingestion_issues` | Rejected or suspicious observations |
| `sync_requests` | Durable work for newly tracked securities |

The daily-bar identity is `security_id + session_date + provider`. Provider is
part of the key so a new source cannot silently overwrite existing history.
Company metadata belongs to `securities`, not every bar. Latest price is derived
from the latest raw close.

## Validation

Reject batches containing unknown symbol mappings, duplicate canonical keys,
invalid dates, nonpositive OHLC prices, negative volume, impossible OHLC
relationships, or missing required fields.

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

Personal access tokens are the initial authentication mechanism for the CLI and
custom consumers. Tokens require high entropy, user ownership, optional scopes,
expiration, revocation, a visible identifier prefix, and a last-used timestamp.
Only token hashes are stored. Tokens are sent in the `Authorization: Bearer`
header over HTTPS.

Browser authentication should move to OIDC and secure HTTP-only sessions rather
than storing long-lived personal tokens in browser storage. Cloudflare Tunnel is
a simple home-server exposure option; Caddy with automatic HTTPS is suitable for
a VPS.

## API Contract

Introduce a versioned `/api/v1` contract before encouraging custom consumers.
Core resources include:

```text
GET    /api/v1/me
GET    /api/v1/watchlist
POST   /api/v1/watchlist
DELETE /api/v1/watchlist/{symbol}

GET    /api/v1/portfolio/positions
POST   /api/v1/portfolio/positions
DELETE /api/v1/portfolio/positions/{id}
GET    /api/v1/portfolio/summary
GET    /api/v1/portfolio/history

GET    /api/v1/securities/{symbol}
GET    /api/v1/securities/{symbol}/bars
GET    /api/v1/securities/{symbol}/technicals

GET    /api/v1/market/overview
GET    /api/v1/market/breadth
GET    /api/v1/data/status
```

Responses identify their observation date, generation time, provider, price
basis, freshness, and warnings. Pydantic models define request and response
contracts. Authenticated user identity is never accepted from a request body.

The user-facing CLI and web application are API consumers. Server-local operator
commands own schema migration, synchronization, backup, and user administration.

## Delivery Roadmap

### Phase 1: SQLite Foundation

1. Introduce shared SQLite connections and schema migrations.
2. Add users and API-token storage.
3. Add user ownership to positions and watchlists.
4. Assign existing local data to a bootstrap development user.
5. Add securities, provider symbols, daily bars, ingestion records, universes,
   issues, and synchronization requests.
6. Add a SQLite market-data repository returning Polars DataFrames.
7. Test initialization and migrations with temporary databases.

### Phase 2: Replace Parquet

1. Replace Parquet operations with SQLite repositories.
2. Replace separate cache paths with database/repository dependencies.
3. Remove cache-specific CLI options and constants.
4. Replace cache resolution with explicit SQLite coverage checks.
5. Delete the local Parquet files without importing them.
6. Update tests and project documentation.

### Phase 3: Provider And Synchronization

1. Define a provider protocol returning canonical Polars frames.
2. Place yfinance behind a Yahoo provider implementation.
3. Build batched ten-year backfills and incremental EOD synchronization.
4. Add validation, audit records, and resumable sync requests.
5. Add `quant data sync`, `quant data status`, and `quant data check`.

### Phase 4: API v1

1. Add bearer-token authentication and user scoping.
2. Add typed, versioned API endpoints.
3. Serve daily history and technical analysis only from SQLite.
4. Return freshness and provenance metadata.
5. Convert the web UI and user-facing CLI to API consumers.

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
