# Development log

This log records delivered behavior, the reason for each change, validation, and
remaining limits. Commit history preserves the corresponding implementation.

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

## 2026-09-08 — Consolidate current code and add manual acceptance tests

**Why:** The latest research features were on a previously merged fix branch while
local `master` was behind GitHub. Keeping obsolete branch names and the unused
startup-only entry point made it harder to identify the current implementation.

**Changed:**

- Fetched and pruned remote references, fast-forwarded local `master` to GitHub's
  current `master`, then merged the two newer feature commits without rewriting
  their history (`fc86521`). Removed local `feat/sqlite-data-foundation` and
  `fix/foundation-stabilization` only after verifying their commits were retained.
  Removed the already-merged GitHub fix branch using an expected-tip guard.
- Removed `startup_sync.py`, its unused server import, an unused worker import,
  and an unused server logger. Moved all three startup-wrapper tests to
  `test_ingestion.py`, exercising `IngestionService` directly. Provider mocks and
  failure/backfill coverage remain. CLI help now describes background ingestion
  and what `--no-sync` actually disables.
- Added [manual acceptance tests](MANUAL_TESTS.md) and an offline fixture generator
  at `scripts/prepare_manual_tests.py`. The generator requires an empty directory,
  supplies deterministic synthetic prices and import requests, and never chooses
  `data/quant.db`. The walkthrough exercises the public interface and includes
  expected values, expected errors, restart/backup checks, and failure reporting.

**Validation:** All 191 unit tests pass. A separate real localhost HTTP/CLI run
verified the ten core manual cases: discovery/quotes, idempotent imports, rejected
requests, risk coverage, unknown cash, frozen replay, live/frozen isolation,
report evidence and warnings, queued offline sync, and restart/backup persistence.
The fixture generator's refusal to overwrite existing files was also checked.
`git diff --check` passes. The optional live-provider check and visual dashboard
inspection are instructions for manual follow-up, not claimed automated validation.

**Scope:** No financial method or schema changed. The legacy dashboard and its
used API compatibility routes remain supported. Personal data and retained
research artifacts were not modified or deleted. `master` is the consolidated
branch; branch deletion removes names, not the merged history.
