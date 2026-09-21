import asyncio
from contextlib import closing
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import polars as pl

from quant.calendars import sessions, latest_completed_session
from quant.contracts import SnapshotInput, RunInput, ReportInput, EvidenceInput, LedgerInput
from quant.database import initialize_database, database_connection, backup_database, _create_schema
from quant.discovery import DiscoveryService, momentum_experiment
from quant.fundamentals import FundamentalService, SECProvider
from quant.ingestion import IngestionService, IngestionWorker
from quant.ledger import ledger_performance, money_weighted_return
from quant.market_store import MarketDataRepository
from quant.record_store import RecordRepository
from quant.workflows import ResearchWorkflow


def bars(days, symbols=("AAPL","SPY")):
    rows=[]
    for i,day in enumerate(days):
        for symbol in symbols:
            price=100+i+(i%3)*0.1
            rows.append({"Date":day,"Symbol":symbol,"Close":price,"Adjusted Close":price,
                         "Open":price,"High":price+1,"Low":price-1,"Volume":1000})
    return pl.DataFrame(rows)


def snapshot(key="snapshot1",day="2026-08-31",quantity=10, cash=1000, value=1500):
    return SnapshotInput(importKey=key,date=day,account="broker",source="export",cash=cash,
        positions=[{"symbol":"AAPL","quantity":quantity,"averageCost":90,"marketValue":value}])


