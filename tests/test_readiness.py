from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from io import StringIO
import unittest
from unittest.mock import patch

from quant.contracts import RunInput
from quant.database import database_connection
from quant.market_store import MarketDataRepository
from quant.platform_service import PlatformService
from quant.readiness import portfolio_readiness
from quant.workflows import ResearchWorkflow
from tests import test_research_engine as fixtures
from tests.test_research_engine import bars, request, snapshot


class ReadinessTests(unittest.TestCase):
    setUp = fixtures.EngineTests.setUp
    seed = fixtures.EngineTests.seed

    def check(self, body=None, **kwargs):
        return portfolio_readiness(self.path, (body or snapshot()).model_dump(mode="json"),
                                   "snapshot", "1mo", **kwargs)

    def test_complete_history_is_ready_and_benchmark_holding_is_not_duplicated(self):
        self.seed()
        result = self.check(benchmark="AAPL")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["requiredPricePoints"], 22)
        self.assertEqual(result["historyStart"], "2026-07-31")
        self.assertEqual(len(result["securities"]), 1)
        self.assertEqual(result["securities"][0]["roles"], ["holding", "benchmark"])

    def test_missing_cash_does_not_block_securities_risk(self):
        self.seed()
        result = self.check(snapshot(cash=None))
        self.assertEqual(result["status"], "limited")
        self.assertEqual(result["checks"]["accountValue"]["status"], "blocked")
        self.assertEqual(result["checks"]["historicalRisk"]["status"], "ready")

    def test_gap_and_null_adjustment_are_not_masked_by_extra_old_rows(self):
        self.seed()
        with database_connection(self.path) as db:
            db.execute("DELETE FROM daily_bars WHERE session_date='2026-08-10'")
            db.execute("UPDATE daily_bars SET adjusted_close=NULL WHERE session_date='2026-08-11'")
        result = self.check()
        self.assertEqual(result["checks"]["valuation"]["status"], "ready")
        self.assertEqual(result["checks"]["historicalRisk"]["status"], "blocked")
        self.assertEqual(result["securities"][0]["missingSessionSample"], ["2026-08-10", "2026-08-11"])
        self.assertEqual(result["securities"][0]["missingSessionCount"], 2)

    def test_missing_prefix_and_price_details_are_bounded(self):
        MarketDataRepository(self.path).save(bars([date(2026, 8, 31)]))
        with database_connection(self.path) as db:
            db.execute("UPDATE securities SET currency='USD',calendar='XNYS'")
        result = self.check()
        self.assertEqual(result["securities"][0]["missingSessionCount"], 21)
        self.assertEqual(len(result["securities"][0]["missingSessionSample"]), 20)

    def test_unknown_calendar_is_not_guessed(self):
        self.seed()
        with database_connection(self.path) as db:
            db.execute("UPDATE securities SET calendar=NULL WHERE symbol='AAPL'")
        result = self.check()
        self.assertIsNone(result["securities"][0]["missingSessionCount"])
        self.assertIsNone(result["securities"][0]["usablePricePoints"])
        self.assertEqual(result["checks"]["historicalRisk"]["status"], "blocked")
        self.assertEqual(result["checks"]["valuation"]["status"], "limited")

    def test_cash_only_snapshot_needs_no_benchmark(self):
        body = snapshot()
        body.positions = []
        result = self.check(body)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["checks"]["historicalRisk"]["status"], "not_applicable")
        self.assertEqual(result["securities"], [])

    def test_other_provider_and_future_prices_do_not_fill_yahoo_history(self):
        days = self.seed()
        with database_connection(self.path) as db:
            db.execute("DELETE FROM daily_bars WHERE session_date='2026-08-31'")
        MarketDataRepository(self.path).save(bars([days[-1]]), provider="other")
        MarketDataRepository(self.path).save(bars([date(2026, 9, 1)]))
        result = self.check()
        self.assertEqual(result["securities"][0]["latestPriceDate"], "2026-08-28")
        self.assertEqual(result["checks"]["valuation"]["status"], "limited")
        self.assertEqual(result["checks"]["historicalRisk"]["status"], "blocked")

    def test_holidays_and_uncompleted_session_use_previous_session(self):
        self.seed()
        for day, now, expected in [
            ("2026-08-31", datetime(2026, 8, 31, 18, tzinfo=timezone.utc), "2026-08-28"),
            ("2026-07-03", datetime(2026, 8, 31, 18, tzinfo=timezone.utc), "2026-07-02"),
        ]:
            with self.subTest(day=day):
                result = self.check(snapshot(day=day), now=now)
                self.assertEqual(result["expectedSession"], expected)
                self.assertEqual(result["status"], "ready")

    def test_currency_mismatch_and_unpriced_holdings_block_complete_valuation(self):
        self.assertEqual(self.check()["checks"]["valuation"]["status"], "blocked")
        self.seed()
        with database_connection(self.path) as db:
            db.execute("UPDATE securities SET currency='EUR' WHERE symbol='AAPL'")
        self.assertEqual(self.check()["status"], "blocked")

    def test_frozen_readiness_survives_live_data_change(self):
        self.seed()
        workflow = ResearchWorkflow(self.path)
        saved = workflow.import_snapshot(snapshot())
        run = workflow.create_run(RunInput(importKey="run", snapshotId=saved["id"], period="1mo"))
        service = PlatformService(self.path)
        before = service.run_readiness(run["id"])
        with database_connection(self.path) as db:
            db.execute("DELETE FROM daily_bars WHERE session_date='2026-08-10'")
        self.assertEqual(before, service.run_readiness(run["id"]))
        self.assertEqual(before["datasetSha256"], run["payload"]["datasetSha256"])
        self.assertEqual(service.readiness(saved["id"], "1mo")["checks"]["historicalRisk"]["status"], "blocked")

    def test_api_read_only_contract_and_strict_parameters(self):
        from quant.dashboard import api_alpha, server
        from quant.dashboard.services import DashboardService
        self.seed()
        saved = ResearchWorkflow(self.path).import_snapshot(snapshot())
        run = ResearchWorkflow(self.path).create_run(RunInput(importKey="api-run", snapshotId=saved["id"], period="1mo"))
        path = f"/api/alpha/portfolio/snapshots/{saved['id']}/readiness"
        service = DashboardService(market_repository=MarketDataRepository(self.path, read_only=True))
        before = self.path.read_bytes()
        with patch.object(api_alpha, "_service", service), \
             patch("quant.ingestion.YahooProvider.history", side_effect=AssertionError("provider")), \
             patch("quant.market_store.initialize_database", side_effect=AssertionError("write")):
            status, body = request(server.app, path, query="period=1mo")
            self.assertEqual(status, 200, body)
            self.assertEqual(body["data"]["status"], "ready")
            self.assertEqual(body["meta"]["methodologyVersion"], "portfolio-readiness-v1")
            status, frozen = request(server.app, f"/api/alpha/research/runs/{run['id']}/readiness")
            self.assertEqual(status, 200, frozen)
            self.assertEqual(frozen["meta"]["datasetSha256"], run["payload"]["datasetSha256"])
            for query in ["period=unknown", "period=1mo&period=1y", "typo=true"]:
                self.assertEqual(request(server.app, path, query=query)[0], 422)
        self.assertEqual(self.path.read_bytes(), before)
        schema = api_alpha.app.openapi()["components"]["schemas"]
        self.assertIn("ReadinessData", schema)

    @patch("quant.query_cli.ApiClient", autospec=True)
    def test_cli_forwards_readiness_request(self, client_type):
        from quant.query_cli import main
        client_type.return_value.request.return_value = {"data": {"status": "ready"}}
        with redirect_stdout(StringIO()):
            main(["readiness", "snapshot-id", "--period", "1mo", "--benchmark", "qqq"])
        client_type.return_value.request.assert_called_once_with(
            "GET", "portfolio/snapshots/snapshot-id/readiness", query={"period": "1mo", "benchmark": "QQQ"})
