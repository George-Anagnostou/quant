# Agent API contract and expansion

The implementation now supports the personal research workflow described in
[AGENT_WORKFLOW.md](AGENT_WORKFLOW.md). OpenAPI at `/api/alpha/docs` contains the
current request schemas. All durable writes use strict input models and immutable
import identities; retrying identical content is safe, conflicting content fails.

## Current surfaces

| Area | Resources |
| --- | --- |
| Discovery | capabilities, calendar sessions, universes/membership, breadth, factor proxies |
| Data | status, quality, gaps, queued synchronization, durable ingestion run status, metadata |
| Efficient reads | multi-symbol projected bars with bounded cursor pages and frozen-run selection |
| Portfolio | snapshots, exposure/risk reviews, look-through, scenarios, ledger imports/performance |
| Research | runs/replay, records, reports, SEC/evidence, financial observations, valuation, theses |
| Evaluation | retained momentum experiments and explicit reviewer scores |

The existing alpha endpoints remain available. New research result payloads are
versioned application structures in the existing data/meta/warnings envelope.
Input models are strict; record payloads preserve their category-specific source
structure. Returns and weights are fractions. Request bodies are bounded to 8 MiB,
record pages to 8 MiB, and the CLI rejects responses larger than 16 MiB.

## Read/write separation

Analytical GETs never contact providers or write SQLite. The dashboard's legacy
analytics follow the same rule. Explicit legacy research routes remain transient;
POST `/research/live/profile` exposes source, retrieval time, cache age, and stale
fallback. POST `/research/sec` performs explicit durable provider ingestion.
Market synchronization is queued through POST `/data/sync` and executed by the
single worker. Read requests never enqueue hidden work.

New durable writes require loopback access and same-origin browser requests.
Identity remains local-admin. These protections are not a hosted authentication
system. Keep the server on loopback.

## Reproducibility and compatibility

Research runs freeze a database, its digest, normalized parameters, calculation
source/digest, results, warnings, and portfolio context. Analytical replay rejects
changed inputs or calculation source. A run id can pin bulk market-data reads.
Live cursors detect intervening ingestion and require pagination to restart.

Raw and normalized evidence remain distinguishable; filing availability and
retrieval timestamps serve different purposes. Neither a current database copy nor
an observed current universe establishes historical information availability.

Future compatibility work should continue tightening output schemas as methods
stabilize, adding broker-specific input adapters, and expanding experiment methods
with explicit dates, costs, and independently calculated acceptance fixtures.
