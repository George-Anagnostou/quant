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

Analyze the full cached S&P 500:

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

Open http://127.0.0.1:8001. Override the bind address when needed:

```sh
HOST=0.0.0.0 PORT=9000 uv run quant-dashboard
```

Routes:

- `/` serves the dashboard.
- `/api/*` provides JSON APIs.
- `/docs` provides generated API documentation.

The dashboard includes an EOD watchlist, holdings and allocation, stored price
history, and configurable technical analysis.

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

The versioned, authenticated API remains planned work. See
`docs/DATA_PIPELINE_ROADMAP.md` for the ingestion and API roadmap.

## Storage

`data/quant.db` is the only runtime data store. SQLite uses WAL mode, foreign
keys, short transactions, and provider-separated daily bars. Generated database,
WAL, and shared-memory files are ignored by Git.

## Tests

```sh
PYTHONDONTWRITEBYTECODE=1 uv run python -m unittest discover -s tests
```

Unit tests must mock Yahoo and Wikipedia boundaries; they should not require
network access.

See `INTEGRATION_REVIEW.md` for the merged PR inventory and current ownership
boundaries. See `docs/DATA_PIPELINE_ROADMAP.md` for planned ingestion,
authentication, API, deployment, and historical-analysis work.
