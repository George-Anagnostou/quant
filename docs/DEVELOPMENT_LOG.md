# Development log

This log records delivered behavior, the reason for each change, validation, and
remaining limits. Commit history preserves the corresponding implementation.

## 2026-09-21 - Ingestion recovery and VPS operating baseline

**Why:** A long-running private VPS needs bounded provider behavior, durable recovery,
and data-quality issues that reflect current observations rather than stale failures.

**Changed:** Grouped provider batches by requested start date, isolated recoverable
batch failures, bounded requests per worker execution, and retried only small partial
batch omissions. Recovery now resumes every interrupted run. Impossible open/high/low
values are quarantined while valid closes remain available; replacement histories
cannot erase previously valid observations. Quality audits atomically reconcile stale
gap and incomplete-bar issues. Added a one-process, loopback-only `systemd` template
and operations guide.

**Limits:** SQLite and the ingestion lock remain single-host mechanisms. Process
health does not prove current data; operators must inspect quality, gaps, and durable
ingestion runs. Verified daily backups remain local until off-host replication and a
retention policy are configured.

## 2026-09-06 — Durable data and reproducible research foundation

Commit: `d070761` (`Add durable ingestion and reproducible research workflows`).

**Why:** Quant is becoming a personal research instrument operated by external
agents. Reliable local inputs, explicit financial assumptions, and retained
evidence must precede investment interpretation.

**Changed:**

- Added non-destructive schema migration with verified SQLite backups, durable
  ingestion jobs and recovery, session-aware coverage, historical correction
  reconciliation, and a single background worker. Analytical reads use stored data.
- Added immutable dated portfolio snapshots, exposure and risk reviews, frozen
  research datasets, retained calculation sources, replay, and cited reports.
- Added initial SEC evidence preservation, fund overlap, valuation sensitivity,
  reconciled transaction performance, market discovery, factor proxies, a bounded
  momentum experiment, and recorded reviewer evaluations.
- Exposed these workflows through the alpha API and thin CLI; documented the
  personal scope, conventions, interfaces, and remaining expansion gates.

**Validation:** 179 unit tests pass with mocked providers and temporary databases.
An isolated loopback HTTP/CLI smoke test exercised capabilities, snapshot import,
frozen run creation, and successful replay. `git diff --check` passes. Live Yahoo
and SEC data have not been audited by these tests.

**Limits:** Broker parsers still require concrete exports. SEC coverage is bounded
to recent supported filings; fund holdings require explicit imports. Ledger and
experiment methods have documented coverage and timing restrictions. Frozen
current inputs support replay, not reconstruction of historical investor knowledge.
See [the roadmap](DATA_PIPELINE_ROADMAP.md) and [agent workflow](AGENT_WORKFLOW.md).

## 2026-09-06 — Portfolio readiness through the public interface

**Why this feature next:** The first agent workflow starts by checking readiness,
but global coverage alone cannot tell whether a particular dated portfolio has
the required inputs. A targeted preflight makes that step actionable without
requiring an actual broker export or adding another analytical method.

**Changed:**

- Added strict, discoverable response schemas for snapshot and frozen-run readiness.
  Valuation, total account value, and historical risk have separate statuses.
  Unknown cash remains unknown; it does not block securities-only risk.
- Added a consistent SQLite read for security metadata and projected history.
  Required coverage uses completed sessions, close/adjusted-close pairs, the
  selected benchmark and horizon, and only Yahoo observations through that date.
- Added per-symbol missing-session counts and bounded samples, explicit unknown
  calendar coverage, and suggested actions. These are instructions for the agent;
  checks do not contact providers, enqueue ingestion, or mutate stored records.
- Added `quant query readiness SNAPSHOT_ID --period 1y --benchmark SPY` and a
  frozen-run endpoint that returns the retained dataset hash. The CLI stays a thin
  HTTP client. Updated capabilities, workflow documentation, roadmap, and API notes.

**Validation:** All 191 tests pass, including 12 new cases covering complete and
incomplete history, absent adjustments, cash-only/unknown-cash cases, unknown
calendars, currency conflicts, holidays and the daily cutoff, provider separation,
frozen checks after live changes, API read-only behavior, strict query parameters,
OpenAPI response schemas, and exact CLI forwarding. `git diff --check` passes.

