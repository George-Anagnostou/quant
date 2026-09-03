# Quant

Quant is a US equity portfolio and market-research project built with SQLite and
Polars. It provides a terminal CLI and a FastAPI dashboard backed by shared
market-data, portfolio, and analysis services.

## Install

Python 3.14 is pinned in `.python-version`. Install dependencies with:

```sh
uv sync
```

## CLI

Print the latest stored S&P 500 constituent quotes. An empty database is
populated from Wikipedia and Yahoo Finance on first use:

```sh
uv run quant index
```

Refresh stored index observations from Yahoo Finance:

```sh
uv run quant index --refresh
```

Analyze the SQLite portfolio using stored prices first:

```sh
uv run quant portfolio
```

Use an alternate SQLite database when needed:

```sh
uv run quant portfolio --database /path/to/quant.db
```

The `index` and `market` commands accept the same `--database` option.

Analyze selected symbols with explicit trading-session windows and price basis:

```sh
uv run quant market AAPL MSFT --windows 5 20 --price adjusted
```

Analyze the full stored S&P 500 universe:

```sh
uv run quant market --index --windows 5 20 --price adjusted
```

Market analysis calculates daily price changes, moving averages, rolling
highs/lows, volume averages, and relative volume.

Portfolio positions are stored as individual lots in SQLite. Each lot records
the symbol, quantity, average cost, and optional account, asset class, sector,
and acquired date metadata.

## Dashboard

Run the FastAPI dashboard:

```sh
uv run quant-dashboard
```

Open http://127.0.0.1:8001.

The current API is unauthenticated and always acts as `local-admin`. Keep it
bound to loopback and do not expose it to a LAN or the internet until Clerk
authentication is implemented.

Routes:

- `/` serves the dashboard.
- `/api/*` provides JSON APIs.
- `/docs` provides generated API documentation.
- `/api/v1/*` provides the stored-only automation API.
- `/api/v1/docs` documents the isolated, typed v1 contract.

The dashboard includes an EOD watchlist, holdings and allocation, local security
search, stored price history, technical and risk analysis, a momentum screener,
and on-demand company research.

FastAPI routes delegate portfolio and technical calculations to shared Polars
services. Durable market data is read from SQLite. Until the scheduled ingestion
pipeline is implemented, missing history is fetched from Yahoo Finance once and
stored before analysis.

The CLI and dashboard both use `data/quant.db` as the sole source of truth for
users, position lots, watchlists, securities, universes, and daily market bars.
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

The stored-only v1 read API is available. Authentication, synchronization, and
write contracts remain planned work; see `docs/DATA_PIPELINE_ROADMAP.md`.

## Automation CLI

`quant-api` is a machine-readable client for the stored-only `/api/v1` API. It
is intentionally separate from the human-oriented, SQLite-direct `quant`
commands. Start `quant-dashboard` first, then query prepared data with:

```sh
uv run quant-api health
uv run quant-api status AAPL MSFT
uv run quant-api quotes AAPL MSFT
uv run quant-api bars AAPL --start 2026-01-01 --limit 100
uv run quant-api technical AAPL --windows 20 50 200
uv run quant-api risk AAPL MSFT --period 1y --benchmark SPY
uv run quant-api screener AAPL MSFT
uv run quant-api portfolio
uv run quant-api portfolio-risk --period 1y
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

V1 GET requests never contact Yahoo or write to SQLite. Missing or incomplete
stored data is reported through partial-data warnings or an
`insufficient_data` error. Populate or refresh data through the existing local
commands until explicit synchronization APIs are implemented.

All v1 returns, weights, alpha, volatility, and contributions are fractions;
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
