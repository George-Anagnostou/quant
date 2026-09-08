# Repository Guide

## Commands

- Use Python 3.14 through `uv`; install/sync with `uv sync`.
- Run all tests with `PYTHONDONTWRITEBYTECODE=1 uv run python -m unittest discover -s tests`.
- Run one test with `PYTHONDONTWRITEBYTECODE=1 uv run python -m unittest tests.test_market_data.LatestMarketDataTests.test_downloads_all_symbols_in_one_batch`.
- `uv run quant serve` starts a background ingestion worker and serves the webpage and API at `http://127.0.0.1:8001`; add `--no-sync` for tests or offline use and `--database PATH` for an isolated database.
- `uv run quant query ...` is the machine-readable `/api/alpha` client. It must not read SQLite, call providers, or recalculate financial results locally.
- `uv run quant data status` queries stored coverage through the running API. Provider synchronization belongs to the background ingestion worker, not API read requests. Explicit POST requests can enqueue durable work.
- No lint, formatter, or typecheck command is configured; do not claim those checks ran.

## Boundaries

- The sole `quant` console script resolves to `main()` in `src/quant/cli.py`; `src/quant/__init__.py` has no CLI logic.
- `src/quant/market_data.py` owns constituent discovery, Yahoo batching, symbol normalization (`BRK.B` -> `BRK-B`), and conversion to Polars.
- `src/quant/database.py` owns SQLite connections, versioned non-destructive migrations, and verified online backups. `src/quant/market_store.py` owns security, universe, and provider-separated daily-bar persistence and returns Polars frames.
- `src/quant/analysis.py` contains pure Polars calculations, including return, risk, correlation, attribution, and EOD screener math. `src/quant/portfolio.py` owns stored-first portfolio quote resolution and shared current-position analysis.
- `src/quant/quotes.py` is the shared stored-first resolver used by portfolio and market analysis. `src/quant/market_analysis.py` owns market-analysis orchestration, not indicator math.
- `src/quant/dashboard/services.py` adapts shared Polars analysis for the API. `src/quant/user_data.py` owns user-scoped positions and watchlists; FastAPI routes must not read or write persistence directly.
- `src/quant/research.py` is the injectable yfinance research boundary. Legacy Yahoo research uses a bounded in-memory TTL cache with explicit stale metadata. `fundamentals.py` owns explicit SEC/evidence ingestion; preserved evidence is separate from the transient cache.
- `src/quant/dashboard/server.py` is a thin FastAPI/static adapter. Research and analytical routes delegate to `dashboard/services.py`; routes must not call yfinance, SQLite, or Polars calculations directly.
- Keep project-facing data as `polars.DataFrame`. `yfinance` returns pandas internally, but pandas must remain confined to that dependency boundary; do not add pandas imports or direct pandas dependencies.
- Unit tests must not call Wikipedia or Yahoo. Mock the `yfinance` boundary and use temporary paths for storage tests.

## Data

- `data/*.db*` and `__pycache__/` are generated and ignored; do not commit them.
- `data/quant.db` is the sole source of truth for user and EOD market data, shared by the CLI and dashboard.
- The current API uses the bootstrap `local-admin` identity. Repositories are user-scoped; request authentication is planned separately.

## Research engine

- `ingestion.py` owns the provider protocol, single-writer file lock, durable jobs,
  retry/recovery, correction reconciliation, and scheduling; `startup_sync.py` is
  a compatibility wrapper. `calendars.py` owns the versioned, bounded US calendar.
- `record_store.py` owns immutable user-scoped records and write audits.
- `workflows.py` owns snapshot reviews, frozen research contexts, and report evidence
  validation. `ledger.py` owns reconciled account history and performance conventions.
- `discovery.py` owns observed-universe discovery and bounded research experiments.
  `platform_service.py` adapts application capabilities for `dashboard/api_research.py`.
- `readiness.py` checks stored prerequisites for dated reviews without performing
  financial calculations. Keep capability statuses separate and do not infer
  missing sessions for unverified calendars. Reads must never enqueue repairs.
- New alpha rates and weights are fractions. Preserve backcast/actual-performance,
  availability/retrieval-time, and current/historical-universe distinctions.
- Frozen databases in `data/artifacts/` are generated evidence copies. Do not commit
  them or modify them after publication. Replay requires the retained method source.
- Schema v1 upgrades automatically with a verified backup. Never delete personal
  data to apply a supported migration. Do not run provider-backed work in unit tests.
