"""Reconciled USD ledger performance with explicit end-of-day event timing."""
from datetime import date
import math

from quant.calendars import sessions
from quant.market_store import MarketDataRepository
from quant.record_store import RecordRepository


def money_weighted_return(cashflows):
    """Conventional dated cash flows only; do not arbitrarily select an IRR root."""
    amounts = [amount for _,amount in cashflows if amount]
    changes = sum(a*b < 0 for a,b in zip(amounts,amounts[1:]))
    if changes != 1 or not amounts or amounts[0] >= 0:
        return None
    first = cashflows[0][0]
    def npv(log_rate):
        return sum(amount*math.exp(-log_rate*((day-first).days/365)) for day,amount in cashflows)
    lo, hi = -10.0, 10.0
    if npv(lo)*npv(hi) > 0:
        return None
    for _ in range(100):
        middle = (lo+hi)/2
        if npv(middle) > 0:
            lo = middle
        else:
            hi = middle
    return math.expm1((lo+hi)/2)


def ledger_performance(opening, closing, events, prices, complete):
    """Replay quantities and cash, then gate performance on exact reconciliation.

    All cash flows are assumed to occur at day end. Actual execution timestamps
    are unavailable; this convention is exposed with every performance result.
    Prices must be contemporaneous unadjusted closes in the ledger share basis.
    """
    start, end = date.fromisoformat(opening["date"]), date.fromisoformat(closing["date"])
    if end <= start or opening["account"] != closing["account"]:
        raise ValueError("Ledger checkpoints must be chronological and in one account")
    if opening["currency"] != "USD" or closing["currency"] != "USD":
        raise ValueError("Ledger currently supports USD only")
    if opening["cash"] is None or closing["cash"] is None:
        raise ValueError("Ledger checkpoints require known cash")
    if any(p["marketValue"] is None for p in opening["positions"]+closing["positions"]):
        raise ValueError("Ledger checkpoints require observed position market values")
    shares = {p["symbol"]:p["quantity"] for p in opening["positions"]}
    cash = opening["cash"]
    initial = sum(p["marketValue"] for p in opening["positions"])+cash
    if initial <= 0:
        raise ValueError("Opening account value must be positive")
    expected = {p["symbol"]:p["quantity"] for p in closing["positions"]}
    by_day, seen = {}, set()
    for event in events:
        day = date.fromisoformat(event["date"])
        if event["sourceId"] in seen or not start < day <= end:
            raise ValueError("Duplicate transaction identity or event outside checkpoints")
        seen.add(event["sourceId"])
        by_day.setdefault(day,[]).append(event)
    lookup = {(r["Date"],r["Symbol"]):r["Close"] for r in prices.to_dicts()}
    days = sorted(set(sessions(start,end)) | set(by_day) | {end})
    series, missing, flows = [], [], [(start,-initial)]
    previous, growth = initial, 1.0
    for day in days:
        if day <= start:
            continue
        external = 0
        for event in by_day.get(day,[]):
            kind, symbol, amount = event["kind"], event["symbol"], event["amount"]
            if kind in {"buy","sell"}:
                sign = 1 if kind == "buy" else -1
                shares[symbol] = shares.get(symbol,0)+sign*event["quantity"]
                cash -= sign*amount
                if shares[symbol] < -1e-8:
                    raise ValueError("Transactions imply an unsupported short position")
            elif kind == "split":
                shares[symbol] = shares.get(symbol,0)*event["ratio"]
            elif kind in {"deposit","withdrawal","transfer_in","transfer_out"}:
                flow = amount*(1 if kind in {"deposit","transfer_in"} else -1)
                external += flow
                cash += flow
            else:
                cash += amount*(1 if kind == "dividend" else -1)
        if external:
            flows.append((day,-external))
        value = cash
        for symbol, quantity in shares.items():
            if abs(quantity) < 1e-8:
                continue
            price = lookup.get((day,symbol))
            if price is None:
                missing.append({"date":day.isoformat(),"symbol":symbol})
            else:
                value += price*quantity
        daily = (value-external)/previous-1 if previous > 0 and not missing else None
        if daily is not None:
            growth *= 1+daily
        series.append({"date":day.isoformat(),"cash":cash,"value":value if not missing else None,"externalFlow":external,"return":daily})
        previous = value
    differences = [{"symbol":s,"derived":shares.get(s,0),"observed":expected.get(s,0)} for s in sorted(set(shares)|set(expected))
                   if abs(shares.get(s,0)-expected.get(s,0)) > 1e-7]
    cash_difference = cash-closing["cash"]
    final = sum(p["marketValue"] for p in closing["positions"])+closing["cash"]
    value_difference = None if missing else previous-final
    reconciled = not differences and abs(cash_difference) < 0.01 and value_difference is not None and abs(value_difference) < 0.02
    eligible = bool(complete and reconciled and not missing and all(p["return"] is not None for p in series))
    flows.append((end,final))
    return {"methodologyVersion":"ledger-eod-v1", "date":end.isoformat(), "startDate":start.isoformat(),
            "reconciled":reconciled, "performanceAvailable":eligible,
            "endingPositions":{symbol:quantity for symbol,quantity in shares.items() if abs(quantity)>1e-8},
            "quantityDifferences":differences,"cashDifference":cash_difference,"valueDifference":value_difference,
            "missingPrices":missing,"history":series,"timeWeightedReturn":growth-1 if eligible else None,
            "moneyWeightedReturnAnnualized":money_weighted_return(flows) if eligible else None,
            "warnings":["Events and external flows are treated as end-of-day; execution timing is not modeled",
                        "Money-weighted return is withheld for nonconventional cash flows or no unique bracketed root"]
                       + ([] if complete else ["Transaction import not attested complete"])}


