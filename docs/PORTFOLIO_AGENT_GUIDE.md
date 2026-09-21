# Agent-managed portfolios

`quant portfolio` is the current-holdings client. It uses the same alpha HTTP API
as `quant query`, never SQLite or a market-data provider. All writes are scoped to
the API's user (currently `local-admin`). Watchlists have been removed. Owned
assets drive personal ingestion; discovery, experiments, and evidence-backed
theses describe potential investments separately from ownership.

## Model

- **Account:** stable, user-chosen name, currency, optional institution and account
  type, recorded cash balance, and state date. Account type is not an account ID.
  A missing cash balance is unknown, not zero. Cash is separate from securities.
- **Lot:** stable generated ID, account, normalized ticker, quantity, **total
  remaining cost basis** including fees, currency, acquisition date, description,
  CUSIP if trustworthy, asset class, strategy, sector, and optional source lot ID.
  Provenance includes source, effective date, original source fields, and optional
  reported price/value with their own date. The latter are source observations,
  never substituted for current stored market prices.
- **Position:** calculated aggregation of lots by account, symbol, and currency.
  Original lots are retained. A position is not another independently editable row.
- **Change:** immutable request identity, effective date, source, reason, action
  inputs, before/after state, cash effects, and sale cost/proceeds/realized gain.
  Revisions prevent stale writes; one batch commits fully or rolls back fully.
- **Research snapshot:** separate immutable, dated account observation. Capturing
  current holdings aggregates lots for the existing review/readiness/run pipeline.
  Importing a research snapshot does not change live holdings.

Amounts and quantities use decimal strings (up to 12 decimal places, bounded at
10^15 per input). Store total lot cost rather than multiplying a rounded displayed
unit cost. Public valuation uses Polars and floating point as before; canonical
lot basis is preserved. Partial sales allocate basis proportionally within the
explicitly selected lot, retaining 12 decimal places; a full close releases the
remaining basis. This is bookkeeping, not an automatic tax-lot optimization rule.

## Inspect and discover

```sh
uv run quant portfolio show --pretty
uv run quant portfolio schema --pretty
uv run quant portfolio history --limit 20 --offset 0 --pretty
uv run quant query portfolio --pretty
```

`show` returns `revision`, `accounts`, individual `lots`, and aggregated `positions`.
`query portfolio` returns stored-market valuation of security lots; cash is shown
separately by `portfolio show`. Its portfolio risk is a current-share backcast,
not actual account return. The dashboard opens on Positions and shows account cash
and security lots. Portfolio changes are handled by the agent command interface.

Use `--base-url http://127.0.0.1:PORT/api/alpha` for an isolated server. CLI success
and error envelopes and exit codes match `quant query`. `--json-file -` accepts
stdin. Schemas are also in `/api/alpha/docs`.

## Normalize, preview, apply

Create a JSON command from the reviewed input. This synthetic example opens an
account, records its cash, and adds an opening lot; it is not a sample-file import.

```json
{
  "importKey": "example-statement-2026-09-01-v1",
  "expectedRevision": 0,
  "effectiveDate": "2026-09-01",
  "source": "example statement; source SHA-256 or document identity",
  "reason": "Record reviewed opening balances",
  "actions": [
    {"action": "account", "account": {"name": "Main brokerage", "currency": "USD", "accountType": "Brokerage"}},
    {"action": "cash", "account": "Main brokerage", "amount": "500.00"},
    {"action": "add", "lot": {
      "account": "Main brokerage", "symbol": "AAPL", "quantity": "2",
      "totalCost": "300.01", "currency": "USD", "acquired": "2026-08-20",
      "description": "Apple Inc.", "assetClass": "Equity",
      "sourceLotId": "statement-line-3",
      "original": {"Ticker": "AAPL", "Quantity": "2", "Cost": "300.01"}
    }}
  ]
}
```

```sh
uv run quant portfolio preview --json-file reviewed-portfolio.json --pretty
uv run quant portfolio apply --json-file reviewed-portfolio.json --pretty
uv run quant portfolio show --pretty
```

Preview validates and returns the complete before/after state, including cash and
lot effects, without writing or synchronizing providers. Applying the identical
request with the identical `importKey` is idempotent, even after later changes.
Reusing that key for different content is an error. The key belongs to the user,
not to the machine running the agent. Use a source/document identity plus an
explicit operation identifier; never create a fresh key just because a retry timed out.
A preview of an already-applied request returns its original result with
`alreadyApplied: true`; that result is not a new projection of current holdings.

The revision is checked inside the write transaction. On a conflict, reread the
portfolio, reconcile the intended changes, and preview a newly identified request.
Do not blindly substitute a new revision. Every active source lot ID must be unique
within its account. A repeated ticker is normal and never a deduplication key.

