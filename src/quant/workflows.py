"""Application services for snapshots and reproducible external-agent research."""
from datetime import date
import hashlib
import json
from pathlib import Path
import uuid

import polars as pl

from quant.analysis import (analyze_portfolio, summarize_allocation,
    calculate_daily_returns, summarize_risk_metrics, calculate_benchmark_metrics,
    calculate_portfolio_history, calculate_endpoint_return_contributions,
    calculate_static_weight_variance_risk_contributions, calculate_pairwise_correlations)
from quant.calendars import sessions
from quant.contracts import SnapshotInput, RunInput, ReportInput
from quant.database import backup_database, initialize_database, database_connection
from quant.ingestion import ingestion_lock
from quant.market_store import MarketDataRepository
from quant.record_store import RecordRepository, canonical

METHOD_VERSION = "portfolio-review-v1"
PERIODS = {"1mo":21, "3mo":63, "6mo":126, "1y":252, "2y":504, "5y":1260}


def calculation_sources():
    root = Path(__file__).parent
    return {name: (root/name).read_text() for name in ("analysis.py","workflows.py","calendars.py","market_store.py")}


def records(frame):
    # Polars performs date conversion itself; no pandas crosses this boundary.
    import re
    percent_columns = [name for name in frame.columns if name.endswith(" %")]
    if percent_columns:
        frame = frame.with_columns(pl.col(name)/100 for name in percent_columns)
    names = {}
    for name in frame.columns:
        if name == "As Of":
            names[name] = "date"
        elif name == "Weight %":
            names[name] = "weight"
        elif name == "Gain/Loss %":
            names[name] = "gainReturn"
        else:
            parts = re.findall(r"[A-Za-z0-9]+", name)
            names[name] = parts[0][0].lower()+parts[0][1:]+"".join(p[0].upper()+p[1:] for p in parts[1:])
    frame = frame.rename(names)
    return json.loads(frame.write_json())


def positions_frame(snapshot):
    return pl.DataFrame([{"Symbol": p["symbol"], "Quantity": p["quantity"],
                         "Average Cost": p["averageCost"] or 0,
                         "Sector": p["sector"], "Asset Class": p["assetClass"]}
                        for p in snapshot["positions"]],
                        schema={"Symbol":pl.String, "Quantity":pl.Float64, "Average Cost":pl.Float64,
                                "Sector":pl.String, "Asset Class":pl.String})


