import unittest
from unittest.mock import patch

from pydantic import ValidationError

from quant.dashboard import server


class DashboardApiTests(unittest.TestCase):
    def test_deferred_research_routes_are_not_registered(self) -> None:
        paths = {route.path for route in server.app.routes}

        self.assertNotIn("/api/info/{symbol}", paths)
        self.assertNotIn("/api/analyst/{symbol}", paths)
        self.assertNotIn("/api/options/{symbol}", paths)
        self.assertNotIn("/api/news/{symbol}", paths)

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


if __name__ == "__main__":
    unittest.main()
