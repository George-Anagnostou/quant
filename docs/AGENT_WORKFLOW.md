# External-agent investment research

Quant owns data and financial calculations. An external agent chooses questions,
interprets results, examines counterarguments, and writes a report. No model SDK,
embedded agent runner, or trading execution is required.

## Start and inspect

```sh
uv run quant serve
uv run quant query capabilities
uv run quant query quality
uv run quant query gaps
```

The server initializes/migrates the database and serves stored data while one
background worker synchronizes. `--no-sync` disables the worker, including queued
sync requests. Scheduled work catches up on startup and after the next completed
US equity session (conservative 20:00 Eastern availability). Each daily cycle
creates a verified backup. Keep the service on loopback.

`quant query request PATH` calls any documented alpha resource. GET is the default;
POST uses `--method POST --json-file FILE` (or `-` for stdin). The CLI forwards JSON;
it never opens SQLite, contacts providers, or computes financial results.
OpenAPI at `/api/alpha/docs` is the source of truth for request models.

## 1. Import a dated portfolio export

Prepare a JSON snapshot using this shape; these are illustrative values:

```json
{
  "importKey": "broker-account-2026-08-31",
  "date": "2026-08-31",
  "account": "broker-account",
  "source": "broker portfolio export",
  "currency": "USD",
  "cash": null,
  "positions": [
    {
      "symbol": "AAPL",
      "quantity": 10,
      "averageCost": null,
      "marketValue": null,
      "sector": null,
      "assetClass": "equity",
      "currency": "USD"
    }
  ],
  "original": "Optional original export text, preserved verbatim"
}
```

```sh
uv run quant query request portfolio/snapshots --method POST --json-file snapshot.json
uv run quant query snapshots
```

Aggregate lots by symbol within one account snapshot. One snapshot represents one
complete account observation, not a position delta. Missing cash, cost, and export
market values remain null. Imported snapshots do not modify the legacy dashboard
lots. Save the returned `data.id`; import keys are immutable and idempotent.
A correction uses a new import key. Preserve the original export in `original`
when converting a broker-specific CSV; no broker-specific parser is assumed.

Snapshot symbols join the tracked ingestion universe. To request immediate work,
POST `/data/sync` with `importKey`, optional `symbols`, and optional `horizon`.
The returned record id also identifies `/data/ingestion-runs/{id}`. Its status is
queued until the worker claims it.

Data quality identifies unknown security metadata. Explicit verified metadata
can be posted to `/securities/metadata`, with `importKey`, `symbol`, `currency`,
`calendar` (`XNYS` or null), `instrumentType`, and `source`. S&P constituents and
SPY receive known US metadata during ingestion. Other instruments are not
automatically classified by ticker spelling.

## 2. Check readiness, then freeze a research run

```sh
uv run quant query readiness SNAPSHOT_ID --period 1y --benchmark SPY
```

This stored-only request checks prerequisites for valuation, total account value,
and historical risk separately. `ready` means all checked prerequisites hold;
`limited` means some checked capabilities or valuations remain usable with stated
limits; `blocked` means none meets these prerequisites. Each capability also has
its own status and issue codes. Cash-only snapshots mark risk `not_applicable`.
Missing cash blocks account value without blocking securities risk. These statuses
do not certify an investment conclusion or statistical significance.

Inspect `warnings[].affects`, `symbols`, and `action`. Per-security coverage counts
usable close/adjusted-close pairs on the requested sessions. Missing-session samples
contain at most 20 dates; the count is the full count. Unknown calendars produce
unknown gap counts. The window ends at the snapshot's latest completed session,
with today's session available only after the conservative provider cutoff.
This checks freshness as well as completeness: a review can still return older
risk results explicitly flagged as stale when readiness blocks current risk.

Fix verified metadata explicitly and enqueue ingestion only when needed. Persistent
gaps may reflect a shorter listing history or unsupported provider data; retrying
does not establish coverage. Check readiness again, then create the frozen run.
Live readiness is advisory and may change before run creation.

```json
{
  "importKey": "portfolio-review-2026-08-31-v1",
  "snapshotId": "ID_FROM_SNAPSHOT_IMPORT",
  "period": "1y",
  "benchmark": "SPY"
}
```

```sh
uv run quant query request research/runs --method POST --json-file run.json
uv run quant query request research/runs/RUN_ID
uv run quant query request research/runs/RUN_ID/readiness
uv run quant query request research/runs/RUN_ID/replay
```

A run preserves a verified SQLite backup, its SHA-256 hash, the snapshot and prior
snapshot, normalized parameters, methodology version, computed results, and
warnings. Replay verifies the input file and compares freshly calculated results
with the retained output. Current code supports its current methodology version;
retain the matching source revision for replay across future method changes.

Run readiness rechecks the frozen dataset using the original request and the run's
creation time. Its dataset hash is returned in both data and envelope metadata.
It uses the currently installed readiness method (reported separately), rather
than adding or altering evidence in the immutable run. It cannot enqueue repairs
to a frozen dataset; fix live inputs and create a new run when needed.

The analysis date is the portfolio snapshot date. Later market data is excluded.
A frozen current dataset enables reproducibility; it does not prove that its
historical prices or fundamentals were available to an investor at an earlier time.

Run creation and `/research/runs/{id}` return a compact `data.result` containing
valuation, allocations, concentration, changes,
risk, and definitions. Risk requires complete common history and is bounded to
50 securities. Missing benchmark sessions, insufficient history, or unpriced
holdings must be discussed. All new return and weight fields are fractions.
Current-holdings backcasts and zero-rate/static-weight assumptions are explicit.
The full immutable record at `/research/records/run/{id}` includes
`data.payload.result`, historical points, correlations, and preserved calculation
sources. Joint risk additionally requires verified USD/XNYS security metadata.

