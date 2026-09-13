import unittest
from datetime import date, timedelta
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
        with self.assertRaisesRegex(ValueError, "positive integers"):
            analyze_symbols(["AAPL"], [True], "Close")

    @patch("quant.quotes.get_market_history", side_effect=AssertionError("network"))
    def test_returns_available_indicators_for_short_stored_history(
        self, get_market_history
    ) -> None:
        sessions = 60
        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save(
                pl.DataFrame(
                    {
                        "Date": [
                            date(2026, 6, 12) + timedelta(days=index)
                            for index in range(sessions)
                        ],
                        "Symbol": ["SPCX"] * sessions,
                        "High": [101.0 + index for index in range(sessions)],
                        "Low": [99.0 + index for index in range(sessions)],
                        "Close": [100.0 + index for index in range(sessions)],
                        "Adjusted Close": [
                            100.0 + index for index in range(sessions)
                        ],
                        "Volume": [1_000 + index for index in range(sessions)],
                    }
                )
            )

            result = analyze_symbols(
                ["SPCX"],
                [20, 50, 200],
                "Adjusted Close",
                repository,
                fetch_missing=False,
            )

        self.assertEqual(result.height, sessions)
        self.assertIsNotNone(result.item(-1, "SMA 20"))
        self.assertIsNotNone(result.item(-1, "SMA 50"))
        self.assertIsNone(result.item(-1, "SMA 200"))
        get_market_history.assert_not_called()

    @patch("quant.quotes.get_market_history")
    def test_accepts_short_history_after_attempting_full_provider_window(
        self, get_market_history
    ) -> None:
        get_market_history.return_value = pl.DataFrame(
            {
                "Date": [date(2026, 8, 20), date(2026, 8, 21)],
                "Symbol": ["SPCX", "SPCX"],
                "High": [101.0, 102.0],
                "Low": [99.0, 100.0],
                "Close": [100.0, 101.0],
                "Adjusted Close": [100.0, 101.0],
                "Volume": [1_000, 1_100],
            }
        )
        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")

            result = analyze_symbols(
                ["SPCX"], [20, 50, 200], "Adjusted Close", repository
            )

        self.assertEqual(result.height, 2)
        self.assertIsNone(result.item(-1, "SMA 20"))
        get_market_history.assert_called_once()

    @patch("quant.quotes.get_market_history", side_effect=RuntimeError("offline"))
    def test_explicit_refresh_still_reports_provider_failure(
        self, get_market_history
    ) -> None:
        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save(
                pl.DataFrame(
                    {
                        "Date": [date(2026, 8, 20), date(2026, 8, 21)],
                        "Symbol": ["SPCX", "SPCX"],
                        "High": [101.0, 102.0],
                        "Low": [99.0, 100.0],
                        "Close": [100.0, 101.0],
                        "Adjusted Close": [100.0, 101.0],
                        "Volume": [1_000, 1_100],
                    }
                )
            )

            with self.assertRaisesRegex(RuntimeError, "offline"):
                analyze_symbols(
                    ["SPCX"],
                    [200],
                    "Adjusted Close",
                    repository,
                    refresh=True,
                )

        get_market_history.assert_called_once()

    @patch("quant.quotes.get_market_history", side_effect=AssertionError("network"))
    def test_partial_history_does_not_drop_invalid_sessions(
        self, get_market_history
    ) -> None:
        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save(
                pl.DataFrame(
                    {
                        "Date": [
                            date(2026, 8, 19),
                            date(2026, 8, 20),
                            date(2026, 8, 21),
                        ],
                        "Symbol": ["SPCX"] * 3,
                        "High": [101.0, 102.0, 103.0],
                        "Low": [99.0, 100.0, 101.0],
                        "Close": [100.0, 101.0, 102.0],
                        "Adjusted Close": [100.0, 101.0, 102.0],
                        "Volume": [1_000, None, 1_200],
                    }
                )
            )

            with self.assertRaisesRegex(ValueError, "prices and volume"):
                analyze_symbols(
                    ["SPCX"],
                    [2],
                    "Adjusted Close",
                    repository,
                    fetch_missing=False,
                )

        get_market_history.assert_not_called()

    @patch("quant.quotes.get_market_history", side_effect=AssertionError("network"))
    def test_allow_missing_preserves_one_session_window_one_analysis(
        self, get_market_history
    ) -> None:
        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save(
                pl.DataFrame(
                    {
                        "Date": [date(2026, 8, 21)],
                        "Symbol": ["SPCX"],
                        "High": [101.0],
                        "Low": [99.0],
                        "Close": [100.0],
                        "Adjusted Close": [100.0],
                        "Volume": [1_000],
                    }
                )
            )

            result = analyze_symbols(
                ["SPCX"],
                [1],
                "Adjusted Close",
                repository,
                allow_missing=True,
                fetch_missing=False,
            )

        self.assertEqual(result.height, 1)
        self.assertEqual(result.item(0, "SMA 1"), 100.0)
        get_market_history.assert_not_called()


