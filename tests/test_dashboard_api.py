import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from pydantic import ValidationError

from quant.dashboard import server


class DashboardApiTests(unittest.TestCase):
    @patch("quant.dashboard.server.initialize_database")
    @patch("quant.dashboard.server.IngestionWorker")
    @patch("uvicorn.run")
    @patch("quant.dashboard.server.configure")
    def test_server_serves_while_worker_synchronizes(self, configure, serve, worker, initialize):
        server.run(database=Path("market.db"), horizon=date(2025,1,1), batch_size=25)
        worker.assert_called_once_with(Path("market.db"), horizon=date(2025,1,1), batch_size=25)
        worker.return_value.start.assert_called_once()
        serve.assert_called_once_with(server.app, host="127.0.0.1", port=8001, reload=False)
        worker.return_value.stop.assert_called_once()

    @patch("quant.dashboard.server.initialize_database")
    @patch("quant.dashboard.server.IngestionWorker")
    @patch("uvicorn.run")
    @patch("quant.dashboard.server.configure")
    def test_no_sync_skips_provider_work(self, configure, serve, worker, initialize):
        server.run(database=Path("test.db"), sync_enabled=False)
        worker.assert_not_called()
        serve.assert_called_once()

    def test_analysis_and_research_routes_are_registered(self) -> None:
        paths = {route.path for route in server.app.routes}

        self.assertIn("/api/risk", paths)
        self.assertIn("/api/portfolio/risk", paths)
        self.assertIn("/api/screener", paths)
        self.assertIn("/api/search", paths)
        self.assertIn("/api/research/{symbol}/profile", paths)
        self.assertIn("/api/research/{symbol}/analyst", paths)
        self.assertIn("/api/research/{symbol}/options", paths)
        self.assertIn("/api/research/{symbol}/news", paths)

    def test_rejects_non_finite_holding_input(self) -> None:
        with self.assertRaises(ValidationError):
            server.HoldingIn(symbol="AAPL", shares=float("nan"), costBasis=100)

    def test_health(self) -> None:
        self.assertEqual(server.health(), {"ok": True})

    @patch("quant.dashboard.server._dashboard_service")
    def test_holdings_route_delegates_to_service(self, service) -> None:
        service.holdings.return_value = {"holdings": [], "totals": {}}

        result = server.holdings_list(refresh=True)

        self.assertEqual(result, {"holdings": [], "totals": {}})
        service.holdings.assert_called_once_with(True)

    @patch("quant.dashboard.server._dashboard_service")
    def test_analysis_route_requires_and_parses_explicit_windows(self, service) -> None:
        service.market_analysis.return_value = {"symbol": "AAPL", "rows": []}

        result = server.technical_analysis("AAPL", "20,50,20", "adjusted")

        self.assertEqual(result["symbol"], "AAPL")
        service.market_analysis.assert_called_once_with(
            "AAPL", [20, 50], "adjusted", False
        )

    @patch("quant.dashboard.server._dashboard_service")
    def test_quote_routes_delegate_to_core_service(self, service) -> None:
        service.quote.return_value = {"symbol": "AAPL", "price": 125.0}

        quote = server.quote("aapl")

        self.assertEqual(quote["price"], 125.0)
        service.quote.assert_called_once_with("aapl", False)

    @patch("quant.dashboard.server._dashboard_service")
    def test_risk_and_screener_routes_parse_symbols(self, service) -> None:
        service.symbol_risk.return_value = {"metrics": []}
        service.screener.return_value = {"rows": []}

        server.symbol_risk("aapl, msft,", "6mo", "spy", True)
        server.screener("aapl,msft", "1y", "SPY", False)

        service.symbol_risk.assert_called_once_with(
            ["aapl", "msft"], "6mo", "spy", True
        )
        service.screener.assert_called_once_with(
            ["aapl", "msft"], "1y", "SPY", False
        )

    @patch("quant.dashboard.server._dashboard_service")
    def test_research_routes_delegate_with_request_options(self, service) -> None:
        service.research_options.return_value = {"calls": [], "puts": []}
        service.research_intraday.return_value = []

        server.research_options("AAPL", "2026-09-18", 50)
        server.research_intraday("AAPL", "5d", "15m", 100)

        service.research_options.assert_called_once_with(
            "AAPL", "2026-09-18", 50
        )
        service.research_intraday.assert_called_once_with(
            "AAPL", "5d", "15m", 100
        )

    @patch("quant.dashboard.server._dashboard_service")
    def test_maps_service_validation_and_provider_errors(self, service) -> None:
        service.security_search.side_effect = ValueError("invalid search")
        with self.assertRaisesRegex(server.HTTPException, "invalid search") as error:
            server.security_search("", 10, False)
        self.assertEqual(error.exception.status_code, 422)

        service.research_profile.side_effect = RuntimeError("provider down")
        with self.assertRaisesRegex(server.HTTPException, "provider down") as error:
            server.research_profile("AAPL")
        self.assertEqual(error.exception.status_code, 502)


if __name__ == "__main__":
    unittest.main()
