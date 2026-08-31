import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import polars as pl

from quant.market_analysis import (
    MAX_ANALYSIS_WINDOW,
    analyze_symbol_risk,
    analyze_symbols,
    get_index_symbols,
    screen_symbols_eod,
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


class StoredEodAnalysisTests(unittest.TestCase):
    @staticmethod
    def _history(symbols: list[str], sessions: int = 260) -> pl.DataFrame:
        end = datetime.now().date()
        dates = [
            end - timedelta(days=sessions - index - 1)
            for index in range(sessions)
        ]
        return pl.DataFrame(
            {
                "Date": dates * len(symbols),
                "Symbol": [
                    symbol for symbol in symbols for _ in range(sessions)
                ],
                "Adjusted Close": [
                    100.0 + symbol_index * 20.0 + index * (symbol_index + 1)
                    for symbol_index, _ in enumerate(symbols)
                    for index in range(sessions)
                ],
                "Close": [
                    100.0 + symbol_index * 20.0 + index * (symbol_index + 1)
                    for symbol_index, _ in enumerate(symbols)
                    for index in range(sessions)
                ],
            }
        )

    @patch("quant.quotes.get_market_history")
    def test_analyzes_stored_history_without_network(self, get_market_history) -> None:
        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save(self._history(["AAA", "BBB", "SPY"]))

            result = analyze_symbol_risk(
                ["aaa", "BBB", "AAA"], repository=repository
            )

        get_market_history.assert_not_called()
        self.assertEqual(
            result["metrics"].get_column("Symbol").to_list(),
            ["AAA", "BBB"],
        )
        self.assertEqual(result["correlations"].height, 4)

    @patch("quant.quotes.get_market_history", side_effect=RuntimeError("offline"))
    def test_keeps_available_symbols_when_provider_is_partial(
        self, get_market_history
    ) -> None:
        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save(self._history(["AAA", "SPY"]))

            result = analyze_symbol_risk(
                ["AAA", "MISSING"], repository=repository
            )

        self.assertEqual(result["metrics"].get_column("Symbol").to_list(), ["AAA"])
        get_market_history.assert_called_once()

    @patch("quant.quotes.get_market_history", side_effect=RuntimeError("offline"))
    def test_requires_benchmark_history(self, get_market_history) -> None:
        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save(self._history(["AAA"]))

            with self.assertRaisesRegex(RuntimeError, "benchmark: SPY"):
                analyze_symbol_risk(["AAA"], repository=repository)

    def test_validates_period_and_symbol_collection(self) -> None:
        with self.assertRaisesRegex(ValueError, "Period must be"):
            analyze_symbol_risk(["AAA"], period="10y")
        with self.assertRaisesRegex(ValueError, "iterable of strings"):
            analyze_symbol_risk("AAA")
        with self.assertRaisesRegex(ValueError, "iterable of strings"):
            analyze_symbol_risk(["AAA", 1])

    @patch("quant.quotes.get_market_history")
    def test_scores_stored_eod_symbols_and_excludes_benchmark(
        self, get_market_history
    ) -> None:
        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save(self._history(["AAA", "SPY"]))

            result = screen_symbols_eod(["AAA"], repository=repository)

        get_market_history.assert_not_called()
        self.assertEqual(result.get_column("Symbol").to_list(), ["AAA"])
        self.assertIn("Composite Score", result.columns)


if __name__ == "__main__":
    unittest.main()