**Limits and compatibility:** Readiness establishes data prerequisites, not the
quality of an investment conclusion. It requires current coverage through the
effective session; existing reviews may still return explicitly stale risk.
Live checks are advisory and can change before a run is frozen. Frozen checks use
the currently installed readiness method and do not amend the run's saved output.
The repository source changed; as with other source changes, replay of older runs
requires their retained calculation revision. No schema migration is needed.

## 2026-09-08 — Review fixes and direct app usage

**Why:** The requested manual testing means commands for using the app, rather
than a synthetic fixture generator or a separate acceptance harness. Preserve the
user's revert of that harness and address concrete review findings in the app.

**Changed:**

- Build ingestion plans before publishing a run, then save the run and all jobs
  in one transaction. Interrupted planning leaves an explicit request queued;
  recovery cannot mistake a partly created job list for the complete request.
- Require a verified supported calendar before inferring internal session gaps.
  Prefix backfills and correction overlaps remain available for unknown calendars.
- Validate nested fact and fund-holding objects, finite values, dates, and symbols.
  Malformed evidence now returns a structured validation response without a write.
  Financial summaries only interpret validated fact-category records, and source
  fields cannot overwrite retained evidence IDs, timestamps, or normalized metrics.
- Correct CLI help to describe background ingestion and `--no-sync`. Remove unused
  imports/logger from active server and worker code; retain the restored startup
  compatibility entry point. Provide usage commands in the conversation.

**Validation:** All 196 unit tests pass, including new interruption, unknown-calendar,
nested-evidence API, and provenance regression cases. Providers are mocked and
storage is temporary. `git diff --check` passes. Personal data was not changed and
live provider availability was not tested. No new manual-test harness was added.

## 2026-09-08 — Agent-managed portfolio transactions and account analytics

**Problem:** Current holdings lacked an agent-safe mutation and transaction path,
and portfolio risk used fixed current quantities rather than reconciled account
history. The watchlist no longer fit an agent-operated research workflow.

**Changed:**

- Saved the initial lot/account/cash implementation as checkpoint `074adbd` on
  `codex/portfolio-lots`; continued on `codex/portfolio-analysis`.
- Removed watchlist APIs, CLI and dashboard behavior. Schema v3 retains retired
  data for recovery and migrates supported databases with a verified backup.
- Added atomic, revision-checked portfolio mutations and ordered transaction
  imports with stable source identities, exact book basis, explicit lot sales,
  cash movements, realized gains, and declared splits. Opening balances and
  corrections remain distinct from transactions.
- Added typed ticker valuation, sector/strategy/account/asset allocation, and
  exact-date price coverage. Full totals and weights remain unavailable when
  required prices or cash are unknown.
- Retained daily NAV, price evidence/digests, checkpoint references, and transaction
  references. Actual risk and return require complete, reconciled history.
  Benchmark gaps, missing prices, in-period corrections, unverified metadata, and
  stale pre-split price data are handled explicitly. Broker-reported NAV is a
  separate observation, and mismatches gate the performance resource.
- Exposed dollar P&L attribution, ticker/sector/strategy shocks, lot holding days,
  unrealized/realized gains, and read-only sale simulations. Added API schemas,
  CLI routes and the portfolio analytics guide. Labeled the dashboard's legacy
  current-share history as hypothetical.

**Validation:** All 232 tests pass. Coverage includes atomic imports, retry and
source-identity conflicts, account isolation, partial/full sales, split basis,
ticker aggregation, missing metadata/prices, cash-flow-adjusted NAV, reconciliation,
benchmark gaps, retained evidence after corrections, reported NAV mismatches,
scenarios, sale simulations, typed APIs, and CLI forwarding. JavaScript syntax and
`git diff --check` pass. Live CLI smoke tests against an isolated `--no-sync` server
passed for holdings, schemas, portfolio analysis, transactions, and NAV listing.
No linter, formatter, or type checker is configured. The personal database and
sample export were not imported or migrated during development.

**Boundaries:** USD analysis, explicit NAV capture, and forward ordered transaction
application. No automatic corporate-action discovery, general historical rebuild,
FX/margin accounting, or assumed tax rates/wash-sale compliance. Simulations are
hypothetical; dollar P&L contributions are not multi-period return contributions.