For additional evidence, bounded `/market/bars` accepts `symbols`, `start`, `end`,
`fields`, `limit`, `cursor`, and optional `runId`. Prefer the run id for consistent
pagination. A live-dataset cursor is invalidated when ingestion changes that
dataset. Date and symbol are always returned. Field projection and row limits are
applied in SQLite before conversion to Polars.

## 3. Investigate and save the report

Follow this sequence:

1. Read the run warnings and establish what can be concluded.
2. Explain the largest exposures and historical risk drivers.
3. Separate recorded account observations from hypothetical analyses.
4. Investigate plausible alternative explanations and contradictory evidence.
5. Explain unfamiliar metrics, their assumptions, and their practical limits.
6. State unresolved questions and evidence that would change the assessment.

POST `/research/reports` with `importKey`, `runId`, `model`, `claims`,
`counterarguments`, `openQuestions`, `acknowledgedWarnings`, and `narrative`.
Each claim has `text`, `kind` (`fact`, `interpretation`, or `hypothesis`), and an
`evidence` list. References are paths such as `result/concentration/largestWeight`
or immutable external evidence record ids. Every run warning code must appear in
`acknowledgedWarnings`. Invalid paths are rejected. This checks traceability;
it does not certify the truth of an agent's interpretation.

Reports are immutable. A changed report uses another import key. Read it through
`/research/records/report/{id}`. Frozen databases live in `data/artifacts/` and
must be backed up along with `data/quant.db` to preserve replay.

## Company and ETF research

Explicit SEC ingestion is POST `/research/sec` with `symbol` and `cik`. Configure
`QUANT_SEC_USER_AGENT` with an identifying name and contact email. It reads SEC
submissions and company facts, checks the issuer ticker, and preserves up to the
20 most recent annual/quarterly filings from the recent-submissions response.
Filings, amendments, and revised extractions remain separate evidence records.
This is intentionally bounded; it is not a complete historical EDGAR crawler.

GET `/research/fundamentals/{symbol}?asOf=TIMESTAMP` returns retained observations
and matched-period revenue, margin, cash-generation metrics. Filing availability,
period, unit, accession, and retrieval time remain distinct. Missing custom tags,
sector-specific accounting, and unmatched fiscal periods are not guessed.
GET performs no provider request. SEC documentation:
[EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces).

POST `/research/evidence` also accepts preserved external documents and dated ETF
holdings. Fund data uses `data.holdings=[{"symbol":"AAPL","weight":0.05}]` plus
`periodEnd`, `availableAt`, source, and source URL. `/portfolio/snapshots/{id}/look-through`
reports underlying exposures and uncovered weight. Import issuer exports explicitly;
there is no universal issuer-website scraper. Mark direct stocks `assetClass=equity`
to include their direct exposure when fund composition is absent.

POST `/research/valuation` evaluates explicit FCFF assumptions across growth,
discount-rate, and terminal-growth cases. Operating cash flow minus capex is a
cash-generation proxy, not automatically FCFF. POST `/research/theses` retains
evidence, counterarguments, assumptions, invalidation conditions, and optional
`supersedes` links.

Legacy Yahoo research remains transient. The explicit POST `/research/live/profile`
returns profile data plus retrieval time, cache age, live/cache source, stale
fallback, and provider-error state. Save selected material separately as evidence.

## Actual performance

POST `/portfolio/ledger` with account/source/import identity, opening and closing
snapshot ids, transaction events, and `complete=true` only for complete records.
Events preserve `sourceId`, date, type, total cash consideration, quantity, and
split ratio where applicable. Fees and dividends are separate events. Supported
transfers are cash transfers; in-kind transfers need a richer accounting model.
Corrections retain old imports using `supersedes` and a new import key.

GET `/portfolio/ledger/{id}/performance` derives shares and cash, reconciles both
against the closing snapshot, and checks closing valuation. Actual performance
is withheld unless coverage and reconciliation pass. An optional `runId` pins this
calculation to a frozen database that must already contain the ledger import.
All dated events are treated
as end-of-day. Non-trading-day events or missing prices can make daily valuation
unavailable; prices are not interpolated. Yahoo's split-adjusted closes are
reconstructed using declared in-period splits, with reconciliation as a required
gate. Later splits outside the ledger window can prevent reconciliation.

Money-weighted returns are annualized using actual elapsed days / 365. The
implementation conservatively withholds IRR for nonconventional cash-flow sign
patterns or an unbracketed root. Snapshot-only account changes never become
investment returns automatically.

## Market discovery and evaluation

`/universes`, `/universes/{name}/members?asOf=DATE`, and `/market/breadth` identify
the observed universe snapshot, eligible coverage, trend breadth, and 1-month
relative-strength percentile. Historical membership before collection is not
invented. `/market/factors` fits up to five explicit return proxies on complete
common daily observations. `/portfolio/scenarios` evaluates supplied shocks.

POST `/research/experiments` creates a retained hypothesis test against a frozen
run, with a fixed momentum lookback, training cutoff, next-close execution, costs,
and at least 20 out-of-sample sessions. `/research/experiments/{id}/replay` verifies
the frozen inputs and preserved calculation source before comparing results.
This is a deliberately narrow experimental
primitive, not a general backtester or validated trading strategy.

POST `/research/evaluations` attaches explicit evaluator scores and cost to a
report. Compare reports from the same frozen run using factual accuracy, numeric
consistency, evidence use, and missing-data handling. Scores are supplied by the
evaluator, not automatically certified. Add multi-agent workflows externally
only when evaluation supports their benefit.

GET `/research/evaluations?runId=ID` compares mean reviewer scores and costs for
models evaluated on that same frozen case.
