# Quant

Quant is a local US equity research server built with SQLite, Polars, and
FastAPI. One `quant` program owns startup synchronization, the database, the
webpage, the API, and its machine-readable CLI client.

## Install

Python 3.14 is pinned in `.python-version`. Install dependencies with:

```sh
uv sync
```

## Server

Start the canonical server:

```sh
uv run quant serve
```

Open http://127.0.0.1:8001.

On startup, Quant refreshes S&P 500 membership, incrementally refreshes existing
symbols with a 14-calendar-day correction overlap, and backfills newly tracked
symbols to January 1, 2025. Provider failures are reported without preventing
the server from serving existing stored data. Disable all startup network work
for tests or offline use with:

```sh
uv run quant serve --no-sync
```

Runtime options include `--host`, `--port`, `--database`, `--horizon`, and
`--batch-size`. The current server is unauthenticated; keep it bound to loopback.

The current API is unauthenticated and always acts as `local-admin`. Keep it
bound to loopback and do not expose it to a LAN or the internet until Clerk
authentication is implemented.

Routes:

- `/` serves the dashboard.
- `/api/*` temporarily provides the webpage's legacy JSON routes.
- `/docs` provides generated API documentation.
- `/api/alpha/*` provides the stored-only automation API.
- `/api/alpha/docs` documents the isolated, typed alpha contract.

The dashboard includes an EOD watchlist, holdings and allocation, local security
search, stored price history, technical and risk analysis, a momentum screener,
and on-demand company research.

FastAPI routes delegate portfolio and technical calculations to shared Polars
services. Durable market data is read from SQLite. Provider synchronization is
an explicit server-startup phase rather than a second program or client-side
calculation path.

The server uses `data/quant.db` as the sole source of truth for users, position
lots, watchlists, securities, universes, and daily market bars.
The schema is user-scoped, while current API requests operate as the bootstrap
`local-admin` user until application authentication is added.

## API

- `/api/health` reports server availability.
- `/api/quotes` and `/api/quote/{symbol}` return stored EOD observations.
- `/api/watchlist` reads and updates the current user's watchlist.
- `/api/holdings` reads and updates portfolio lots.
- `/api/analysis/{symbol}` returns stored technical history.
- `/api/search` searches locally stored securities, with optional remote search.
- `/api/risk` and `/api/portfolio/risk` return stored-EOD risk analysis.
- `/api/screener` scores explicit symbols or the watchlist and holdings.
- `/api/research/{symbol}/*` returns cached profile, analyst, earnings, options,
  news, daily-history, and intraday provider responses.

Risk-return fields are fractions. Annualized metrics use 252 trading sessions
and a zero risk-free rate. Historical portfolio valuation assumes the currently
stored shares were held for the full selected period and uses dates where every
holding has an adjusted close. Return attribution compares the first and last
common dates; variance-risk attribution uses static latest-date weights.

Company research is not durable application data. It is requested explicitly
through an injectable yfinance boundary and held in a bounded in-memory cache.
Profiles are cached for 24 hours, analyst and earnings data for one hour, search
and news for five minutes, options for one minute, and history/intraday data for
30 seconds. Expired values are used as a fallback when the provider is briefly
unavailable.

The stored-only alpha read API is available. Authentication and typed write
contracts remain planned work; see `docs/DATA_PIPELINE_ROADMAP.md`.

## Automation CLI

`quant query` is a thin, machine-readable client for `/api/alpha`. It validates
inputs and renders JSON, while all database access and financial computation
remain on the running server:

```sh
uv run quant query health
uv run quant query status AAPL MSFT
uv run quant query quotes AAPL MSFT
uv run quant query bars AAPL --start 2026-01-01 --limit 100
uv run quant query technical AAPL --windows 20 50 200
uv run quant query risk AAPL MSFT --period 1y --benchmark SPY
uv run quant query screener AAPL MSFT
uv run quant query portfolio
uv run quant query portfolio-risk --period 1y
uv run quant data status AAPL MSFT
```

Responses are deterministic JSON envelopes containing `data`, `meta`, and
`warnings`. Configure the client with `QUANT_API_BASE_URL` and
`QUANT_API_TIMEOUT`, or the corresponding `--base-url` and `--timeout` options.
Global options work before or after the command. Use `--pretty` for indented
output. Success JSON is written to stdout; errors are written to stderr as
`{"ok":false,"error":...}` and use these stable exit codes:

| Code | Meaning |
| ---: | --- |
| 2 | Invalid command or local configuration |
| 3 | Connection or transport failure |
| 4 | Timeout |
| 5 | API 4xx response |
| 6 | API 5xx response |
| 7 | Invalid API response |
| 130 | Interrupted by the user |

The API rejects unknown and repeated query parameters so misspelled agent input
cannot silently change request semantics. It also refuses redirects and the CLI
rejects oversized, malformed, duplicate-key, and non-finite JSON responses.

Alpha GET requests never contact Yahoo or write to SQLite. Missing or incomplete
stored data is reported through partial-data warnings or an
`insufficient_data` error. Data synchronization occurs before the server starts
unless `--no-sync` is set.

All alpha returns, weights, alpha, volatility, and contributions are fractions;
`0.0125` means 1.25%. Screener scores remain on a 0-100 scale. Every market-data
response identifies its observation date, provider, price basis, and a simple
calendar-day freshness status. Data status separately reports close-bar,
complete-OHLCV, and adjusted-close coverage.

## Storage

`data/quant.db` is the only runtime data store. SQLite uses WAL mode, foreign
keys, short transactions, and provider-separated daily bars. Generated database,
WAL, and shared-memory files are ignored by Git.

This pre-release schema starts fresh and does not migrate older local database
layouts. Delete `data/quant.db*` after switching from an earlier branch.

## Tests

```sh
PYTHONDONTWRITEBYTECODE=1 uv run python -m unittest discover -s tests
```

Unit tests must mock Yahoo and Wikipedia boundaries; they should not require
network access.

See `INTEGRATION_REVIEW.md` for the merged PR inventory and current ownership
boundaries. See `docs/DATA_PIPELINE_ROADMAP.md` for planned ingestion,
authentication, API, deployment, and historical-analysis work.
