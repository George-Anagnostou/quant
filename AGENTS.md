# Repository Guide

## Commands

- Use Python 3.14 through `uv`; install/sync with `uv sync`.
- Run all tests with `PYTHONDONTWRITEBYTECODE=1 uv run python -m unittest discover -s tests`.
- Run one test with `PYTHONDONTWRITEBYTECODE=1 uv run python -m unittest tests.test_market_data.LatestMarketDataTests.test_downloads_all_symbols_in_one_batch`.
- `uv run quant index` reads S&P 500 bars from `data/quant.db`; if absent, it downloads data and prints roughly 500 rows. Add `--refresh` for Wikipedia/Yahoo requests or `--database PATH` for an isolated database.
- Market analysis requires explicit choices: `uv run quant market AAPL MSFT --windows 5 20 --price adjusted`; replace tickers with `--index` for the stored S&P 500 universe.
- `uv run quant-dashboard` serves the FastAPI dashboard at `http://127.0.0.1:8001`; routes under `/api/*` must delegate portfolio and indicator work to shared services rather than recalculate it.
- No lint, formatter, or typecheck command is configured; do not claim those checks ran.

## Boundaries

- The `quant` console script resolves to `main()` in `src/quant/cli.py`; `src/quant/__init__.py` has no CLI logic.
- `src/quant/market_data.py` owns constituent discovery, Yahoo batching, symbol normalization (`BRK.B` -> `BRK-B`), and conversion to Polars.
- `src/quant/database.py` owns SQLite connections and fresh-schema initialization. `src/quant/market_store.py` owns security, universe, and provider-separated daily-bar persistence and returns Polars frames.
- `src/quant/analysis.py` contains pure Polars calculations, including return, risk, correlation, attribution, and EOD screener math. `src/quant/portfolio.py` owns stored-first portfolio quote resolution and shared current-position analysis.
- `src/quant/quotes.py` is the shared stored-first resolver used by portfolio and market analysis. `src/quant/market_analysis.py` owns market-analysis orchestration, not indicator math.
- `src/quant/dashboard/services.py` adapts shared Polars analysis for the API. `src/quant/user_data.py` owns user-scoped positions and watchlists; FastAPI routes must not read or write persistence directly.
- `src/quant/research.py` is the injectable yfinance research boundary. Research responses use a bounded in-memory TTL cache with stale fallback and are never persisted.
- `src/quant/dashboard/server.py` is a thin FastAPI/static adapter. Research and analytical routes delegate to `dashboard/services.py`; routes must not call yfinance, SQLite, or Polars calculations directly.
- Keep project-facing data as `polars.DataFrame`. `yfinance` returns pandas internally, but pandas must remain confined to that dependency boundary; do not add pandas imports or direct pandas dependencies.
- Unit tests must not call Wikipedia or Yahoo. Mock the `yfinance` boundary and use temporary paths for storage tests.

## Data

- `data/*.db*` and `__pycache__/` are generated and ignored; do not commit them.
- `data/quant.db` is the sole source of truth for user and EOD market data, shared by the CLI and dashboard.
- The current API uses the bootstrap `local-admin` identity. Repositories are user-scoped; request authentication is planned separately.
