# Integration Review

## Merged Work

- PR #1 (`UI v1`) originally introduced FastAPI, watchlist, holdings, stock
  detail, fundamentals, analyst data, earnings, options, news, and static UI.
- PR #2 (`Add market analysis indicators`) is present: full daily bars, shared
  quote resolution, moving averages, changes, rolling levels, and volume metrics.
- PR #3 was closed after its merge commit and portfolio metadata were incorporated
  into PR #4.
- PR #4 reconciled the dashboard, CLI, analysis engine, portfolio metadata,
  SQLite user data, and shared API services into `master`.
- The SQLite data-foundation update moved all durable market data into the same
  database, added user ownership and provider-aware bars, and removed legacy
  research persistence and direct-route provider calls.
- The consolidation update restored risk analytics, local search, EOD screening,
  and transient company research through shared services and a bounded cache.

## Consolidated Boundaries

- `analysis.py` contains pure Polars portfolio, return, risk, attribution,
  correlation, and screener calculations.
- `portfolio.py` owns shared stored-first portfolio analysis orchestration used by
  both CLI and dashboard.
- `market_analysis.py` owns shared technical-analysis orchestration used by CLI
  and API.
- `database.py` owns shared SQLite connections and fresh-schema initialization.
- `market_store.py` owns securities, universes, and provider-aware daily bars.
- `quotes.py` owns stored daily-bar resolution and temporary missing-only
  downloads until scheduled ingestion is implemented.
- `research.py` owns injectable provider calls, JSON-safe conversion, bounded
  TTL caching, single-flight requests, and stale fallback.
- `dashboard/services.py` adapts shared analysis results to web response shapes.
- `user_data.py` owns user-scoped SQLite positions and watchlists.
- `dashboard/server.py` is a thin HTTP/static-file adapter.

## Intentionally Preserved Differences

| Area | CLI / core | Web dashboard | Review question |
| --- | --- | --- | --- |
| Portfolio source | Reads user-scoped SQLite lots | Reads user-scoped SQLite lots | Authentication will select the user in API v1 |
| Position detail | Preserves individual lots internally; terminal output remains position-oriented | Preserves account, asset class, sector, acquired date, and lot IDs | Should CLI expose lot and allocation views? |
| Market prices | Provider-separated SQLite daily bars | Same EOD bars for watchlists, holdings, and indicators | Scheduled freshness belongs to Phase 3 |
| History | Daily analysis history through Polars | Daily chart history through the same service | Unified in this change |
| Indicators | User-supplied windows and price basis | Stock chart currently uses 20/50/200 adjusted averages | Should web controls expose windows and price selection? |
| Output contract | Polars frames formatted for terminal | JSON with camelCase fields for browser compatibility | Keep camelCase or standardize API fields later? |

The remaining differences are presentation choices, not separate data sources.
CSV and Parquet are absent from the runtime workflow. SQLite is shared by the
CLI, dashboard, and analysis services.
