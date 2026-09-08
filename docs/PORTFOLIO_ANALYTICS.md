# Portfolio analytics and transactions

The current-holdings foundation is described in [the portfolio guide](PORTFOLIO_AGENT_GUIDE.md).
This extension gives clients explicit portfolio valuation, a transaction book,
daily NAV records, reconciled account performance, and read-only simulations.
All calculations use stored data. No analysis read calls a provider or enqueues work.

## What the six roadmap items deliver

| Capability | Interface | Interpretation |
| --- | --- | --- |
| Ticker valuation and returns | `quant portfolio analysis` / `quant query portfolio-analysis` | One ticker row across selected account lots; dated value, remaining basis, unrealized return, current-quantity daily price change, recorded realized gains |
| Strategy and sector allocation | `analysis.allocations` | Account, asset class, strategy, sector; weights include known cash; mixed lot classifications remain separate allocations |
| Transaction imports and realized gains | `transactions-preview`, `transactions-import`, `transactions` | Ordered, atomic, idempotent imports; purchases, explicit-lot sales, cash flows, dividends, fees, declared splits |
| Daily NAV snapshots | `nav-capture`, `nav`, `nav-list`, `nav-report` | Retained daily account values with immutable price evidence; broker observations stored separately |
| Actual account risk | `performance NAV_ID` | Flow-adjusted TWR, conventional MWR, volatility, Sharpe, Sortino, drawdown, beta and alpha, gated by completeness and reconciliation |
| Scenarios, contributions, tax-lot views | `scenario`, `performance`, `tax-lots`, `simulate-sale` | Ticker/sector/strategy shocks, dollar P&L attribution, holding days and gain/basis information, explicit-lot hypothetical sales |

Discover input schemas with `quant portfolio schemas`; OpenAPI documents the
response fields, including typed `PortfolioAnalysisData`, `TransactionData`, and
`PerformanceData`. Every CLI command returns the usual `data/meta/warnings` envelope.
Use `--pretty`, `--base-url`, and `--json-file -` as with the existing client.

## Current valuation

```sh
uv run quant portfolio analysis --account 'Main brokerage' --date 2026-09-04 --pretty
uv run quant query portfolio-analysis --account 'Main brokerage' --date 2026-09-04
uv run quant portfolio tax-lots --account 'Main brokerage' --date 2026-09-04
```

Omit account to include all USD accounts. The default valuation date is the latest
completed XNYS session. A dated analysis uses a close on that exact date; it never
silently mixes older quotes. Currency must be verified in security metadata.
Full security value, gains, and weights are withheld when quotes are incomplete;
`pricedSecuritiesValue` and `unpricedLotCount` explain partial coverage. Unknown
cash leaves full account value and weights unavailable. No foreign-currency
conversion is assumed.

An analysis of current holdings cannot precede the account's state date. Historical
holdings and returns come from retained NAV records and transaction replay.
`unrealizedReturn` is gain divided by remaining basis. `dayGain` is current quantity
times the close-to-close price change; it is not trade-adjusted account P&L.
Actual performance is a different resource. All rates and weights are fractions.
Cash is shown separately, so sector and strategy weights need not sum to one.

Classification is lot metadata. A ticker may participate in several strategies;
strategy allocation sums the relevant lots rather than arbitrarily choosing one.
`Unclassified` remains visible. Aggregated snapshots retain a common classification
or `Mixed` when their source lots disagree. Existing snapshot reviews now include
strategy allocation too.

## Transaction intake

Start with observed opening lots and cash at a known date. Import subsequent
activity in date order. This is a forward application to current state, not a silent
retroactive replacement of already-recorded balances. For externally supplied
historical ledgers that should not change current holdings, the existing
`portfolio/ledger` import and reconciliation API remains available.

```json
{
  "importKey": "broker-activity-2026-09-01-through-09-04",
  "expectedRevision": 1,
  "source": "Broker account activity export",
  "reason": "Record reviewed transactions following opening balances",
  "transactions": [
    {
      "sourceId": "execution-001",
      "date": "2026-09-01",
      "entry": {
        "action": "buy",
        "lot": {
          "account": "Main brokerage", "symbol": "AAPL",
          "quantity": "2", "totalCost": "300.50", "acquired": "2026-09-01",
          "sourceLotId": "broker-lot-001", "strategy": "Core", "sector": "Technology"
        },
        "fees": "0.50"
      }
    },
    {
      "sourceId": "execution-002",
      "date": "2026-09-04",
      "entry": {
        "action": "sell", "account": "Main brokerage", "sourceLotId": "broker-lot-001",
        "quantity": "1", "unitPrice": "160.00", "fees": "0.25"
      }
    }
  ]
}
```

```sh
uv run quant portfolio transactions-preview --json-file transactions.json --pretty
uv run quant portfolio transactions-import --json-file transactions.json --pretty
uv run quant portfolio transactions --account 'Main brokerage' --start 2026-09-01 --end 2026-09-04 --pretty
```

A purchase's `totalCost` includes fees already. Sell by `lotId`, or by account plus
`sourceLotId`; the latter can refer to a purchase earlier in the same import.
Every source transaction has a stable `sourceId`. Repeating the same request key
and content is safe; overlapping source IDs in a differently identified import
are rejected. Keep `source` stable for that data source. One failed event rolls
back the entire batch and its audit. Same-day input order is preserved. One import
advances the portfolio revision once.

Internal `quant portfolio apply` buys/sells use this same transaction book.
Realized gains are net proceeds minus released basis. Full closes retain their
sale history after the active lot disappears. Opening `add`/`cash` and corrective
`edit`/`remove` operations are not invented trades or deposits. Corrections inside
an account-performance period block actual-performance eligibility.