class LedgerService:
    def __init__(self,path):
        self.path, self.store = path, RecordRepository(path)

    def import_ledger(self, body):
        opening = self.store.get("snapshot",body.openingSnapshotId)["payload"]
        closing = self.store.get("snapshot",body.closingSnapshotId)["payload"]
        if opening["account"] != body.account or closing["account"] != body.account:
            raise ValueError("Ledger account must match both checkpoints")
        if closing["date"] <= opening["date"] or any(not opening["date"] < event.date.isoformat() <= closing["date"] for event in body.events):
            raise ValueError("Ledger events must fall strictly after opening and no later than closing checkpoint")
        if body.supersedes:
            prior = self.store.get("ledger",body.supersedes)["payload"]
            if prior["account"] != body.account:
                raise ValueError("Ledger correction must retain its account")
        return self.store.put("ledger",body.importKey,body.model_dump(mode="json"))

    def performance(self, identifier):
        ledger = self.store.get("ledger",identifier)["payload"]
        opening = self.store.get("snapshot",ledger["openingSnapshotId"])["payload"]
        closing = self.store.get("snapshot",ledger["closingSnapshotId"])["payload"]
        symbols = sorted({p["symbol"] for p in opening["positions"]+closing["positions"]} | {e["symbol"] for e in ledger["events"] if e["symbol"]})
        history = MarketDataRepository(self.path,read_only=True).load(symbols,start=date.fromisoformat(opening["date"]),end=date.fromisoformat(closing["date"]))
        # Yahoo Close is split-adjusted. Reconstruct the opening-share basis using
        # declared splits within this period; without a complete action ledger no
        # actual performance is claimed. Terminal values must still reconcile.
        import polars as pl
        for event in ledger["events"]:
            if event["kind"] == "split":
                history = history.with_columns(pl.when((pl.col("Symbol")==event["symbol"]) & (pl.col("Date")<date.fromisoformat(event["date"])))
                    .then(pl.col("Close")*event["ratio"]).otherwise(pl.col("Close")).alias("Close"))
        return {**ledger_performance(opening,closing,ledger["events"],history,ledger["complete"]),
                "provider":"yahoo","currency":"USD","priceBasis":"close_reconstructed_with_declared_splits"}
