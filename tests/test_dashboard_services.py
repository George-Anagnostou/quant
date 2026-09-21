import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import polars as pl

from quant.analysis import analyze_portfolio
from quant.dashboard.services import MAX_QUOTE_SYMBOLS, DashboardService
from quant.market_store import MarketDataRepository
from quant.user_data import UserDataRepository


class DashboardServiceTests(unittest.TestCase):
    @patch("quant.quotes.get_market_history", side_effect=RuntimeError("not found"))
    def test_keeps_unpriced_holdings_visible(self, get_market_history) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "quant.db"
            market_repository = MarketDataRepository(path)
            market_repository.save(
                pl.DataFrame(
                    {
                        "Date": [date(2026, 8, 20), date(2026, 8, 21)],
                        "Symbol": ["AAPL", "AAPL"],
                        "Close": [100.0, 125.0],
                        "Volume": [1_000, 2_000],
                    }
                )
            )
            repository = UserDataRepository(path)
            repository.add_position("AAPL", 1, 100)
            repository.add_position("MISSING", 1, 50)

            result = DashboardService(repository, market_repository).holdings()

        self.assertEqual(len(result["holdings"]), 2)
        self.assertEqual(result["unpricedSymbols"], ["MISSING"])
        missing = next(
            holding
            for holding in result["holdings"]
            if holding["symbol"] == "MISSING"
        )
        self.assertFalse(missing["marketDataAvailable"])
        self.assertIsNone(missing["marketValue"])
        self.assertEqual(result["totals"]["cost"], 150.0)
        self.assertEqual(result["totals"]["pricedCost"], 100.0)

    def test_limits_batched_quote_requests(self) -> None:
        service = DashboardService()

        with self.assertRaisesRegex(ValueError, "At most"):
            service.quotes(
                [f"SYM{index}" for index in range(MAX_QUOTE_SYMBOLS + 1)]
            )

    def test_rejects_blank_single_quote(self) -> None:
        with self.assertRaisesRegex(ValueError, "Symbol is required"):
            DashboardService().quote("   ")

    def test_rejects_unknown_analysis_price_basis(self) -> None:
        with self.assertRaisesRegex(ValueError, "Price must be"):
            DashboardService().market_analysis("AAPL", [20], "unknown")

    def test_serves_eod_quotes_from_shared_market_history(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "quant.db"
            market_repository = MarketDataRepository(path)
            market_repository.save(
                pl.DataFrame(
                    {
                        "Date": [date(2026, 8, 20), date(2026, 8, 21)],
                        "Symbol": ["AAPL", "AAPL"],
                        "Company": ["Apple Inc.", "Apple Inc."],
                        "Close": [100.0, 125.0],
                        "Volume": [1_000, 2_000],
                    }
                )
            )
            service = DashboardService(
                UserDataRepository(path),
                market_repository,
            )

            quote = service.quote("aapl")

        self.assertEqual(quote["name"], "Apple Inc.")
        self.assertEqual(quote["change"], 25.0)
        self.assertEqual(quote["changePercent"], 25.0)
        self.assertEqual(quote["asOf"], "2026-08-21")

    def test_data_status_uses_the_stalest_requested_symbol(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "quant.db"
            market_repository = MarketDataRepository(path)
            market_repository.save(
                pl.DataFrame(
                    {
                        "Date": [date(2026, 8, 1), date(2026, 8, 21)],
                        "Symbol": ["STALE", "CURRENT"],
                        "Close": [10.0, 20.0],
                    }
                )
            )
            service = DashboardService(
                UserDataRepository(path), market_repository
            )

            result = service.data_status(["CURRENT", "STALE"])

        self.assertEqual(result["summary"]["lastSession"], "2026-08-01")
        self.assertEqual(
            [row["symbol"] for row in result["coverage"]],
            ["CURRENT", "STALE"],
        )

    @patch("quant.dashboard.services.analyze_positions")
    def test_values_persisted_lots_with_core_analysis(self, analyze_positions) -> None:
        market_data = pl.DataFrame(
            {
                "Date": [date(2026, 8, 21)],
                "Symbol": ["AAPL"],
                "Last Price": [125.0],
                "Volume": [1_000],
            }
        )
        analyze_positions.side_effect = lambda positions, *_: analyze_portfolio(
            positions, market_data
        )
        with TemporaryDirectory() as directory:
            repository = UserDataRepository(Path(directory) / "quant.db")
            repository.initialize()
            repository.add_position(
                "AAPL",
                2,
                100.0,
                account="roth",
                asset_class="equity",
                sector="technology",
            )
            service = DashboardService(repository)

            result = service.holdings()

        holding = result["holdings"][0]
        self.assertEqual(holding["marketValue"], 250.0)
        self.assertEqual(holding["gain"], 50.0)
        self.assertEqual(holding["weightPercent"], 100.0)
        self.assertEqual(holding["account"], "roth")
        self.assertEqual(result["totals"]["gainPercent"], 25.0)

    @patch("quant.dashboard.services.analyze_symbols")
    def test_serializes_core_market_indicators(self, analyze_symbols) -> None:
        analyze_symbols.return_value = pl.DataFrame(
            {
                "Date": [date(2026, 8, 21)],
                "Symbol": ["AAPL"],
                "Close": [125.0],
                "Adjusted Close": [124.0],
                "Daily Change": [1.0],
                "Daily Change %": [0.8],
                "SMA 20": [120.0],
                "Rolling High 20": [130.0],
                "Rolling Low 20": [110.0],
                "Volume SMA 20": [1_000.0],
                "Relative Volume 20": [1.2],
            }
        )
        service = DashboardService()

        result = service.market_analysis("aapl", [20], "adjusted")

        self.assertEqual(result["symbol"], "AAPL")
        self.assertEqual(result["rows"][0]["price"], 124.0)
        self.assertEqual(result["rows"][0]["movingAverages"]["20"], 120.0)
        self.assertEqual(result["warnings"][0]["code"], "partial_data")
        detail = result["warnings"][0]["details"][0]
        self.assertEqual(detail["requestedWindow"], 20)
        self.assertEqual(detail["availableObservations"], 1)
        self.assertIn("movingAverages.20", detail["fields"])

    def test_searches_local_securities_and_only_uses_remote_when_requested(
        self,
    ) -> None:
        research = Mock()
        research.search.return_value = [{"symbol": "AAPL", "exchange": "NMS"}]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "quant.db"
            market_repository = MarketDataRepository(path)
            market_repository.save(
                pl.DataFrame(
                    {
                        "Date": [date(2026, 8, 21)],
                        "Symbol": ["AAPL"],
                        "Company": ["Apple Inc."],
                        "Close": [225.0],
                    }
                )
            )
            service = DashboardService(
                UserDataRepository(path), market_repository, research
            )

            local = service.security_search("apple")
            combined = service.security_search("apple", 5, remote=True)

        self.assertEqual(local["local"][0]["symbol"], "AAPL")
        self.assertEqual(local["remote"], [])
        self.assertEqual(combined["remote"][0]["exchange"], "NMS")
        research.search.assert_called_once_with("apple", 5)

    @patch("quant.dashboard.services.analyze_symbol_risk")
    def test_serializes_symbol_risk_analysis(self, analyze_symbol_risk) -> None:
        analyze_symbol_risk.return_value = {
            "metrics": pl.DataFrame(
                {
                    "Symbol": ["AAPL"],
                    "Latest Date": [date(2026, 8, 21)],
                    "Sharpe Ratio": [1.25],
                }
            ),
            "correlations": pl.DataFrame(
                {
                    "Symbol": ["AAPL"],
                    "Other Symbol": ["AAPL"],
                    "Correlation": [1.0],
                }
            ),
        }

        result = DashboardService().symbol_risk(["aapl"], "1y", "spy")

        self.assertEqual(result["metrics"][0]["latestDate"], "2026-08-21")
        self.assertEqual(result["metrics"][0]["sharpeRatio"], 1.25)
        self.assertEqual(result["correlations"][0]["otherSymbol"], "AAPL")

    @patch("quant.dashboard.services.screen_symbols_eod")
    def test_screener_defaults_to_holdings(
        self, screen_symbols_eod
    ) -> None:
        screen_symbols_eod.return_value = pl.DataFrame(
            {"Symbol": ["AAPL"], "Composite Score": [75.0]}
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "quant.db"
            repository = UserDataRepository(path)
            repository.add_position("AAPL", 1, 100)
            repository.add_position("MSFT", 1, 100)
            service = DashboardService(repository)

            result = service.screener()

        self.assertEqual(result["rows"][0]["compositeScore"], 75.0)
        self.assertEqual(
            screen_symbols_eod.call_args.args[0], ["AAPL", "MSFT"]
        )

    def test_research_methods_delegate_to_cached_provider_service(self) -> None:
        research = Mock()
        research.profile.return_value = {"longName": "Apple Inc."}
        service = DashboardService(research_service=research)

        self.assertEqual(
            service.research_profile("aapl"), {"longName": "Apple Inc."}
        )
        research.profile.assert_called_once_with("aapl")


if __name__ == "__main__":
    unittest.main()