A declared split uses `{"action":"split","account":"Main brokerage","symbol":"AAPL","ratio":"2"}`.
It changes all matching lot quantities while preserving total basis and acquisition
dates. NAV replay restores historical quote/share units using recorded splits.
Splits already known after a requested historical NAV period are also considered
when undoing Yahoo's split-adjusted price basis. Unrecorded corporate actions cannot
be inferred from the transaction book; a completeness attestation must cover them.
Cash-in-lieu, mergers, spinoffs, transfers with basis, margin, and short positions
remain outside this first transaction implementation.

## Daily NAV and actual performance

NAV is securities value plus cash. A **computed NAV** uses the stored daily prices
and transaction book. A **reported NAV** is a broker's account total. Keeping both
lets clients detect missing transactions, fees, or price discrepancies without
replacing one observation with the other.

Import or capture opening and closing account snapshots using the existing
snapshot interfaces. Their quantities and cash are reconciliation checkpoints.
Checkpoint market values may be broker-provided; if absent, quant calculates them
from exact-date stored closes and preserves that calculation in the NAV evidence.
Both checkpoints must describe one USD account. Periods are bounded to ten years.

```json
{
  "importKey": "main-nav-review-2026-09-04-v1",
  "account": "Main brokerage",
  "openingSnapshotId": "OPENING_SNAPSHOT_ID",
  "closingSnapshotId": "CLOSING_SNAPSHOT_ID",
  "complete": true,
  "benchmark": "SPY"
}
```

```sh
uv run quant portfolio nav-capture --json-file nav.json --pretty
uv run quant portfolio nav-list --account 'Main brokerage'
uv run quant portfolio nav NAV_RECORD_ID --pretty
uv run quant portfolio performance NAV_RECORD_ID --pretty
```

Set `complete: true` only when all activity in the interval is known. The default
is false. The response retains daily cash, value, external flow, and flow-adjusted
return, plus the exact price evidence and its digest. The opening day has no return.
Subsequent returns use `(ending NAV - external flow) / previous NAV - 1` under the
end-of-day convention. Non-session cash flows are booked to the next session for
valuation and return calculations, including MWR; original transaction dates remain
in the book. Exchange trades and splits require supported trading-session dates
for NAV replay. No execution-time weighting is claimed.

Actual return and risk require complete activity, reconciled cash and quantities,
positive opening value, supported USD/XNYS security metadata, and daily prices.
Missing prices and corrections withhold actual metrics. Missing benchmark coverage
withholds beta/alpha while leaving otherwise eligible account-only risk available.
A short period may have too few observations for some risk statistics. Annualization
uses 252 sessions and a zero risk-free rate. MWR is only returned for conventional
cash-flow patterns with a unique bracketed solution.

`profitContributions` is ticker-level **dollar P&L**, computed as closing value minus
opening value, plus sale proceeds and income, minus purchase outlays and fees.
Unattributed cash income/fees are separate. Contributions reconcile to the change
in account NAV after external flows. They are not mislabeled as additive
multi-period percentage returns.

NAV capture is an explicit write. GETs do not create snapshots or schedule captures.
An unchanged import key returns the original retained calculation after later
price corrections. Capture a newly identified record to calculate revised evidence.
The original remains available. No automated daily schedule is installed by this feature.

### Broker NAV observations

```json
{"importKey":"broker-nav-2026-09-04","account":"Main brokerage","date":"2026-09-04","value":"25000.50","source":"Broker statement"}
```

```sh
uv run quant portfolio nav-report --json-file reported-nav.json
```

NAV reads include comparisons for reported dates in the computed series. The
latest reported observation per date is used for reconciliation. A difference
greater than $0.02 withholds metrics from the `performance` resource until resolved.
The immutable NAV record still preserves the original computation; broker evidence
does not overwrite it. Reported totals are optional and do not fabricate a daily
return series when only a few statements exist.

## Scenarios and tax-lot tools

A holdings scenario accepts `account`, valuation `date`, `dimension` (`symbol`,
`sector`, or `strategy`), and a `shocks` object of fractional price changes.
For example, `{"Technology":-0.15}` with `dimension:"sector"` shocks the selected
lots while leaving cash unchanged. Unknown targets are rejected; complete prices
and known cash are required. Call `quant portfolio scenario --json-file scenario.json`.

A sale simulation accepts an account, date, and a `sales` list of explicit `sell`
actions. Call `quant portfolio simulate-sale --json-file sales.json`. The result
shows proceeds, released basis, realized gains, and holding days. It does not
modify holdings or require a known cash balance. Each lot appears at most once.

`tax-lots` reports holding days, current basis, unrealized gain/loss, and recorded
realized sales. These are descriptive tools: no assumed tax rates, tax liability,
wash-sale compliance, or automatic tax-lot optimization. Simulations use Polars
floating-point analytics; applied transactions retain decimal book values.

## API map

| Method | Alpha resource |
| --- | --- |
| GET | `/portfolio/analysis`, `/portfolio/transactions`, `/portfolio/tax-lots`, `/portfolio/schemas` |
| POST | `/portfolio/transactions/preview`, `/portfolio/transactions/import` |
| POST / GET | `/portfolio/nav` (capture / list) |
| GET | `/portfolio/nav/{id}`, `/portfolio/nav/{id}/performance` |
| POST | `/portfolio/nav/observations`, `/portfolio/holdings-scenario`, `/portfolio/sale-simulation` |

Writes enforce loopback and browser-origin checks and use the current user scope.
Preview and simulation POSTs are read-only. No broker-specific parser or personal
portfolio import was run as part of this implementation.