def portfolio_review(path, snapshot, previous=None, period="1y", benchmark="SPY"):
    """Pure orchestration over an explicit date and stored dataset."""
    if snapshot["currency"] != "USD" or any(p["currency"] != "USD" for p in snapshot["positions"]):
        raise ValueError("Portfolio review currently supports USD positions; FX conversion is unavailable")
    as_of = date.fromisoformat(snapshot["date"])
    symbols = [p["symbol"] for p in snapshot["positions"]]
    market = MarketDataRepository(path, read_only=True)
    history = market.load(list(dict.fromkeys([*symbols, benchmark])), end=as_of)
    warnings = [{"code":"current_holdings_backcast", "message":"Historical risk uses today's snapshot quantities, not actual account performance"},
                {"code":"zero_risk_free_rate", "message":"Sharpe, Sortino and benchmark alpha use zero risk-free rate and 252 sessions"}]
    with database_connection(path,read_only=True) as db:
        metadata={r["symbol"]:dict(r) for r in db.execute("SELECT symbol,currency,calendar FROM securities")}
    for position in snapshot["positions"]:
        currency=metadata.get(position["symbol"],{}).get("currency")
        if currency and currency!=position["currency"]:
            raise ValueError("Snapshot and stored security currencies disagree")
    unverified=[s for s in [*symbols,benchmark] if metadata.get(s,{}).get("calendar")!="XNYS" or metadata.get(s,{}).get("currency")!="USD"] if symbols else []
    if unverified:
        warnings.append({"code":"unverified_security_metadata", "symbols":unverified,
                         "message":"Valuation uses declared snapshot currency; joint risk needs verified USD/XNYS security metadata"})
    frame = positions_frame(snapshot)
    equity = history.filter(pl.col("Symbol").is_in(symbols))
    current = analyze_portfolio(frame, equity).sort("Symbol")
    priced = current.filter(pl.col("Market Value").is_not_null())
    unpriced = current.filter(pl.col("Market Value").is_null())["Symbol"].to_list()
    if unpriced:
        warnings.append({"code":"unpriced_positions", "symbols":unpriced})
    unknown_cost = any(p["averageCost"] is None for p in snapshot["positions"])
    if unknown_cost:
        warnings.append({"code":"unknown_cost_basis", "message":"Cost and gain fields are omitted because some costs are unknown"})
        current = current.drop("Average Cost", "Cost Basis", "Gain/Loss", "Gain/Loss %")
    if snapshot["cash"] is None:
        warnings.append({"code":"unknown_cash", "message":"Weights describe priced securities only; total account value is unknown"})
    account_value = None if unpriced or snapshot["cash"] is None else priced["Market Value"].sum()+snapshot["cash"]
    observed_value = (sum(p["marketValue"] for p in snapshot["positions"])+snapshot["cash"]
                      if snapshot["cash"] is not None and all(p["marketValue"] is not None for p in snapshot["positions"]) else None)
    allocations = {}
    for name,column in [("sector","Sector"),("assetClass","Asset Class")]:
        allocation = summarize_allocation(priced,column).sort(column) if priced.height else None
        if allocation is not None and unknown_cost:
            allocation = allocation.drop("Cost Basis","Gain/Loss")
        allocations[name] = records(allocation) if allocation is not None else []
    prior = {p["symbol"]:p["quantity"] for p in previous["positions"]} if previous else {}
    quantities = {p["symbol"]:p["quantity"] for p in snapshot["positions"]}
    changes = ([{"symbol":s, "previousQuantity":prior.get(s,0), "quantity":quantities.get(s,0),
                 "quantityDifference":quantities.get(s,0)-prior.get(s,0)}
                for s in sorted(set(prior)|set(quantities))] if previous else [])
    weights = [v/100 for v in priced["Weight %"].to_list()]
    result = {"methodologyVersion":METHOD_VERSION, "date":snapshot["date"], "period":period,
              "provider":"yahoo", "currency":"USD", "priceBasis":"adjustedClose_for_returns_close_for_valuation",
              "benchmark":benchmark, "account":snapshot["account"], "positions":records(current),
              "cash":snapshot["cash"], "accountValue":account_value, "observedSnapshotValue":observed_value,
              "allocations":allocations, "concentration":{"largestWeight":max(weights,default=None),
                  "herfindahl":sum(w*w for w in weights) if weights else None, "basis":"priced_securities"},
              "snapshotChanges":changes, "changeInterpretation":"Observed quantity differences; trades and returns are not inferred",
              "risk":None, "warnings":warnings,
              "definitions":{"beta":"Historical sensitivity to the chosen benchmark; not a forecast",
                 "riskContribution":"Share of modeled variance, using static latest-date weights",
                 "backcast":"Hypothetical behavior of current holdings; not account performance"}}
    if equity.height:
        last_by_symbol = equity.group_by("Symbol").agg(pl.col("Date").max())
        expected = sessions(as_of.replace(day=1), as_of)
        if expected and last_by_symbol.filter(pl.col("Date") < expected[-1]).height:
            warnings.append({"code":"stale_prices", "message":"Some valuations precede the snapshot's latest completed trading session"})
    if not symbols:
        return result
    clean = history.drop_nulls("Adjusted Close")
    benchmark_history = clean.filter(pl.col("Symbol") == benchmark).tail(PERIODS[period]+1)
    dates = benchmark_history["Date"].to_list()
    if len(dates) < PERIODS[period]+1:
        warnings.append({"code":"insufficient_history", "message":"Full requested benchmark period unavailable"})
        return result
    if unverified:
        return result
    if dates != sessions(dates[0], dates[-1]):
        warnings.append({"code":"benchmark_gaps", "message":"Benchmark has internal session gaps"})
        return result
    from datetime import timedelta
    expected_session = sessions(as_of-timedelta(days=14),as_of)[-1]
    if dates[-1] < expected_session:
        warnings.append({"code":"stale_benchmark", "message":"Risk period ends before the latest snapshot-date trading session"})
    assets = clean.filter(pl.col("Symbol").is_in(symbols) & pl.col("Date").is_in(dates))
    counts = dict(assets.group_by("Symbol").len().iter_rows())
    if any(counts.get(s,0) != len(dates) for s in symbols):
        warnings.append({"code":"incomplete_common_history", "message":"Risk withheld because not every holding has the full benchmark calendar"})
        return result
    if len(symbols) > 50:
        warnings.append({"code":"risk_symbol_limit", "message":"Joint risk is bounded to 50 holdings"})
        return result
    # Normalize total-return price levels to observed terminal closes. Adjusted
    # close levels are otherwise arbitrary across securities and data vintages.
    terminal = assets.filter(pl.col("Date") == dates[-1]).select("Symbol",
        (pl.col("Close")/pl.col("Adjusted Close")).alias("scale"))
    assets = assets.join(terminal,on="Symbol").with_columns((pl.col("Adjusted Close")*pl.col("scale")).alias("Adjusted Close")).drop("scale")
    portfolio = calculate_portfolio_history(frame, assets)
    returns = calculate_daily_returns(assets)
    portfolio_returns = portfolio.select("Date", pl.lit("Portfolio").alias("Symbol"), pl.col("Portfolio Return").alias("Return"))
    bench_returns = calculate_daily_returns(benchmark_history).select("Date", "Return")
    latest = assets.filter(pl.col("Date") == dates[-1]).join(frame.select("Symbol", "Quantity"), on="Symbol")
    risk_weights = latest.with_columns((pl.col("Adjusted Close")*pl.col("Quantity")).alias("value")).select("Symbol", (pl.col("value")/pl.col("value").sum()).alias("Weight"))
    result["risk"] = {"basis":"securities_only", "startDate":dates[0].isoformat(), "endDate":dates[-1].isoformat(),
        "metrics":records(summarize_risk_metrics(portfolio_returns)),
        "benchmarkMetrics":records(calculate_benchmark_metrics(portfolio_returns, bench_returns)),
        "history":records(portfolio), "returnContributions":records(calculate_endpoint_return_contributions(frame,assets)),
        "riskContributions":records(calculate_static_weight_variance_risk_contributions(returns,risk_weights)),
        "correlations":records(calculate_pairwise_correlations(returns)), "observations":len(dates)-1}
    warnings.append({"code":"static_weight_risk", "message":"Variance contributions use static latest-date weights; cash is excluded"})
    warnings.append({"code":"cash_excluded_from_risk", "message":"All hypothetical risk metrics describe securities only; cash and borrowing are not modeled"})
    return result


