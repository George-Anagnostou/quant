import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import polars as pl

from quant.analysis import analyze_portfolio
from quant.dashboard.services import MAX_QUOTE_SYMBOLS, DashboardService
from quant.market_store import MarketDataRepository
from quant.user_data import UserDataRepository


class DashboardServiceTests(unittest.TestCase):
    def test_limits_batched_quote_requests(self) -> None:
        service = DashboardService()

        with self.assertRaisesRegex(ValueError, "At most"):
            service.quotes(
                [f"SYM{index}" for index in range(MAX_QUOTE_SYMBOLS + 1)]
            )

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


if __name__ == "__main__":
    unittest.main()