## Evolve holdings

All actions use the same batch envelope above, with a new import key, current
revision, source, reason, and effective date. Multiple account/lot actions may be
combined in one batch.

| Action | Required payload | Meaning |
| --- | --- | --- |
| `account` | `account` object | Create a named account with initially unknown cash |
| `add` | complete `lot` | Opening/imported lot; does not imply a purchase or change cash |
| `edit` | `lotId`, complete replacement `lot` | Correct a lot; omitted optional fields are cleared; cash unchanged |
| `remove` | `lotId` | Remove an erroneous lot; retained audit, cash unchanged; not a sale |
| `buy` | complete `lot`, optional `fees` | Acquire a new lot and debit its totalCost from known cash; acquired must equal effectiveDate |
| `sell` | `lotId`, `quantity`, `unitPrice`, optional `fees` | Reduce/close the selected lot; credit quantity × unitPrice − fees |
| `cash` | `account`, `amount` | Opening cash or balance correction; does not imply a deposit |
| `deposit`, `withdrawal` | `account`, `amount` | External cash flow |
| `dividend`, `fee` | `account`, `amount` | Internal cash income or expense |

For a purchase, `totalCost` **already includes fees**; fees are descriptive and
must not be added twice. To sell across lots, provide one `sell` action for each
lot. Quant never selects lots automatically. Negative cash and selling more than
a lot contains are rejected. Cash must be explicitly initialized before a trade.
Edits cannot transfer lots between accounts/currencies. A correction after a trade
must explicitly correct cash if necessary; it does not rewrite past events.

The state date advances per account. Earlier effective dates are rejected because
applying them to a current balance would misrepresent chronology. Same-day actions
are ordered by the batch and portfolio revision. Original acquisition dates remain
historical. Imported holdings do not establish complete prior transaction history.
The separate ledger reconciliation workflow still gates actual performance; current
mutation history does not automatically claim a reconciled performance series.

## Capture for research

```json
{"importKey":"review-revision-1","expectedRevision":1,"account":"Main brokerage","date":"2026-09-01"}
```

```sh
uv run quant portfolio capture --json-file capture.json --pretty
uv run quant query readiness SNAPSHOT_ID --period 1y --benchmark SPY
```

The capture date must match the account state date. Historical reconstruction from
current lots is rejected. Capture retains account cash and aggregates lots by symbol.
It does not invent checkpoint market values; stored quote coverage and the existing
readiness and ledger contracts remain separate requirements.

## Prompt for a future portfolio intake agent

> Review the supplied portfolio export and use `quant portfolio` to prepare an
> accurate opening portfolio. First inspect `show` and `schema`. Determine the
> header row, row types, account identity, reporting date, currency, quantity, total
> cost, and acquisition dates. Preserve each lot separately and record cash as cash.
> Map source columns by meaning, retaining useful original fields and source identity.
> Preserve unknown metadata; do not invent identifiers, cost basis, trades, or sectors.
> Distinguish source classifications from independently verified classifications.
> Flag damaged identifiers, missing required values, rounding discrepancies, and
> ambiguous accounts for clarification. Do not scrape or infer a missing trade history.
> Produce normalized JSON with a stable import key and the current expected revision.
> Use `preview`; reconcile lot count, symbol count, total source basis, and cash to
> the export. Do not double count security totals repeated on lot rows. Explain any
> unresolved discrepancies. Apply only when the user's instruction authorizes intake;
> if they requested study or preview only, stop before applying. Retry the exact request
> with the same key after uncertain transport results. Verify with `show` and `history`,
> then capture a research snapshot if requested. Never write SQLite directly.

## Current boundaries

Multiple accounts and currencies can be recorded, with one currency per account.
FX conversion is not implemented; analysis rejects portfolios containing non-USD
lots instead of adding different currencies together. Long positions and nonnegative
known basis are supported. Missing basis must be resolved before an opening lot is
applied; zero is a known zero basis, not an unknown-value placeholder. Account
renaming, short/margin balances, automatic corporate-action discovery, transfers,
and a full retrospective transaction reconstruction remain outside this version.
Declared splits, forward transaction imports, retained NAV, and actual performance
are covered in [the analytics extension](PORTFOLIO_ANALYTICS.md).
Limits: 2,000 active lots, 100 accounts, 1,000 actions per batch, and 8 MiB per audit
record. History pages are bounded; reduce `--limit` when a page is too large.

Schema v4 upgrades v1 through v3 non-destructively with a verified backup. Legacy positions
keep their IDs and acquire explicit metadata when next written. Retired watchlist
rows are preserved in `retired_watchlist` solely for recovery, with no API or
runtime feature using them. Restart the server to apply the migration. Frozen
research databases are not migrated as a side effect of reads.
