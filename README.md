# Quant

Quant is a local investment research engine for external agents. SQLite preserves
market and portfolio data, Polars performs financial calculations, and FastAPI
provides one contract shared by the CLI and dashboard. Agents investigate and
interpret results without implementing their own financial calculation path.

## Run

```sh
uv sync
uv run quant serve
```

Python 3.14 is pinned in `.python-version`. The server listens at
http://127.0.0.1:8001. It serves stored data while a single background worker
synchronizes Yahoo daily history. `--no-sync` disables all scheduled ingestion;
`--database PATH` selects an isolated store. An optional `--horizon YYYY-MM-DD`
overrides the defaults of ten years for personal symbols/benchmarks and five years
for the current S&P universe. Other options are `--host`, `--port`, and
`--batch-size` (1–100).

Keep the server on loopback. The current identity is `local-admin`; new durable
write endpoints enforce loopback access and same-origin browser requests.
Hosted accounts and network authentication remain outside this personal release.

## Agent workflow

```sh
uv run quant query capabilities
uv run quant query quality
uv run quant query gaps
uv run quant query quotes AAPL MSFT
uv run quant query risk AAPL MSFT --period 1y --benchmark SPY
uv run quant query request portfolio/snapshots --method POST --json-file snapshot.json
uv run quant query request research/runs --method POST --json-file run.json
uv run quant query request research/runs/RUN_ID/replay
```

Follow [the complete external-agent workflow](docs/AGENT_WORKFLOW.md) for snapshot
formats, research runs, evidence references, reports, fundamentals, ETF holdings,
ledger reconciliation, scenarios, experiments, and evaluation.

`quant query` is a thin JSON API client. It never reads SQLite, calls providers,
or computes financial results. `quant data status` is an alias for the same API
coverage query. `quant query request` provides all additional alpha resources
through GET or explicit POST; `--json-file -` reads a JSON body from stdin.
Global `--base-url`, `--timeout`, and `--pretty` options also work after commands.
Environment equivalents are `QUANT_API_BASE_URL` and `QUANT_API_TIMEOUT`.

Success responses contain `data`, `meta`, and `warnings`. Errors go to stderr as
`{"ok":false,"error":...}`. Exit codes: 2 invalid input/configuration, 3 transport,
4 timeout, 5 API 4xx, 6 API 5xx, 7 invalid response, 130 interruption. Unknown and
repeated query options, non-finite JSON, duplicate JSON keys in CLI input/output,
redirects, and oversized CLI responses are rejected.

## API

- `/` is the existing dashboard; legacy `/api/*` routes remain compatible.
- `/api/alpha/docs` documents the agent contract and write request schemas.
- `/api/alpha/capabilities` describes methods, conventions, features, and limits.
- `/api/alpha/data/quality` and `/data/gaps` explain stored data readiness.
- `/api/alpha/portfolio/snapshots` imports dated account observations.
- `/api/alpha/research/runs` freezes data and calculations for repeatable research.
- `/api/alpha/research/records/{kind}/{id}` retrieves retained records.

All analytical GETs, including legacy dashboard analytics, read stored data.
Legacy `refresh` flags no longer trigger market downloads. Explicit transient
research routes remain provider-backed. POST `/research/live/profile` also reports
cache age and stale fallback. Explicit POST `/research/sec` ingests SEC evidence;
configure `QUANT_SEC_USER_AGENT` with an identifying name and contact email.

## Data and financial conventions

`data/quant.db` is authoritative. Schema v1 upgrades non-destructively to v2 with a
verified pre-migration backup. Unknown/unversioned database layouts are not
silently converted. SQLite uses WAL, foreign keys, short transactions, and
provider-separated bars. Online backups use SQLite's backup interface and verify
integrity and foreign keys. Make an additional backup with:

```sh
uv run quant data backup --destination /absolute/path/quant-backup.db
```

Research-run copies in `data/artifacts/` are immutable evidence artifacts. Preserve
that directory alongside the database for replay. Runs include calculation source
and its digest; replay rejects a changed source revision instead of silently
using different financial methods. Backups and artifacts have no automatic deletion
policy; monitor disk use and archive them deliberately.

New returns, rates, and weights use fractions; existing screener scores use 0–100.
Risk uses 252 sessions and zero risk-free rate. Current-holding history is a
hypothetical backcast; actual ledger performance is separately gated on declared
completeness, daily valuation, and reconciliation. Cash flows and ledger events
are treated as end-of-day. See the workflow for limitations, including split
adjustments and conservative money-weighted-return availability.

The built-in XNYS calendar covers 2010–2030 and uses conservative 20:00 Eastern
provider availability. Currency and calendar are explicit security metadata;
unknown metadata remains visible. The supported analytical scope is USD stocks
and ETFs, not a general multi-currency or intraday accounting system.

## Tests

```sh
PYTHONDONTWRITEBYTECODE=1 uv run python -m unittest discover -s tests
```

Tests use temporary storage and mock provider boundaries. No linter, formatter,
or type checker is configured. See [the delivery status](docs/DATA_PIPELINE_ROADMAP.md)
and [API notes](docs/API_EXPANSION_PLAN.md) for implemented boundaries and future work.
