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
