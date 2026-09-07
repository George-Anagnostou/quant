# Development log

This log records delivered behavior, the reason for each change, validation, and
remaining limits. Commit history preserves the corresponding implementation.

## 2026-09-06 — Durable data and reproducible research foundation

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