class StoredEodAnalysisTests(unittest.TestCase):
    CURRENT_DATE = date(2026, 9, 1)

    def setUp(self) -> None:
        self.datetime_patcher = patch("quant.market_analysis.datetime")
        current_datetime = self.datetime_patcher.start()
        current_datetime.now.return_value.date.return_value = self.CURRENT_DATE

    def tearDown(self) -> None:
        self.datetime_patcher.stop()

    @staticmethod
    def _history(symbols: list[str], sessions: int = 260) -> pl.DataFrame:
        end = StoredEodAnalysisTests.CURRENT_DATE
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
        self.assertEqual(
            result["metrics"].get_column("Observations").to_list(),
            [252, 252],
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

            result = screen_symbols_eod(["SPY", "AAA"], repository=repository)

        get_market_history.assert_not_called()
        self.assertEqual(result.get_column("Symbol").to_list(), ["AAA"])
        self.assertIn("Composite Score", result.columns)

    @patch("quant.quotes.get_market_history", side_effect=RuntimeError("offline"))
    def test_excludes_symbols_without_the_complete_benchmark_period(
        self, get_market_history
    ) -> None:
        full = self._history(["AAA", "SPY"], sessions=253)
        short = self._history(["SHORT"], sessions=252)
        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save(pl.concat([full, short], how="diagonal_relaxed"))

            risk = analyze_symbol_risk(
                ["AAA", "SHORT"], repository=repository
            )
            screen = screen_symbols_eod(
                ["AAA", "SHORT"], repository=repository
            )

        self.assertEqual(risk["metrics"].get_column("Symbol").to_list(), ["AAA"])
        self.assertEqual(screen.get_column("Symbol").to_list(), ["AAA"])
        self.assertEqual(get_market_history.call_count, 2)

    @patch("quant.quotes.get_market_history", side_effect=RuntimeError("offline"))
    def test_refreshes_equal_count_history_with_a_benchmark_date_gap(
        self, get_market_history
    ) -> None:
        full = self._history(["AAA", "SPY"], sessions=254)
        dates = full.filter(pl.col("Symbol") == "SPY").get_column("Date").to_list()
        gapped_dates = [dates[0] - timedelta(days=1), *dates]
        gapped_dates.remove(dates[100])
        gapped = pl.DataFrame(
            {
                "Date": gapped_dates,
                "Symbol": ["GAPPED"] * len(gapped_dates),
                "Close": [150.0 + index for index in range(len(gapped_dates))],
                "Adjusted Close": [
                    150.0 + index for index in range(len(gapped_dates))
                ],
            }
        )
        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save(pl.concat([full, gapped], how="diagonal_relaxed"))

            result = analyze_symbol_risk(
                ["AAA", "GAPPED"], repository=repository
            )

        self.assertEqual(result["metrics"].get_column("Symbol").to_list(), ["AAA"])
        self.assertEqual(get_market_history.call_args.args[0], ["GAPPED"])

    @patch("quant.quotes.get_market_history")
    def test_uses_benchmark_calendar_when_asset_has_an_extra_date(
        self, get_market_history
    ) -> None:
        benchmark = self._history(["SPY"], sessions=254)
        dates = benchmark.get_column("Date").to_list()
        asset_dates = [*dates, dates[-1] + timedelta(days=1)]
        asset = pl.DataFrame(
            {
                "Date": asset_dates,
                "Symbol": ["AAA"] * len(asset_dates),
                "Close": [100.0 + index for index in range(len(asset_dates))],
                "Adjusted Close": [
                    100.0 + index for index in range(len(asset_dates))
                ],
            }
        )
        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save(pl.concat([asset, benchmark], how="diagonal_relaxed"))

            result = analyze_symbol_risk(["AAA"], repository=repository)

        get_market_history.assert_not_called()
        self.assertEqual(result["metrics"].item(0, "Observations"), 252)

    @patch("quant.quotes.get_market_history")
    def test_refreshes_missing_period_context_for_ytd(
        self, get_market_history
    ) -> None:
        current_date = date(2026, 9, 1)
        dates = [current_date - timedelta(days=253 - index) for index in range(254)]
        benchmark = pl.DataFrame(
            {
                "Date": dates,
                "Symbol": ["SPY"] * len(dates),
                "Close": [200.0 + index for index in range(len(dates))],
                "Adjusted Close": [
                    200.0 + index for index in range(len(dates))
                ],
            }
        )
        ytd_base = max(value for value in dates if value.year == 2025)
        gapped_dates = [dates[0] - timedelta(days=1), *dates]
        gapped_dates.remove(ytd_base)
        incomplete = pl.DataFrame(
            {
                "Date": gapped_dates,
                "Symbol": ["AAA"] * len(gapped_dates),
                "Close": [100.0 + index for index in range(len(gapped_dates))],
                "Adjusted Close": [
                    100.0 + index for index in range(len(gapped_dates))
                ],
            }
        )
        complete = pl.DataFrame(
            {
                "Date": dates,
                "Symbol": ["AAA"] * len(dates),
                "Close": [100.0 + index for index in range(len(dates))],
                "Adjusted Close": [
                    100.0 + index for index in range(len(dates))
                ],
            }
        )
        get_market_history.return_value = complete
        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save(
                pl.concat([benchmark, incomplete], how="diagonal_relaxed")
            )
            with patch("quant.market_analysis.datetime") as current_datetime:
                current_datetime.now.return_value.date.return_value = current_date
                result = analyze_symbol_risk(["AAA"], repository=repository)

        self.assertEqual(get_market_history.call_args.args[0], ["AAA"])
        self.assertIsNotNone(result["metrics"].item(0, "YTD Return"))

    @patch("quant.quotes.get_market_history", side_effect=RuntimeError("offline"))
    def test_does_not_substitute_an_earlier_asset_date_for_ytd_base(
        self, get_market_history
    ) -> None:
        dates = [
            self.CURRENT_DATE - timedelta(days=253 - index)
            for index in range(254)
        ]
        ytd_base = max(value for value in dates if value.year == 2025)
        benchmark = pl.DataFrame(
            {
                "Date": dates,
                "Symbol": ["SPY"] * len(dates),
                "Close": [200.0 + index for index in range(len(dates))],
                "Adjusted Close": [
                    200.0 + index for index in range(len(dates))
                ],
            }
        )
        asset_dates = [value for value in dates if value != ytd_base]
        asset = pl.DataFrame(
            {
                "Date": asset_dates,
                "Symbol": ["AAA"] * len(asset_dates),
                "Close": [100.0 + index for index in range(len(asset_dates))],
                "Adjusted Close": [
                    100.0 + index for index in range(len(asset_dates))
                ],
            }
        )
        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save(pl.concat([asset, benchmark], how="diagonal_relaxed"))

            result = analyze_symbol_risk(
                ["AAA"], period="1mo", repository=repository
            )

        self.assertEqual(get_market_history.call_args.args[0], ["AAA"])
        self.assertIsNone(result["metrics"].item(0, "YTD Return"))

    @patch("quant.quotes.get_market_history")
    def test_keeps_prior_year_close_as_ytd_context_outside_exact_risk_period(
        self, get_market_history
    ) -> None:
        prior_close = date(2024, 12, 31)
        current_dates = [
            date(2025, 1, 1) + timedelta(days=index) for index in range(253)
        ]
        dates = [prior_close, *current_dates]
        aaa_prices = [90.0, *[100.0 + index for index in range(253)]]
        spy_prices = [180.0, *[200.0 + index for index in range(253)]]
        history = pl.DataFrame(
            {
                "Date": dates * 2,
                "Symbol": ["AAA"] * 254 + ["SPY"] * 254,
                "Close": aaa_prices + spy_prices,
                "Adjusted Close": aaa_prices + spy_prices,
            }
        )
        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save(history)
            with patch("quant.market_analysis.datetime") as current_datetime:
                current_datetime.now.return_value.date.return_value = date(2026, 1, 2)
                result = analyze_symbol_risk(["AAA"], repository=repository)

        get_market_history.assert_not_called()
        row = result["metrics"].row(0, named=True)
        self.assertEqual(row["Observations"], 252)
        self.assertAlmostEqual(row["YTD Return"], aaa_prices[-1] / 90.0 - 1.0)


if __name__ == "__main__":
    unittest.main()