class FakeProvider:
    name="yahoo"
    def __init__(self,history):
        self.frame=history
        self.calls=[]
    def constituents(self):
        return pl.DataFrame({"Symbol":["AAPL"],"Company":["Apple"]})
    def history(self,symbols,start):
        self.calls.append((symbols,start))
        return self.frame.filter(pl.col("Symbol").is_in(symbols) & (pl.col("Date")>=start))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.directory=TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path=Path(self.directory.name)/"quant.db"
        initialize_database(self.path)

    def seed(self):
        days=sessions(date(2026,6,1),date(2026,8,31))
        MarketDataRepository(self.path).save(bars(days))
        with database_connection(self.path) as db:
            db.execute("UPDATE securities SET currency='USD',calendar='XNYS',instrument_type='equity'")
        return days

    def test_migration_preserves_v1_and_creates_restorable_backup(self):
        old=Path(self.directory.name)/"old.db"
        with closing(sqlite3.connect(old)) as db:
            _create_schema(db)
            db.execute("PRAGMA user_version=1")
            db.execute("INSERT INTO watchlist VALUES ('local-admin','AAPL',0)")
            db.commit()
        initialize_database(old)
        with database_connection(old,read_only=True) as db:
            self.assertEqual(db.execute("SELECT symbol FROM retired_watchlist").fetchone()[0],"AAPL")
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],3)
        copies=list(old.parent.glob("old.db.v1-*.backup"))
        self.assertEqual(len(copies),1)
        with closing(sqlite3.connect(copies[0])) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],1)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0],"ok")

    def test_backup_does_not_overwrite_existing_destination(self):
        destination=Path(self.directory.name)/"backup.db"
        backup_database(self.path,destination)
        before=destination.read_bytes()
        with self.assertRaises(FileExistsError):
            backup_database(self.path,destination)
        self.assertEqual(before,destination.read_bytes())

    def test_calendar_holidays_special_closure_and_settlement(self):
        self.assertNotIn(date(2025,1,9),sessions(date(2025,1,8),date(2025,1,10)))
        self.assertNotIn(date(2026,6,19),sessions(date(2026,6,18),date(2026,6,22)))
        self.assertIn(date(2021,12,31),sessions(date(2021,12,30),date(2022,1,4)))
        self.assertEqual(latest_completed_session(datetime(2026,9,7,18,tzinfo=timezone.utc)),date(2026,9,4))

    def test_snapshot_import_idempotent_and_conflicts_rejected(self):
        service=ResearchWorkflow(self.path)
        first=service.import_snapshot(snapshot())
        self.assertEqual(first,service.import_snapshot(snapshot()))
        with self.assertRaises(ValueError):
            service.import_snapshot(snapshot(quantity=20))
        with database_connection(self.path,read_only=True) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0],1)

    def test_frozen_run_replays_after_live_prices_and_positions_change(self):
        days=self.seed()
        service=ResearchWorkflow(self.path)
        snap=service.import_snapshot(snapshot())
        body=RunInput(importKey="run1",snapshotId=snap["id"],period="1mo")
        run=service.create_run(body)
        self.assertIsNotNone(run["payload"]["result"]["risk"])
        MarketDataRepository(self.path).save(bars(days).with_columns((pl.col("Close")*2).alias("Close"),
            (pl.col("Open")*2).alias("Open"),(pl.col("High")*2).alias("High"),(pl.col("Low")*2).alias("Low")))
        service.import_snapshot(snapshot(key="later",quantity=30))
        self.assertTrue(service.replay(run["id"])["matches"])
        self.assertEqual(service.create_run(body)["id"],run["id"])
        self.assertNotEqual(service.review(snap["id"],"1mo")["positions"],run["payload"]["result"]["positions"])

    def test_run_detects_tampered_dataset(self):
        self.seed()
        service=ResearchWorkflow(self.path)
        snap=service.import_snapshot(snapshot())
        run=service.create_run(RunInput(importKey="r",snapshotId=snap["id"],period="1mo"))
        artifact=self.path.parent/"artifacts"/run["payload"]["datasetFile"]
        with artifact.open("ab") as stream:
            stream.write(b"tampered")
        with self.assertRaisesRegex(ValueError,"integrity"):
            service.replay(run["id"])

    def test_report_requires_real_evidence_paths_and_warning_acknowledgment(self):
        self.seed()
        workflow=ResearchWorkflow(self.path)
        snap=workflow.import_snapshot(snapshot(cash=None))
        run=workflow.create_run(RunInput(importKey="r",snapshotId=snap["id"],period="1mo"))
        report=ReportInput(importKey="report",runId=run["id"],model="external",claims=[{
            "kind":"fact","text":"Cash is unknown","evidence":["result/cash"]}],
            counterarguments=[],openQuestions=["Verify cash"],acknowledgedWarnings=[],narrative="Review")
        with self.assertRaises(ValueError):
            workflow.save_report(report)
        report.acknowledgedWarnings=[w["code"] for w in run["payload"]["result"]["warnings"]]
        self.assertEqual(workflow.save_report(report)["kind"],"report")
        report.importKey="other"
        report.claims[0].evidence=["result/not-real"]
        with self.assertRaises(ValueError):
            workflow.save_report(report)

    def test_partial_risk_withheld_without_invented_account_value(self):
        MarketDataRepository(self.path).save(bars([date(2026,8,31)]))
        service=ResearchWorkflow(self.path)
        snap=service.import_snapshot(snapshot(cash=None))
        result=service.review(snap["id"])
        self.assertIsNone(result["risk"])
        self.assertIsNone(result["accountValue"])
        self.assertIn("insufficient_history",[w["code"] for w in result["warnings"]])

    def test_ingestion_backfills_prefix_and_old_gap(self):
        days=sessions(date(2026,8,3),date(2026,8,31))
        source=bars(days)
        MarketDataRepository(self.path).save(source.filter((pl.col("Date")>=days[3]) & (pl.col("Date")!=days[6])))
        with database_connection(self.path) as db:
            db.execute("UPDATE securities SET calendar='XNYS' WHERE symbol='AAPL'")
        provider=FakeProvider(source)
        service=IngestionService(self.path,provider,sleep=lambda _:None)
        start,reasons=service.plan("AAPL",days[0],days[-1])
        self.assertEqual(start,days[0])
        self.assertIn("missing_prefix",reasons)
        self.assertIn("internal_gap",reasons)
        run=service.synchronize(horizon=days[0],today=days[-1])
        self.assertEqual(run["status"],"complete")
        self.assertEqual(MarketDataRepository(self.path).load(["AAPL"]).height,len(days))

    def test_ingestion_batches_symbols_by_requested_start(self):
        old_day = date(2025, 1, 2)
        recent_day = date(2026, 8, 31)
        source = pl.concat(
            [bars([old_day], ("OLD",)), bars([recent_day], ("CURRENT", "SAME"))]
        )
        provider = FakeProvider(source)
        with database_connection(self.path) as db:
            db.execute(
                "INSERT INTO ingestion_runs VALUES ('starts',?,NULL,'running','explicit')",
                (datetime.now(timezone.utc).isoformat(),),
            )
            db.executemany(
                "INSERT INTO ingestion_jobs(run_id,symbol,start_date,reasons,status) VALUES ('starts',?,?,?,'pending')",
                [
                    ("OLD", old_day.isoformat(), "[]"),
                    ("CURRENT", recent_day.isoformat(), "[]"),
                    ("SAME", recent_day.isoformat(), "[]"),
                ],
            )

        IngestionService(self.path, provider, sleep=lambda _: None)._execute(
            "starts", 50, recent_day
        )

        self.assertEqual(
            provider.calls,
            [(["OLD"], old_day), (["CURRENT", "SAME"], recent_day)],
        )

    def test_systemic_batch_failure_has_bounded_provider_calls(self):
        symbols = [f"S{index}" for index in range(8)]
        provider = FakeProvider(bars([date(2026, 8, 31)], tuple(symbols)))
        market = MarketDataRepository(self.path)
        market.save(bars([date(2026, 8, 28)], tuple(symbols)))
        before = market.load(symbols).to_dicts()

        def fail(symbols, start):
            provider.calls.append((symbols, start))
            raise RuntimeError("offline")

        provider.history = fail
        with database_connection(self.path) as db:
            db.execute(
                "INSERT INTO ingestion_runs VALUES ('failed-batch',?,NULL,'running','explicit')",
                (datetime.now(timezone.utc).isoformat(),),
            )
            db.executemany(
                "INSERT INTO ingestion_jobs(run_id,symbol,start_date,reasons,status) VALUES ('failed-batch',?,'2026-08-31','[]','pending')",
                [(symbol,) for symbol in symbols],
            )

        service = IngestionService(self.path, provider, sleep=lambda _: None)
        service._execute("failed-batch", 50, date(2026, 8, 31))

        self.assertEqual(len(provider.calls), 3)
        self.assertEqual(service.run("failed-batch")["status"], "partial")
        self.assertTrue(
            all(job["status"] == "failed" for job in service.run("failed-batch")["jobs"])
        )
        self.assertEqual(market.load(symbols).to_dicts(), before)

    def test_batch_split_isolates_one_bad_symbol(self):
        symbols = ["BAD", "GOOD1", "GOOD2", "GOOD3"]
        day = date(2026, 8, 31)
        provider = FakeProvider(bars([day], tuple(symbols)))

        def selective(symbols, start):
            provider.calls.append((symbols, start))
            if "BAD" in symbols:
                raise RuntimeError("bad symbol")
            return provider.frame.filter(pl.col("Symbol").is_in(symbols))

        provider.history = selective
        with database_connection(self.path) as db:
            db.execute(
                "INSERT INTO ingestion_runs VALUES ('bad-symbol',?,NULL,'running','explicit')",
                (datetime.now(timezone.utc).isoformat(),),
            )
            db.executemany(
                "INSERT INTO ingestion_jobs(run_id,symbol,start_date,reasons,status) VALUES ('bad-symbol',?,'2026-08-31','[]','pending')",
                [(symbol,) for symbol in symbols],
            )

        service = IngestionService(self.path, provider, sleep=lambda _: None)
        service._execute("bad-symbol", 50, day)

        jobs = {job["symbol"]: job["status"] for job in service.run("bad-symbol")["jobs"]}
        self.assertEqual(len(provider.calls), 5)
        self.assertEqual(jobs["BAD"], "failed")
        self.assertTrue(all(jobs[symbol] == "complete" for symbol in symbols[1:]))

    def test_large_partial_batch_suppresses_individual_retries(self):
        symbols = [f"S{index}" for index in range(7)]
        day = date(2026, 8, 31)
        provider = FakeProvider(bars([day], tuple(symbols)))

        def partial(symbols, start):
            provider.calls.append((symbols, start))
            return provider.frame.filter(pl.col("Symbol") == symbols[0])

        provider.history = partial
        with database_connection(self.path) as db:
            db.execute(
                "INSERT INTO ingestion_runs VALUES ('partial-batch',?,NULL,'running','explicit')",
                (datetime.now(timezone.utc).isoformat(),),
            )
            db.executemany(
                "INSERT INTO ingestion_jobs(run_id,symbol,start_date,reasons,status) VALUES ('partial-batch',?,'2026-08-31','[]','pending')",
                [(symbol,) for symbol in symbols],
            )

        service = IngestionService(self.path, provider, sleep=lambda _: None)
        service._execute("partial-batch", 50, day)

        jobs = service.run("partial-batch")["jobs"]
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(sum(job["status"] == "complete" for job in jobs), 1)
        self.assertEqual(sum(job["status"] == "failed" for job in jobs), 6)

    def test_small_partial_batch_retries_missing_symbols_individually(self):
        symbols = ["FIRST", "SECOND"]
        day = date(2026, 8, 31)
        provider = FakeProvider(bars([day], tuple(symbols)))

        def partial(symbols, start):
            provider.calls.append((symbols, start))
            return provider.frame.filter(pl.col("Symbol") == symbols[0])

        provider.history = partial
        with database_connection(self.path) as db:
            db.execute(
                "INSERT INTO ingestion_runs VALUES ('small-partial',?,NULL,'running','explicit')",
                (datetime.now(timezone.utc).isoformat(),),
            )
            db.executemany(
                "INSERT INTO ingestion_jobs(run_id,symbol,start_date,reasons,status) VALUES ('small-partial',?,'2026-08-31','[]','pending')",
                [(symbol,) for symbol in symbols],
            )

        service = IngestionService(self.path, provider, sleep=lambda _: None)
        service._execute("small-partial", 50, day)

        self.assertEqual(provider.calls, [(["FIRST", "SECOND"], day), (["SECOND"], day)])
        self.assertTrue(
            all(job["status"] == "complete" for job in service.run("small-partial")["jobs"])
        )

    @patch("quant.ingestion.MAX_PROVIDER_CALLS_PER_EXECUTION", 3)
    def test_provider_call_budget_applies_across_start_date_groups(self):
        symbols = [f"S{index}" for index in range(5)]
        days = [date(2026, 8, 24 + index) for index in range(5)]
        provider = FakeProvider(
            pl.concat([bars([day], (symbol,)) for symbol, day in zip(symbols, days)])
        )
        with database_connection(self.path) as db:
            db.execute(
                "INSERT INTO ingestion_runs VALUES ('budget',?,NULL,'running','explicit')",
                (datetime.now(timezone.utc).isoformat(),),
            )
            db.executemany(
                "INSERT INTO ingestion_jobs(run_id,symbol,start_date,reasons,status) VALUES ('budget',?,?,?,'pending')",
                [
                    (symbol, day.isoformat(), "[]")
                    for symbol, day in zip(symbols, days)
                ],
            )

        service = IngestionService(self.path, provider, sleep=lambda _: None)
        service._execute("budget", 50, days[-1])

        jobs = service.run("budget")["jobs"]
        self.assertEqual(len(provider.calls), 3)
        self.assertEqual(sum(job["status"] == "complete" for job in jobs), 3)
        self.assertEqual(sum(job["status"] == "failed" for job in jobs), 2)

    @patch("quant.ingestion.MAX_PROVIDER_CALLS_PER_EXECUTION", 1)
    def test_provider_call_budget_resets_for_each_execution(self):
        day = date(2026, 8, 31)
        provider = FakeProvider(bars([day], ("FIRST", "SECOND")))
        with database_connection(self.path) as db:
            db.executemany(
                "INSERT INTO ingestion_runs VALUES (?,?,NULL,'running','explicit')",
                [
                    ("first-run", datetime.now(timezone.utc).isoformat()),
                    ("second-run", datetime.now(timezone.utc).isoformat()),
                ],
            )
            db.executemany(
                "INSERT INTO ingestion_jobs(run_id,symbol,start_date,reasons,status) VALUES (?,?,?,'[]','pending')",
                [
                    ("first-run", "FIRST", day.isoformat()),
                    ("second-run", "SECOND", day.isoformat()),
                ],
            )

        service = IngestionService(self.path, provider, sleep=lambda _: None)
        service._execute("first-run", 50, day)
        service._execute("second-run", 50, day)

        self.assertEqual(provider.calls, [(["FIRST"], day), (["SECOND"], day)])
        self.assertIsNone(service.provider_calls_remaining)

    def test_worker_recognizes_persisted_scheduled_attempt(self):
        session = date(2026, 8, 31)
        worker = IngestionWorker(self.path)
        with database_connection(self.path) as db:
            db.execute(
                "INSERT INTO ingestion_runs VALUES ('explicit','2026-09-01T00:01:00+00:00',NULL,'complete','explicit')"
            )
        self.assertFalse(worker._scheduled_run_exists(session))
        with database_connection(self.path) as db:
            db.execute(
                "INSERT INTO ingestion_runs VALUES ('scheduled','2026-09-01T00:01:00+00:00',NULL,'partial','stored')"
            )
        self.assertTrue(worker._scheduled_run_exists(session))

    def test_running_explicit_job_recovers_despite_scheduled_attempt(self):
        session = date(2026, 8, 31)
        provider = FakeProvider(bars([session], ("AAPL",)))
        with database_connection(self.path) as db:
            db.execute(
                "INSERT INTO ingestion_runs VALUES ('scheduled','2026-09-01T00:01:00+00:00','2026-09-01T00:02:00+00:00','complete','stored')"
            )
            db.execute(
                "INSERT INTO ingestion_runs VALUES ('explicit-running',?,NULL,'running','explicit')",
                (datetime.now(timezone.utc).isoformat(),),
            )
            db.execute(
                "INSERT INTO ingestion_jobs(run_id,symbol,start_date,reasons,status) VALUES ('explicit-running','AAPL','2026-08-31','[]','running')"
            )

        service = IngestionService(self.path, provider, sleep=lambda _: None)
        service.recover(batch_size=50, today=session)

        self.assertEqual(service.run("explicit-running")["status"], "complete")
        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(IngestionWorker(self.path)._scheduled_run_exists(session))

    def test_recovery_processes_every_running_run(self):
        session = date(2026, 8, 31)
        provider = FakeProvider(bars([session], ("FIRST", "SECOND")))
        with database_connection(self.path) as db:
            db.executemany(
                "INSERT INTO ingestion_runs VALUES (?,?,NULL,'running','explicit')",
                [
                    ("first-running", "2026-09-01T00:01:00+00:00"),
                    ("second-running", "2026-09-01T00:02:00+00:00"),
                ],
            )
            db.executemany(
                "INSERT INTO ingestion_jobs(run_id,symbol,start_date,reasons,status) VALUES (?,?,?,'[]','running')",
                [
                    ("first-running", "FIRST", session.isoformat()),
                    ("second-running", "SECOND", session.isoformat()),
                ],
            )

        service = IngestionService(self.path, provider, sleep=lambda _: None)
        service.recover(batch_size=50, today=session)

        self.assertEqual(service.run("first-running")["status"], "complete")
        self.assertEqual(service.run("second-running")["status"], "complete")
        self.assertEqual(provider.calls, [(["FIRST"], session), (["SECOND"], session)])

    def test_ingestion_quarantines_impossible_provider_ohlc(self):
        days = sessions(date(2026, 8, 3), date(2026, 8, 5))
        source = bars(days, ("HUBB",)).with_columns(
            pl.when(pl.col("Date") == days[1])
            .then(pl.col("Low") - 1)
            .otherwise(pl.col("Open"))
            .alias("Open")
        )

        run = IngestionService(
            self.path, FakeProvider(source), sleep=lambda _: None
        ).synchronize(symbols=["HUBB"], horizon=days[0], today=days[-1])

        self.assertEqual(run["status"], "complete")
        stored = MarketDataRepository(self.path).load(["HUBB"])
        quarantined = stored.filter(pl.col("Date") == days[1]).row(0, named=True)
        self.assertIsNone(quarantined["Open"])
        self.assertIsNone(quarantined["High"])
        self.assertIsNone(quarantined["Low"])
        self.assertIsNotNone(quarantined["Close"])
        with database_connection(self.path, read_only=True) as db:
            issues = db.execute(
                "SELECT code,session_date FROM data_issues WHERE resolved_at IS NULL AND symbol='HUBB'"
            ).fetchall()
        self.assertIn(("incomplete_bar", days[1].isoformat()), [tuple(row) for row in issues])

    def test_ingestion_reconciles_stale_issues_from_expected_first_session(self):
        requested_start = date(2026, 1, 1)
        days = sessions(requested_start, date(2026, 1, 6))
        MarketDataRepository(self.path).save(bars(days, ("AAPL",)))
        with database_connection(self.path) as db:
            db.execute("UPDATE securities SET calendar='XNYS' WHERE symbol='AAPL'")
            db.execute(
                "INSERT INTO sync_coverage VALUES ('AAPL',?,?)",
                (requested_start.isoformat(), datetime.now(timezone.utc).isoformat()),
            )
        service = IngestionService(self.path)
        service.issue("AAPL", "history_starts_late", days[0].isoformat(), "stale")
        service.issue("AAPL", "unknown_calendar", "", "stale")

        service._audit_gaps("AAPL", days[-1])

        with database_connection(self.path, read_only=True) as db:
            unresolved = db.execute(
                "SELECT code FROM data_issues WHERE resolved_at IS NULL AND symbol='AAPL'"
            ).fetchall()
        self.assertEqual(unresolved, [])

        with database_connection(self.path) as db:
            db.execute(
                "DELETE FROM daily_bars WHERE security_id=(SELECT id FROM securities WHERE symbol='AAPL') AND session_date=?",
                (days[0].isoformat(),),
            )
        service._audit_gaps("AAPL", days[-1])
        with database_connection(self.path, read_only=True) as db:
            unresolved = db.execute(
                "SELECT code,session_date FROM data_issues WHERE resolved_at IS NULL AND symbol='AAPL'"
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in unresolved],
            [("history_starts_late", days[1].isoformat())],
        )

    def test_failed_gap_audit_does_not_resolve_existing_issues(self):
        days = [date(2010, 1, 4), date(2030, 1, 30)]
        MarketDataRepository(self.path).save(bars(days, ("AAPL",)))
        with database_connection(self.path) as db:
            db.execute("UPDATE securities SET calendar='XNYS' WHERE symbol='AAPL'")
        service = IngestionService(self.path)
        service.issue("AAPL", "missing_session", "2010-01-05", "existing")

        with self.assertRaisesRegex(ValueError, "maximum 20 years"):
            service._audit_gaps("AAPL", days[-1])

        with database_connection(self.path, read_only=True) as db:
            issue = db.execute(
                "SELECT resolved_at FROM data_issues WHERE symbol='AAPL' AND code='missing_session'"
            ).fetchone()
        self.assertIsNone(issue["resolved_at"])

    def test_interrupted_planning_keeps_request_queued_until_full_plan_is_saved(self):
        from quant.platform_service import PlatformService
        platform = PlatformService(self.path)
        day = date(2026, 8, 31)
        request_record = platform.request_sync("interrupted-plan", ["AAPL", "SPY"], day)
        service = IngestionService(self.path, FakeProvider(bars([day])), sleep=lambda _: None)
        with patch.object(service, "plan", side_effect=[(day, ["backfill"]), KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                service.synchronize(symbols=["AAPL", "SPY"], horizon=day, today=day, run_id=request_record["id"])
        with database_connection(self.path, read_only=True) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ingestion_jobs").fetchone()[0], 0)
        self.assertEqual(platform.run(request_record["id"])["status"], "queued")
        recovered = service.synchronize(symbols=["AAPL", "SPY"], horizon=day, today=day, run_id=request_record["id"])
        self.assertEqual(recovered["status"], "complete")
        self.assertEqual({job["symbol"] for job in recovered["jobs"]}, {"AAPL", "SPY"})

    def test_ingestion_does_not_infer_session_gaps_for_unknown_calendar(self):
        days = self.seed()
        with database_connection(self.path) as db:
            db.execute("DELETE FROM daily_bars WHERE session_date=? AND security_id=(SELECT id FROM securities WHERE symbol='AAPL')", (days[10].isoformat(),))
            db.execute("UPDATE securities SET calendar=NULL WHERE symbol='AAPL'")
        service = IngestionService(self.path)
        self.assertNotIn("internal_gap", service.plan("AAPL", days[0], days[-1])[1])
        with database_connection(self.path) as db:
            db.execute("UPDATE securities SET calendar='XNYS' WHERE symbol='AAPL'")
        self.assertIn("internal_gap", service.plan("AAPL", days[0], days[-1])[1])

    def test_fact_source_fields_cannot_replace_retained_provenance(self):
        service = FundamentalService(self.path)
        evidence = service.save(EvidenceInput(
            importKey="fact-provenance", symbol="AAPL", category="facts", source="test",
            sourceUrl="https://example.com", availableAt="2026-08-01T12:00:00Z",
            data={"facts": [{"tag": "NetIncomeLoss", "unit": "USD", "val": 20,
                "start": "2026-04-01", "end": "2026-06-30", "metric": "debt",
                "evidenceId": "wrong", "availableAt": "2000-01-01", "retrievedAt": "wrong"}]}))
        fact = service.summary("AAPL")["observations"][0]
        self.assertEqual(fact["metric"], "netIncome")
        self.assertEqual(fact["evidenceId"], evidence["id"])
        self.assertEqual(fact["availableAt"], evidence["payload"]["availableAt"])
        self.assertEqual(fact["retrievedAt"], evidence["createdAt"])

    def test_document_contents_are_not_interpreted_as_validated_financial_facts(self):
        service = FundamentalService(self.path)
        service.save(EvidenceInput(importKey="document", symbol="AAPL", category="document",
            source="test", sourceUrl="https://example.com", availableAt="2026-08-01T12:00:00Z",
            data={"facts": ["unstructured document content"]}))
        self.assertEqual(service.summary("AAPL")["observations"], [])

    def test_revision_failure_preserves_previous_series(self):
        days=sessions(date(2026,8,3),date(2026,8,10))
        old=bars(days)
        market=MarketDataRepository(self.path)
        market.save(old)
        partial=old.filter(pl.col("Date")>=days[-2]).with_columns((pl.col("Adjusted Close")*0.9).alias("Adjusted Close"))
        service=IngestionService(self.path,FakeProvider(partial),sleep=lambda _:None)
        before=market.load(["AAPL"]).to_dicts()
        with self.assertRaises(ValueError):
            service._publish("AAPL",partial.filter(pl.col("Symbol")=="AAPL"),days[-1])
        self.assertEqual(before,market.load(["AAPL"]).to_dicts())

    def test_invalid_revision_cannot_remove_existing_ohlc(self):
        days = sessions(date(2026, 8, 3), date(2026, 8, 5))
        old = bars(days, ("AAPL",))
        market = MarketDataRepository(self.path)
        market.save(old)
        before = market.load(["AAPL"]).to_dicts()
        invalid = old.with_columns(
            pl.when(pl.col("Date") == days[1])
            .then(pl.col("Low") - 1)
            .otherwise(pl.col("Open"))
            .alias("Open")
        )

        with self.assertRaisesRegex(ValueError, "remove existing prices"):
            IngestionService(
                self.path, FakeProvider(invalid), sleep=lambda _: None
            )._publish("AAPL", invalid, days[-1])

        self.assertEqual(market.load(["AAPL"]).to_dicts(), before)

    def test_revision_full_history_has_no_splice(self):
        days=sessions(date(2026,8,3),date(2026,8,10))
        old=bars(days,("AAPL",))
        MarketDataRepository(self.path).save(old)
        new=old.with_columns((pl.col("Adjusted Close")*0.9).alias("Adjusted Close"))
        service=IngestionService(self.path,FakeProvider(new),sleep=lambda _:None)
        service._publish("AAPL",new.tail(2),days[-1])
        self.assertEqual(MarketDataRepository(self.path).load(["AAPL"])["Adjusted Close"].to_list(),new["Adjusted Close"].to_list())

    def test_revised_full_history_quarantines_impossible_new_ohlc(self):
        days = sessions(date(2026, 8, 3), date(2026, 8, 10))
        source = bars(days, ("AAPL",))
        MarketDataRepository(self.path).save(source.tail(2))
        revised = source.with_columns(
            (pl.col("Adjusted Close") * 0.9).alias("Adjusted Close"),
            pl.when(pl.col("Date") == days[0])
            .then(pl.col("Low") - 1)
            .otherwise(pl.col("Open"))
            .alias("Open"),
        )

        IngestionService(
            self.path, FakeProvider(revised), sleep=lambda _: None
        )._publish("AAPL", revised, days[-1])

        stored = MarketDataRepository(self.path).load(["AAPL"])
        quarantined = stored.filter(pl.col("Date") == days[0]).row(0, named=True)
        self.assertIsNone(quarantined["Open"])
        self.assertIsNone(quarantined["High"])
        self.assertIsNone(quarantined["Low"])
        self.assertEqual(
            stored["Adjusted Close"].to_list(), revised["Adjusted Close"].to_list()
        )

    def test_universe_failure_does_not_block_personal_symbols(self):
        from quant.user_data import UserDataRepository
        UserDataRepository(self.path).add_position("AAPL", 1, 100)
        provider=FakeProvider(bars([date(2026,8,31)]))
        provider.constituents=lambda: (_ for _ in ()).throw(RuntimeError("offline"))
        result=IngestionService(self.path,provider).synchronize(horizon=date(2026,8,31),today=date(2026,8,31))
        self.assertEqual(result["status"],"complete")
        self.assertEqual({r["symbol"] for r in result["jobs"]},{"AAPL","SPY"})

    def test_evidence_availability_and_amendments_remain_separate(self):
        service=FundamentalService(self.path)
        for key,day,value in [("first","2026-01-01T12:00:00Z",10),("amended","2026-02-01T12:00:00Z",20)]:
            service.save(EvidenceInput(importKey=key,symbol="AAPL",category="facts",source="fixture",sourceUrl="https://www.sec.gov/fixture",availableAt=day,
                data={"facts":[{"tag":"NetIncomeLoss","unit":"USD","val":value,"end":"2025-12-31"}]}))
        summary=service.summary("AAPL","2026-01-15T00:00:00Z")
        self.assertEqual([r["val"] for r in summary["observations"]],[10])
        self.assertEqual(len(service.evidence("AAPL")),2)

    def test_money_weighted_conventional_return_and_ambiguous_flows(self):
        self.assertAlmostEqual(money_weighted_return([(date(2025,1,1),-100),(date(2026,1,1),110)]),0.1)
        self.assertIsNone(money_weighted_return([(date(2025,1,1),-100),(date(2025,6,1),200),(date(2026,1,1),-110)]))

    def test_ledger_cash_flows_reconcile_and_are_not_returns(self):
        opening=snapshot(day="2026-08-03",quantity=1,cash=100,value=100).model_dump(mode="json")
        closing=snapshot(key="end",day="2026-08-04",quantity=1,cash=150,value=110).model_dump(mode="json")
        event={"sourceId":"deposit1","date":"2026-08-04","kind":"deposit","symbol":None,"quantity":None,"amount":50,"ratio":None}
        prices=pl.DataFrame({"Date":[date(2026,8,4)],"Symbol":["AAPL"],"Close":[110.0]})
        result=ledger_performance(opening,closing,[event],prices,True)
        self.assertTrue(result["reconciled"])
        self.assertAlmostEqual(result["timeWeightedReturn"],0.05)
        self.assertIsNone(ledger_performance(opening,closing,[event],prices,False)["timeWeightedReturn"])
        closing["cash"]=200
        self.assertFalse(ledger_performance(opening,closing,[event],prices,True)["performanceAvailable"])

    def test_universe_asof_does_not_invent_earlier_membership(self):
        MarketDataRepository(self.path).save_universe("sp500",pl.DataFrame({"Symbol":["AAPL"]}),date(2026,8,3))
        service=DiscoveryService(self.path)
        with self.assertRaises(ValueError):
            service.members("sp500","2026-08-02")
        self.assertEqual(service.members("sp500","2026-08-04")["observedOn"],"2026-08-03")

    def test_experiment_does_not_trade_on_same_close_as_signal(self):
        days=sessions(date(2026,6,1),date(2026,8,31))
        result=momentum_experiment(bars(days,("AAPL",)),days[30],2,0)
        self.assertEqual(result["history"][1]["held"],0)
        self.assertEqual(result["history"][2]["held"],0)
        self.assertEqual(result["history"][3]["held"],1)

    def test_pagination_preserves_fields_and_rejects_changed_dataset(self):
        from quant.platform_service import PlatformService
        days=self.seed()
        service=PlatformService(self.path)
        first=service.bars(["AAPL"],days[0],days[-1],["adjustedClose"],3,None,None)
        self.assertEqual(len(first["bars"]),3)
        self.assertEqual(set(first["bars"][0]),{"date","symbol","adjustedClose"})
        second=service.bars(["AAPL"],days[0],days[-1],["adjustedClose"],3,first["nextCursor"],None)
        self.assertLess(first["bars"][-1]["date"],second["bars"][0]["date"])
        MarketDataRepository(self.path).save(bars([date(2026,9,1)]))
        with self.assertRaises(ValueError):
            service.bars(["AAPL"],days[0],days[-1],["adjustedClose"],3,first["nextCursor"],None)

    def test_resume_interrupted_job(self):
        from quant.record_store import utc_now
        with database_connection(self.path) as db:
            db.execute("INSERT INTO ingestion_runs VALUES ('interrupted',?,NULL,'running','explicit')",(utc_now(),))
            db.execute("INSERT INTO ingestion_jobs(run_id,symbol,start_date,reasons,status) VALUES ('interrupted','AAPL','2026-08-31','[]','running')")
        service=IngestionService(self.path,FakeProvider(bars([date(2026,8,31)])),sleep=lambda _:None)
        service.synchronize(symbols=["AAPL"],horizon=date(2026,8,31),today=date(2026,8,31))
        self.assertEqual(service.run("interrupted")["status"],"complete")
        self.assertEqual(service.run("interrupted")["jobs"][0]["attempts"],1)

    def test_metadata_reimport_does_not_revert_newer_metadata(self):
        from quant.contracts import SecurityMetadataInput
        from quant.platform_service import PlatformService
        service=PlatformService(self.path)
        first=SecurityMetadataInput(importKey="first",symbol="TEST",currency="USD",calendar="XNYS",instrumentType="equity",source="fixture")
        newer=first.model_copy(update={"importKey":"newer","instrumentType":"etf"})
        service.security_metadata(first)
        service.security_metadata(newer)
        service.security_metadata(first)
        with database_connection(self.path,read_only=True) as db:
            self.assertEqual(db.execute("SELECT instrument_type FROM securities WHERE symbol='TEST'").fetchone()[0],"etf")

    def test_ledger_splits_dividends_sales_and_fees(self):
        opening=snapshot(day="2026-08-03",quantity=2,cash=100,value=200).model_dump(mode="json")
        closing=snapshot(key="end",day="2026-08-04",quantity=3,cash=156,value=165).model_dump(mode="json")
        from quant.contracts import LedgerEvent
        events=[LedgerEvent(sourceId="s",date="2026-08-04",kind="split",symbol="AAPL",ratio=2),
                LedgerEvent(sourceId="sale",date="2026-08-04",kind="sell",symbol="AAPL",quantity=1,amount=55),
                LedgerEvent(sourceId="div",date="2026-08-04",kind="dividend",symbol="AAPL",amount=2),
                LedgerEvent(sourceId="fee",date="2026-08-04",kind="fee",amount=1)]
        result=ledger_performance(opening,closing,[e.model_dump(mode="json") for e in events],pl.DataFrame({"Date":[date(2026,8,4)],"Symbol":["AAPL"],"Close":[55.0]}),True)
        self.assertTrue(result["performanceAvailable"])
        self.assertAlmostEqual(result["timeWeightedReturn"],0.07)

    def test_valuation_uses_explicit_fcff_and_rejects_invalid_terminal_rate(self):
        from quant.contracts import ValuationInput
        from quant.fundamentals import valuation_sensitivity
        body=ValuationInput(freeCashFlowToFirm=100,shares=10,cash=20,debt=10,years=5,cases=[{"name":"flat","growth":0,"discountRate":0.1,"terminalGrowth":0}])
        self.assertAlmostEqual(valuation_sensitivity(body)["cases"][0]["valuePerShare"],101)
        with self.assertRaises(ValueError):
            ValuationInput(freeCashFlowToFirm=100,shares=10,cash=20,debt=10,cases=[{"name":"bad","growth":0,"discountRate":0.02,"terminalGrowth":0.03}])

    def test_cache_metadata_exposes_stale_fallback(self):
        from quant.research import BoundedTTLCache
        now=[0.0]
        cache=BoundedTTLCache(clock=lambda:now[0])
        first=cache.get_or_load("key",10,lambda:{"value":1},with_metadata=True)
        self.assertEqual(first["cache"]["source"],"live")
        now[0]=11
        def fail():
            raise RuntimeError("private provider message")
        stale=cache.get_or_load("key",10,fail,with_metadata=True)
        self.assertTrue(stale["cache"]["staleFallback"])
        self.assertEqual(stale["cache"]["cacheAgeSeconds"],11)
        self.assertEqual(stale["cache"]["retrievedAt"],first["cache"]["retrievedAt"])
        self.assertNotIn("private",json.dumps(stale))

    def test_factors_recover_known_exposure(self):
        days=sessions(date(2026,5,1),date(2026,8,31))
        asset,benchmark=100.,100.
        rows=[]
        for i,day in enumerate(days):
            if i:
                factor=((i%7)-3)*0.002
                benchmark *= 1+factor
                asset *= 1+0.0001+1.5*factor
            rows.extend([{"Date":day,"Symbol":"AAPL","Close":asset,"Adjusted Close":asset},
                         {"Date":day,"Symbol":"SPY","Close":benchmark,"Adjusted Close":benchmark}])
        MarketDataRepository(self.path).save(pl.DataFrame(rows))
        result=DiscoveryService(self.path).factors("AAPL",["SPY"],days[0],days[-1])
        self.assertAlmostEqual(result["proxies"]["SPY"],1.5)
        self.assertAlmostEqual(result["annualizedIntercept"],0.0252)

    def test_sec_ingestion_retains_filing_identity_and_source_facts(self):
        recent={"accessionNumber":["0001-26-000001"],"form":["10-Q"],"acceptanceDateTime":["2026-08-01T12:00:00Z"],
                "filingDate":["2026-08-01"],"reportDate":["2026-06-30"],"primaryDocument":["filing.htm"]}
        facts={"facts":{"us-gaap":{"NetIncomeLoss":{"units":{"USD":[{"accn":"0001-26-000001","val":20,"start":"2026-04-01","end":"2026-06-30"}]}}}}}
        provider=SECProvider(transport=lambda path: {"tickers":["AAPL"],"filings":{"recent":recent}} if path.startswith("submissions/") else facts)
        service=FundamentalService(self.path,provider)
        result=service.ingest_sec("AAPL","1234")
        self.assertEqual(len(result["records"]),1)
        evidence=result["records"][0]
        self.assertEqual(evidence["payload"]["accession"],"0001-26-000001")
        self.assertEqual(service.ingest_sec("AAPL","1234")["records"][0]["id"],evidence["id"])
        self.assertEqual(service.summary("AAPL")["observations"][0]["val"],20)

    def test_unknown_cost_does_not_create_zero_cost_gain(self):
        self.seed()
        body=snapshot()
        body.positions[0].averageCost=None
        service=ResearchWorkflow(self.path)
        snap=service.import_snapshot(body)
        result=service.review(snap["id"],"1mo")
        self.assertNotIn("averageCost",result["positions"][0])
        self.assertNotIn("gainLoss",result["allocations"]["sector"][0])


def request(app,path,method="GET",body=None,client="127.0.0.1",query=""):
    sent=[]
    delivered=False
    async def receive():
        nonlocal delivered
        if not delivered:
            delivered=True
            return {"type":"http.request","body":json.dumps(body).encode() if body is not None else b"","more_body":False}
        return {"type":"http.disconnect"}
    async def send(message):
        sent.append(message)
    scope={"type":"http","asgi":{"version":"3.0"},"http_version":"1.1","method":method,"scheme":"http","path":path,
           "raw_path":path.encode(),"query_string":query.encode(),"root_path":"","headers":[(b"host",b"127.0.0.1:8001"),(b"content-type",b"application/json")],
           "client":(client,1234),"server":("127.0.0.1",8001)}
    asyncio.run(app(scope,receive,send))
    return next(m["status"] for m in sent if m["type"]=="http.response.start"),json.loads(b"".join(m.get("body",b"") for m in sent if m["type"]=="http.response.body"))


class EngineAPITests(unittest.TestCase):
    setUp = EngineTests.setUp
    seed = EngineTests.seed

    def test_malformed_evidence_returns_validation_error_without_writing(self):
        from quant.dashboard import api_alpha, server
        from quant.dashboard.services import DashboardService
        service = DashboardService(market_repository=MarketDataRepository(self.path, read_only=True))
        common = {"importKey": "bad-evidence", "symbol": "AAPL", "source": "test",
                  "sourceUrl": "https://example.com", "availableAt": "2026-08-01T12:00:00Z"}
        cases = [
            ("facts", {"facts": [None]}),
            ("facts", {"facts": [{"tag": "Revenues", "unit": "USD", "val": 1, "end": 20260801}]}),
            ("facts", {"facts": [{"tag": "Revenues", "unit": "USD", "val": 1, "start": "20260801"}]}),
            ("facts", {"facts": [{"tag": " ", "unit": "USD", "val": 1}]}),
            ("facts", {"facts": [{"tag": "Revenues", "unit": "USD", "val": 10**500}]}),
            ("fund_holdings", {"holdings": [None]}),
            ("fund_holdings", {"holdings": [{"symbol": "bad symbol", "weight": 0.5}]}),
            ("fund_holdings", {"holdings": [{"symbol": "AAPL", "weight": True}]}),
        ]
        before = self.path.read_bytes()
        with patch.object(api_alpha, "_service", service):
            for category, data in cases:
                with self.subTest(category=category, data=data):
                    status, response = request(server.app, "/api/alpha/research/evidence", "POST",
                                               {**common, "category": category, "data": data})
                    self.assertEqual(status, 422, response)
                    self.assertEqual(response["detail"]["code"], "invalid_request")
        self.assertEqual(self.path.read_bytes(), before)
    def test_snapshot_to_run_to_replay_through_api(self):
        from quant.dashboard import api_alpha,server
        from quant.dashboard.services import DashboardService
        self.seed()
        service=DashboardService(market_repository=MarketDataRepository(self.path,read_only=True))
        with patch.object(api_alpha,"_service",service):
            status,body=request(server.app,"/api/alpha/portfolio/snapshots","POST",snapshot().model_dump(mode="json"))
            self.assertEqual(status,200,body)
            identifier=body["data"]["id"]
            status,body=request(server.app,"/api/alpha/research/runs","POST",{"importKey":"run","snapshotId":identifier,"period":"1mo"})
            self.assertEqual(status,200,body)
            identifier=body["data"]["id"]
            status,body=request(server.app,f"/api/alpha/research/runs/{identifier}/replay")
            self.assertEqual(status,200,body)
            self.assertTrue(body["data"]["matches"])

    def test_remote_writes_and_unknown_parameters_rejected(self):
        from quant.dashboard.server import app
        status,_=request(app,"/api/alpha/portfolio/snapshots","POST",snapshot().model_dump(mode="json"),client="10.0.0.1")
        self.assertEqual(status,403)
        status,_=request(app,"/api/alpha/capabilities",query="misspelled=true")
        self.assertEqual(status,422)

    def test_new_and_legacy_reads_do_not_write_or_contact_providers(self):
        from quant.dashboard import api_alpha,server
        from quant.dashboard.services import DashboardService
        from quant.user_data import UserDataRepository
        self.seed()
        service=DashboardService(UserDataRepository(self.path,read_only=True),MarketDataRepository(self.path,read_only=True))
        cases=[("/api/quotes","symbols=AAPL&refresh=true"), ("/api/holdings",""),
               ("/api/alpha/capabilities",""),("/api/alpha/data/quality",""),("/api/alpha/data/gaps",""),
               ("/api/alpha/portfolio/snapshots",""),("/api/alpha/research/fundamentals/AAPL",""),
               ("/api/alpha/universes",""),("/api/alpha/research/records/report","")]
        before=self.path.read_bytes()
        with patch.object(api_alpha,"_service",service),patch.object(server,"_dashboard_service",service),patch("quant.quotes.get_market_history",side_effect=AssertionError("provider")),patch("quant.fundamentals.SECProvider.get",side_effect=AssertionError("provider")):
            for path,query in cases:
                with self.subTest(path=path):
                    status,body=request(server.app,path,query=query)
                    self.assertEqual(status,200,body)
        self.assertEqual(self.path.read_bytes(),before)

    def test_api_rejects_conflicting_or_invalid_snapshot_input(self):
        from quant.dashboard import api_alpha,server
        from quant.dashboard.services import DashboardService
        service=DashboardService(market_repository=MarketDataRepository(self.path,read_only=True))
        body=snapshot().model_dump(mode="json")
        body["positions"].append(body["positions"][0])
        with patch.object(api_alpha,"_service",service):
            status,result=request(server.app,"/api/alpha/portfolio/snapshots","POST",body)
        self.assertEqual(status,422,result)
        self.assertEqual(result["detail"]["code"],"invalid_request")
