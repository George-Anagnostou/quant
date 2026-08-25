import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import polars as pl

from quant.market_analysis import (
    MAX_ANALYSIS_WINDOW,
    analyze_symbols,
    get_index_symbols,
)
from quant.market_store import MarketDataRepository


class MarketAnalysisUniverseTests(unittest.TestCase):
    def test_uses_cached_index_symbols_without_network_access(self) -> None:
        cached = pl.DataFrame(
            {
                "Date": [date(2026, 8, 21), date(2026, 8, 21)],
                "Symbol": ["AAPL", "MSFT"],
            }
        )

        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save_universe("sp500", cached)

            self.assertEqual(get_index_symbols(repository), ["AAPL", "MSFT"])

    @patch("quant.quotes.get_market_history")
    def test_rejects_partial_provider_history(self, get_market_history) -> None:
        get_market_history.return_value = pl.DataFrame(
            {
                "Date": [date(2026, 8, 20), date(2026, 8, 21)],
                "Symbol": ["AAPL", "AAPL"],
                "High": [101.0, 102.0],
                "Low": [99.0, 100.0],
                "Close": [100.0, 101.0],
                "Volume": [1_000, 1_100],
            }
        )
        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")

            with self.assertRaisesRegex(RuntimeError, "MSFT"):
                analyze_symbols(
                    ["AAPL", "MSFT"],
                    [2],
                    "Close",
                    repository,
                )

    def test_bounds_analysis_windows_before_date_arithmetic(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            analyze_symbols(["AAPL"], [MAX_ANALYSIS_WINDOW + 1], "Close")


if __name__ == "__main__":
    unittest.main()