class ResearchWorkflow:
    def __init__(self, path):
        self.path = Path(path)
        self.store = RecordRepository(self.path)

    def import_snapshot(self, body: SnapshotInput):
        return self.store.put("snapshot", body.importKey, body.model_dump(mode="json"))

    def previous_snapshot(self, snapshot):
        # Query JSON dates, not import order; backfilled imports must not change chronology.
        from quant.database import database_connection
        with database_connection(self.path, read_only=True) as db:
            row = db.execute("""SELECT * FROM records WHERE user_id=? AND kind='snapshot'
                AND json_extract(payload,'$.account')=? AND json_extract(payload,'$.date')<?
                ORDER BY json_extract(payload,'$.date') DESC,created_at DESC,id DESC LIMIT 1""",
                (self.store.user_id, snapshot["account"], snapshot["date"])).fetchone()
        return self.store._decode(row)["payload"] if row else None

    def review(self, snapshot_id, period="1y", benchmark="SPY"):
        snapshot = self.store.get("snapshot", snapshot_id)["payload"]
        return portfolio_review(self.path, snapshot, self.previous_snapshot(snapshot), period, benchmark)

    def create_run(self, body: RunInput):
        initialize_database(self.path)
        request = body.model_dump(mode="json")
        # A separate lock serializes run creation, not ingestion; SQLite backup provides a consistent snapshot.
        with ingestion_lock(self.path.with_name(self.path.name+".research")):
            existing = self.store.by_key("run", body.importKey)
            if existing:
                if existing["payload"]["request"] != request:
                    raise ValueError("Run identity already exists with different parameters")
                return existing
            identifier = uuid.uuid4().hex
            artifact = self.path.parent / "artifacts" / (identifier+".db")
            backup_database(self.path, artifact)
            try:
                frozen = ResearchWorkflow(artifact)
                snapshot = frozen.store.get("snapshot", body.snapshotId)["payload"]
                previous = frozen.previous_snapshot(snapshot)
                result = portfolio_review(artifact, snapshot, previous, body.period, body.benchmark)
                payload = {"request":request, "datasetFile":artifact.name,
                    "datasetSha256":file_digest(artifact), "methodologyVersion":METHOD_VERSION,
                    "snapshot":snapshot, "previousSnapshot":previous, "result":result,
                    "temporalMeaning":"Frozen observed dataset; not proof of historical information availability"}
                payload["calculationSources"] = calculation_sources()
                payload["calculationDigest"] = hashlib.sha256(canonical(payload["calculationSources"]).encode()).hexdigest()
                return self.store.put("run", body.importKey, payload)
            except BaseException:
                artifact.unlink(missing_ok=True)
                raise

    def dataset(self, run):
        name = run["datasetFile"]
        if Path(name).name != name:
            raise ValueError("Invalid dataset reference")
        path = self.path.parent / "artifacts" / name
        if file_digest(path) != run["datasetSha256"]:
            raise ValueError("Frozen dataset integrity check failed")
        return path

    def replay(self, run_id):
        run = self.store.get("run", run_id)["payload"]
        if run["methodologyVersion"] != METHOD_VERSION:
            raise ValueError("Replay requires the original methodology version")
        if run["calculationDigest"] != hashlib.sha256(canonical(calculation_sources()).encode()).hexdigest():
            raise ValueError("Replay requires the original calculation source revision, preserved in the run record")
        result = portfolio_review(self.dataset(run), run["snapshot"], run["previousSnapshot"],
                                  run["request"]["period"], run["request"]["benchmark"])
        return {"matches":canonical(result)==canonical(run["result"]), "result":result}

    def save_report(self, body: ReportInput):
        run = self.store.get("run", body.runId)["payload"]
        required = {w["code"] for w in run["result"]["warnings"]}
        if not required.issubset(body.acknowledgedWarnings):
            raise ValueError("Report must acknowledge every run warning")
        for claim in body.claims:
            for reference in claim.evidence:
                if reference.startswith("result/"):
                    value = run["result"]
                    try:
                        for key in reference.split("/")[1:]:
                            value = value[int(key)] if isinstance(value, list) else value[key]
                    except (KeyError, IndexError, ValueError, TypeError) as error:
                        raise ValueError("Invalid result evidence path") from error
                else:
                    # Durable external evidence is referenced by immutable record id.
                    self.store.get("evidence", reference)
        return self.store.put("report", body.importKey, body.model_dump(mode="json"))


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()
