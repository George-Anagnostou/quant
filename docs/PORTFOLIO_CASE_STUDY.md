# Tax-lot export case study

The local `taxlots.csv` was inspected, not imported. It remains excluded from Git.
This review preserves structural mapping lessons; it omits actual holdings and
financial values. It does not independently verify market observations.

The export has a title line before the actual CSV header. Its data rows contain
multiple security lots and a separate cash row with no ticker. Rows carry a base
currency and a shared reporting date. Several tickers
have multiple acquisition dates. The account-type column says Brokerage and the
sub-account column is empty, so the file alone cannot identify a unique account.
A future intake must obtain or explicitly choose a stable account name.

## Mapping by meaning

| Source column | Canonical meaning |
| --- | --- |
| Ticker | Normalized `symbol`, for security rows only |
| Quantity | Lot `quantity`; for the USD cash row, reconcile against Value |
| Cost | `totalCost`; authoritative source lot basis |
| Unit Cost | Displayed per-share basis; reconciliation aid, not source total basis |
| Acquisition Date | `acquired`, parsed as month/day/year |
| Account type | `accountType`, not unique account identity |
| Base CCY | `currency` |
| Description | `description`; also distinguishes a cash row |
| CUSIP | `cusip` only when trustworthy; preserve questionable raw values in `original` |
| Asset Class | `assetClass`, a broker classification |
| Asset Strategy | `strategy`; it is not a business sector |
| As of | Batch `effectiveDate` |
| Price, Value, Pricing Date | Dated `reportedPrice`, `reportedValue`, `priceDate` source evidence |

Sector and a stable source lot identifier are not provided. Future intake should
leave sector unknown and use a document/row identity where a broker lot ID is
missing. Do not aggregate lots or equate the same ticker/acquisition date with a
unique source lot.

## Reconciliation findings

Displayed unit cost does not exactly reproduce total lot cost for several rows.
As a synthetic illustration, three shares displayed at 10.00 per share can have
a source total cost of 30.01. Preserve the source total; deriving basis from a
rounded unit price loses cents.

Some identifiers appear damaged: one CUSIP has only eight characters and another
is scientific-notation text. Do not pad or reverse-engineer these values without
verification. Preserve the raw text and omit the canonical identifier until resolved.

The US DOLLAR row has a zero source cost and no acquisition date; that does not
make its value an unrealized security gain. Its reported balance belongs in cash.
Across the security rows, source value minus source cost reconciles to the source
unrealized gain. Including cash in that subtraction would overstate security gain.

`Used/Outstanding` repeats symbol-level quantities on multiple lot rows and should
not be summed as lot quantity. Other repeated totals, daily change percentages,
reported tax term/days held, and unused debt/derivative fields do not belong in the
canonical lot model. Retain useful source evidence when needed, but derive current
metrics from canonical holdings and dated stored prices. `01/01/0001` in an
inapplicable maturity column is a sentinel, not a relevant date.

Before a real import: settle account identity, flag invalid identifiers, reconcile
cash separately, preserve exact lot costs, and preview a stable, idempotent batch.
No personal database changes or normalized import payload were made from this file.
