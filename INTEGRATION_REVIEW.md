# Integration review

The personal research-engine implementation preserves the original shared
SQLite/Polars/API foundation and extends it with durable ingestion, snapshots,
frozen research runs, evidence, ledger reconciliation, and bounded discovery.

Current ownership boundaries are documented in `AGENTS.md`. See
`docs/DATA_PIPELINE_ROADMAP.md` for delivery status and explicit limits, and
`docs/AGENT_WORKFLOW.md` for the public API/CLI workflow.
`docs/MANUAL_TESTS.md` provides repeatable acceptance checks with synthetic inputs.

The existing dashboard remains a compatibility client. Its lots are separate
from immutable imported account snapshots. Its analytical reads are now stored-only;
explicit legacy research remains transient. New durable data flows use the alpha
research API and immutable, audited records.
