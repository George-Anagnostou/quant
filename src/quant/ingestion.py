"""Single-writer, resumable ingestion. Downloads never run in a DB transaction."""
from collections.abc import Callable
from contextlib import contextmanager
from datetime import date, datetime, timedelta
import fcntl
import json
import logging
from pathlib import Path
import threading
import time
from typing import Protocol
import uuid

import polars as pl

from quant.calendars import EASTERN, latest_completed_session, sessions
from quant.database import database_connection, initialize_database
from quant.market_store import MarketDataRepository
from quant.record_store import utc_now
from quant.user_data import UserDataRepository

logger = logging.getLogger(__name__)


class MarketProvider(Protocol):
    name: str
    def constituents(self) -> pl.DataFrame: ...
    def history(self, symbols: list[str], start: date) -> pl.DataFrame: ...


class YahooProvider:
    name = "yahoo"

    def constituents(self):
        from quant.market_data import get_sp500_constituents
        return get_sp500_constituents()

    def history(self, symbols, start):
        from quant.market_data import get_market_history
        return get_market_history(symbols, start)


@contextmanager
def ingestion_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".ingestion.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Ingestion is already running") from error
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class IngestionService:
    def __init__(self, path: Path, provider: MarketProvider | None = None,
                 sleep: Callable = time.sleep):
        self.path, self.provider, self.sleep = path, provider or YahooProvider(), sleep
        self.market = MarketDataRepository(path)
        self.known_us_symbols = set()

    def synchronize(self, *, horizon=None, batch_size=50, today=None,
                    symbols=None, benchmarks=("SPY",), run_id=None):
        latest = today or latest_completed_session()
        if horizon and horizon > latest or not 1 <= batch_size <= 100:
            raise ValueError("Invalid synchronization horizon or batch size")
        if not 2010 <= latest.year <= 2030 or (horizon and not 2010 <= horizon.year <= 2030):
            raise ValueError("Synchronization calendar supports 2010-2030")
        with ingestion_lock(self.path):
            initialize_database(self.path)
            # Only the lock owner can recover interrupted work.
            with database_connection(self.path) as db:
                previous = db.execute("SELECT id FROM ingestion_runs WHERE status='running' ORDER BY started_at LIMIT 1").fetchone()
            if previous:
                self._execute(previous["id"], batch_size, latest)
            universe_source = "explicit"
            if symbols is None:
                try:
                    members = self.provider.constituents()
                    if members.is_empty():
                        raise RuntimeError("Empty constituent response")
                    self.market.save_universe("sp500", members, observed_on=today or datetime.now(EASTERN).date())
                    universe_source = "remote"
                except RuntimeError:
                    universe_source = "stored"
                    logger.warning("Universe refresh failed; continuing with stored and personal symbols")
                universe = self.market.list_universe_symbols("sp500")
                users = UserDataRepository(self.path)
                personal = [*users.list_watchlist(), *users.positions_frame()["Symbol"].to_list(), *benchmarks]
                # Snapshot imports are durable tracking inputs, without modifying legacy lots.
                with database_connection(self.path, read_only=True) as db:
                    for row in db.execute("SELECT payload FROM records WHERE kind='snapshot'"):
                        personal.extend(p["symbol"] for p in json.loads(row[0])["positions"])
                symbols = list(dict.fromkeys([*universe, *personal]))
            else:
                universe, personal = [], list(symbols)
            symbols = list(dict.fromkeys(s.strip().upper() for s in symbols))
            self.known_us_symbols = set(universe) | {"SPY"}
            if not symbols or len(symbols) > 2000:
                raise ValueError("Sync requires 1-2000 symbols")
            starts = {s: horizon or date(max(2010, latest.year-(10 if s in personal else 5)), 1, 1)
                      for s in symbols}
            run_id = run_id or str(uuid.uuid4())
            with database_connection(self.path, read_only=True) as db:
                existing = db.execute("SELECT id FROM ingestion_runs WHERE id=?", (run_id,)).fetchone()
                if existing:
                    return self.run(run_id)
            jobs = []
            for symbol in symbols:
                start, reasons = self.plan(symbol, starts[symbol], latest)
                jobs.append((run_id, symbol, start.isoformat(), json.dumps(reasons)))
            # Publish the entire plan atomically. Interrupted planning leaves an
            # explicit request queued; recovery never sees a truncated job list.
            with database_connection(self.path) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO ingestion_runs VALUES (?,?,NULL,'running',?)", (run_id, utc_now(), universe_source))
                db.executemany("INSERT INTO ingestion_jobs(run_id,symbol,start_date,reasons,status) VALUES (?,?,?,?, 'pending')", jobs)
            self._execute(run_id, batch_size, latest)
            return self.run(run_id)

    def plan(self, symbol, horizon, latest):
        history = self.market.load([symbol], provider=self.provider.name)
        if history.is_empty():
            return horizon, ["backfill"]
        first, last = history["Date"].min(), history["Date"].max()
        starts, reasons = [max(horizon, last-timedelta(days=14))], ["correction_overlap"]
        with database_connection(self.path, read_only=True) as db:
            coverage = db.execute("SELECT requested_start FROM sync_coverage WHERE symbol=?", (symbol,)).fetchone()
            security = db.execute("SELECT calendar FROM securities WHERE symbol=?", (symbol,)).fetchone()
        if first > horizon and (not coverage or horizon.isoformat() < coverage[0]):
            starts.append(horizon)
            reasons.append("missing_prefix")
        calendar = security["calendar"] if security else None
        missing = (sorted(set(sessions(max(horizon, first), min(last, latest), calendar)) - set(history["Date"]))
                   if calendar == "XNYS" and max(horizon,first) <= min(last,latest) else [])
        if missing:
            starts.append(missing[0])
            reasons.append("internal_gap")
        if last < latest:
            reasons.append("recent_sessions")
        return min(starts), reasons

    def _execute(self, run_id, batch_size, latest):
        with database_connection(self.path, read_only=True) as db:
            jobs = [dict(r) for r in db.execute("SELECT * FROM ingestion_jobs WHERE run_id=? AND status!='complete' ORDER BY start_date,symbol", (run_id,))]
        for offset in range(0, len(jobs), batch_size):
            batch = jobs[offset:offset+batch_size]
            start = min(date.fromisoformat(j["start_date"]) for j in batch)
            frame = None
            try:
                frame = self.provider.history([j["symbol"] for j in batch], start)
            except Exception:
                logger.warning("Batch download failed; retrying symbols individually")
            for job in batch:
                symbol = job["symbol"]
                with database_connection(self.path) as db:
                    db.execute("UPDATE ingestion_jobs SET status='running',attempts=attempts+1 WHERE run_id=? AND symbol=?", (run_id, symbol))
                try:
                    part = frame.filter(pl.col("Symbol") == symbol) if frame is not None else None
                    if part is None or part.is_empty():
                        part = self._download(symbol, date.fromisoformat(job["start_date"]))
                    if part.is_empty():
                        raise ValueError("Provider returned no completed observations")
                    rows = self._publish(symbol, part, latest)
                    with database_connection(self.path) as db:
                        db.execute("UPDATE ingestion_jobs SET status='complete',saved_rows=?,error=NULL WHERE run_id=? AND symbol=?", (rows, run_id, symbol))
                        db.execute("INSERT INTO sync_coverage VALUES (?,?,?) ON CONFLICT(symbol) DO UPDATE SET requested_start=MIN(requested_start,excluded.requested_start),checked_at=excluded.checked_at",
                                   (symbol, job["start_date"], utc_now()))
                    self._audit_gaps(symbol, latest)
                except Exception as error:
                    logger.warning("Ingestion failed for %s: %s", symbol, type(error).__name__)
                    with database_connection(self.path) as db:
                        db.execute("UPDATE ingestion_jobs SET status='failed',error=? WHERE run_id=? AND symbol=?", (type(error).__name__, run_id, symbol))
                    self.issue(symbol, "ingestion_failed", "", "Provider or validation failure; previous observations retained")
        with database_connection(self.path) as db:
            failures = db.execute("SELECT COUNT(*) FROM ingestion_jobs WHERE run_id=? AND status!='complete'", (run_id,)).fetchone()[0]
            db.execute("UPDATE ingestion_runs SET status=?,finished_at=? WHERE id=?", ("partial" if failures else "complete", utc_now(), run_id))

    def _download(self, symbol, start):
        for attempt in range(3):
            try:
                return self.provider.history([symbol], start)
            except Exception:
                if attempt == 2:
                    raise
                self.sleep(0.25 * 2**attempt)

    def _publish(self, symbol, incoming, latest):
        required = {"Date", "Symbol", "Open", "High", "Low", "Close", "Adjusted Close", "Volume"}
        if not required.issubset(incoming.columns):
            raise ValueError("Provider frame lacks canonical daily-bar columns")
        incoming = incoming.filter(pl.col("Date") <= latest)
        if incoming.is_empty() or incoming.filter(pl.col("Symbol") != symbol).height:
            raise ValueError("Invalid provider symbol/date range")
        old = self.market.load([symbol], provider=self.provider.name)
        changed = False
        fields = ["Open", "High", "Low", "Close", "Adjusted Close", "Volume"]
        if not old.is_empty():
            paired = old.join(incoming, on=["Date", "Symbol"], suffix="_new")
            for field in fields:
                for a, b in zip(paired[field], paired[field+"_new"]):
                    if a is not None and b is None:
                        raise ValueError("Refresh would remove existing prices")
                    if a is not None and b is not None and abs(a-b) > max(1e-8, abs(a)*1e-7):
                        changed = True
            if changed:
                replacement = self._download(symbol, min(old["Date"].min(), incoming["Date"].min()))
                if not required.issubset(replacement.columns) or replacement.filter(pl.col("Symbol") != symbol).height:
                    raise ValueError("Revision has invalid symbols or columns")
                replacement = replacement.filter(pl.col("Date") <= latest)
                if set(old["Date"]) - set(replacement["Date"]):
                    raise ValueError("Revision does not cover previously stored history")
                old_fields = old.select("Date",*fields).join(replacement.select("Date",*fields), on="Date", suffix="_new")
                if old_fields.filter(pl.any_horizontal(pl.col(field).is_not_null() & pl.col(field+"_new").is_null() for field in fields)).height:
                    raise ValueError("Revision drops existing observations")
                incoming = replacement
        with database_connection(self.path,read_only=True) as db:
            alias = db.execute("SELECT s.symbol FROM provider_symbols p JOIN securities s ON s.id=p.security_id WHERE provider=? AND provider_symbol=?",
                               (self.provider.name,symbol.replace('.', '-'))).fetchone()
            if alias and alias[0] != symbol:
                raise ValueError("Provider symbol already belongs to another canonical security")
        # save validates the entire frame before the per-symbol atomic upsert.
        saved = self.market.save(incoming, provider=self.provider.name)
        with database_connection(self.path) as db:
            if symbol in self.known_us_symbols:
                db.execute("UPDATE securities SET currency=COALESCE(currency,'USD'),calendar=COALESCE(calendar,'XNYS'),instrument_type=COALESCE(instrument_type,'us_listed') WHERE symbol=?", (symbol,))
            db.execute("INSERT INTO provider_symbols SELECT id,?,? FROM securities WHERE symbol=? ON CONFLICT(security_id,provider) DO UPDATE SET provider_symbol=excluded.provider_symbol",
                       (self.provider.name, symbol.replace('.', '-'), symbol))
            db.execute("UPDATE data_issues SET resolved_at=? WHERE symbol=? AND code='ingestion_failed'", (utc_now(), symbol))
        return saved

    def issue(self, symbol, code, day, detail):
        with database_connection(self.path) as db:
            db.execute("INSERT INTO data_issues VALUES (?,?,?,?,?,NULL,?) ON CONFLICT(symbol,code,session_date) DO UPDATE SET last_seen=excluded.last_seen,resolved_at=NULL,detail=excluded.detail",
                       (symbol, code, day, utc_now(), utc_now(), detail))

    def _audit_gaps(self, symbol, latest):
        history = self.market.load([symbol], provider=self.provider.name)
        first = history["Date"].min()
        with database_connection(self.path) as db:
            calendar = db.execute("SELECT calendar FROM securities WHERE symbol=?", (symbol,)).fetchone()[0]
            db.execute("UPDATE data_issues SET resolved_at=? WHERE symbol=? AND code='missing_session'", (utc_now(), symbol))
            db.execute("UPDATE data_issues SET resolved_at=? WHERE symbol=? AND code='incomplete_bar'", (utc_now(), symbol))
            checked = db.execute("SELECT requested_start FROM sync_coverage WHERE symbol=?",(symbol,)).fetchone()
        if checked and checked[0] < first.isoformat():
            self.issue(symbol,"history_starts_late",first.isoformat(),"Provider history begins after requested horizon; pre-listing status has not been established")
        if calendar == "XNYS":
            for day in sorted(set(sessions(first, latest)) - set(history["Date"])):
                self.issue(symbol, "missing_session", day.isoformat(), "Missing within observed history; listing/delisting status not established")
        else:
            self.issue(symbol, "unknown_calendar", "", "Security calendar/currency requires explicit metadata before completeness can be established")
        for row in history.filter(pl.any_horizontal(pl.col(c).is_null() for c in ["Open", "High", "Low", "Volume", "Adjusted Close"])).to_dicts():
            self.issue(symbol, "incomplete_bar", row["Date"].isoformat(), "Incomplete OHLCV or adjusted close")

    def run(self, identifier):
        with database_connection(self.path, read_only=True) as db:
            run = db.execute("SELECT * FROM ingestion_runs WHERE id=?", (identifier,)).fetchone()
            if run is None:
                raise ValueError("Unknown ingestion run")
            return {**dict(run), "jobs": [dict(r) for r in db.execute("SELECT * FROM ingestion_jobs WHERE run_id=? ORDER BY symbol", (identifier,))]}


class IngestionWorker:
    def __init__(self, path, **options):
        self.path, self.options = path, options
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="quant-ingestion", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=5)

    def _loop(self):
        last_session = None
        while not self.stop_event.is_set():
            try:
                initialize_database(self.path)
                with database_connection(self.path,read_only=True) as db:
                    pending = db.execute("""SELECT r.* FROM records r LEFT JOIN ingestion_runs i ON i.id=r.id
                        WHERE r.kind='sync_request' AND i.id IS NULL ORDER BY r.created_at LIMIT 20""").fetchall()
                for row in pending:
                    request = json.loads(row["payload"])
                    IngestionService(self.path).synchronize(symbols=request["symbols"],
                        horizon=date.fromisoformat(request["horizon"]) if request["horizon"] else None,
                        run_id=row["id"])
                latest = latest_completed_session()
                if latest != last_session:
                    IngestionService(self.path).synchronize(**self.options)
                    from quant.database import backup_database
                    backup = self.path.with_name(f"{self.path.name}.{latest.isoformat()}.backup")
                    if not backup.exists():
                        backup_database(self.path, backup)
                    last_session = latest
            except Exception:
                logger.exception("Background synchronization failed")
            self.stop_event.wait(60)
