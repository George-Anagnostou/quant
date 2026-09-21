# Personal research engine: delivery status

## Product direction

Quant owns durable data, deterministic financial calculations, and reproducible
evidence. External agents own investigation and interpretation. The deployment is
personal and local, optimized for long-term USD stock/ETF research. Hosted identity,
trading execution, an embedded agent runner, and broad intraday ingestion are deferred.

## Implemented foundation

- Non-destructive v1-to-v2 migration with verified online backup.
- A provider protocol, one locked ingestion writer, durable runs/jobs/issues,
  recovery of every interrupted run, bounded per-execution provider retries, daily
  scheduling, and explicit queued sync requests. Existing data is served during
  synchronization.
- Missing-prefix, internal-session-gap and correction-overlap planning. Historical
  revisions must cover previously stored dates and preserve non-null observations
  before publication; failed reconciliation leaves old prices intact.
- Provider-symbol mapping, explicit security metadata, bounded XNYS sessions,
  per-security freshness, quality inspection, and verified daily backups.
- Stored-only legacy and alpha analytical reads, plus explicit transient research.

## Implemented research workflow

- Immutable, idempotent dated snapshot imports with original-source preservation.
- Snapshot-based exposure/risk bundles, observed changes, missing-data warnings,
  and explicit separation of backcasts from account performance.
- Typed snapshot and frozen-run readiness checks: separate valuation/account/risk
  prerequisites, per-symbol session gaps, and explicit suggested repair steps.
- Frozen datasets, retained calculation sources/results, content hashes, replay,
  evidence-referenced reports, and a documented external-agent workflow.
- Explicit SEC recent-filing ingestion, preserved financial observations and
  amendments, matched-period cash/revenue/margin analysis, explicit valuation
  sensitivity, imported fund holdings/overlap, and retained research theses.
- Transaction imports with correction links, quantity/cash reconciliation, gated
  time-weighted returns and conservative dated money-weighted returns.
- Observed universes, breadth/relative strength, factor-proxy regression, explicit
  portfolio shocks, a bounded momentum experiment, and evaluator-scored reports.

## Boundaries that remain deliberate

- Snapshot imports use a canonical JSON format. Broker-specific export parsers
  require actual source formats; original exports can be retained verbatim now.
- SEC ingestion covers the most recent 20 supported filings in recent submissions,
  not all EDGAR archives. Custom taxonomy normalization and sector-specific
  accounting are not synthesized. ETF holdings use explicit issuer-data imports.
- The ledger supports USD cash transfers, not in-kind transfers or tax-lot accounting.
  Intraday cash-flow timing is unavailable. Missing valuation or reconciliation
  prevents actual-performance claims.
- A frozen current dataset is not a historically point-in-time data vendor. Universe
  observations cannot establish membership before collection; current adjusted
  histories can include later corrections. Experiments retain these limitations.
- The experiment implementation is one fixed momentum research primitive with
  stated timing/costs, not a general event-driven backtester. Evaluations record
  explicit reviewer scores; Quant does not automatically certify investment ideas.
- Single-host file locks and SQLite are intentional. No Redis, Celery, PostgreSQL,
  MCP adapter, or embedded model SDK is required.
- The supported first deployment is one private VPS process under `systemd`, bound
  to loopback. Local backups require a separately chosen off-host replication and
  retention policy for disaster recovery.

## Next expansion gates

Use real broker exports to add parsers and reconcile real accounts. Add further
company/fund adapters when concrete source formats are known. Validate additional
hypotheses against frozen datasets and broaden methods only with independent test
cases. Add multi-agent interpretation only after evaluations demonstrate value.

Calendar changes require a version update and regression fixtures. New financial
methods require versioned conventions and retained source. Backups and evidence
artifacts must be archived together; disk retention is explicitly managed by the owner.
