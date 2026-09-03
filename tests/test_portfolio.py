import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import polars as pl

from quant.portfolio import (
    analyze_portfolio_risk,
    analyze_positions,
    load_portfolio_market_data,
)
from quant.market_store import MarketDataRepository


class PortfolioInputTests(unittest.TestCase):
    @patch("quant.quotes.get_market_history")
    def test_downloads_and_caches_only_missing_symbols(self, get_market_history) -> None:
        primary_data = pl.DataFrame(
            {
                "Date": [date(2026, 8, 21)],
                "Symbol": ["AAPL"],
                "Company": ["Apple Inc."],
                "Last Price": [225.0],
                "Volume": [2_000_000],
            }
        )
        downloaded = pl.DataFrame(
            {
                "Date": [date(2026, 8, 21)],
                "Symbol": ["VOO"],
                "Last Price": [700.0],
                "Volume": [3_000_000],
            }
        )
        get_market_history.return_value = downloaded

        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save(primary_data)

            result = load_portfolio_market_data(["AAPL", "VOO"], repository)

            get_market_history.assert_called_once_with(["VOO"])
            self.assertEqual(set(result.get_column("Symbol")), {"AAPL", "VOO"})
            self.assertEqual(
                repository.load(["VOO"]).get_column("Symbol").to_list(), ["VOO"]
            )

            get_market_history.reset_mock()
            load_portfolio_market_data(["AAPL", "VOO"], repository)
            get_market_history.assert_not_called()

    @patch("quant.portfolio.load_portfolio_market_data")
    def test_analyzes_positions_through_shared_quote_resolution(
        self,
        load_portfolio_market_data,
    ) -> None:
        positions = pl.DataFrame(
            {
                "Symbol": ["AAPL", "AAPL"],
                "Quantity": [1.0, 2.0],
                "Average Cost": [100.0, 110.0],
                "Account": ["taxable", "roth"],
            }
        )
        load_portfolio_market_data.return_value = pl.DataFrame(
            {
                "Date": [date(2026, 8, 21)],
                "Symbol": ["AAPL"],
                "Last Price": [125.0],
                "Volume": [2_000_000],
            }
        )

        result = analyze_positions(positions)

        self.assertEqual(result.height, 2)
        self.assertEqual(result.get_column("Market Value").to_list(), [250.0, 125.0])
        load_portfolio_market_data.assert_called_once()
        self.assertEqual(load_portfolio_market_data.call_args.args[0], ["AAPL"])

    @patch("quant.quotes.get_market_history")
    def test_does_not_hide_storage_failures_for_optional_quotes(
        self,
        get_market_history,
    ) -> None:
        get_market_history.return_value = pl.DataFrame(
            {
                "Date": [date(2026, 8, 21)],
                "Symbol": ["AAPL"],
                "Last Price": [125.0],
            }
        )
        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            with patch.object(
                repository,
                "save",
                side_effect=ValueError("invalid provider data"),
            ):
                with self.assertRaisesRegex(RuntimeError, "invalid provider data"):
                    load_portfolio_market_data(["AAPL"], repository)

    @patch("quant.portfolio.load_eod_analysis_history")
    def test_composes_portfolio_risk_from_shared_analytics(
        self, load_eod_analysis_history
    ) -> None:
        positions = pl.DataFrame(
            {
                "Symbol": ["AAA", "BBB"],
                "Quantity": [2.0, 1.0],
            }
        )
        dates = [date(2026, 1, day) for day in range(1, 6)]
        history = pl.DataFrame(
            {
                "Date": dates * 3,
                "Symbol": ["AAA"] * 5 + ["BBB"] * 5 + ["SPY"] * 5,
                "Adjusted Close": [
                    10.0,
                    11.0,
                    10.5,
                    12.0,
                    13.0,
                    20.0,
                    19.0,
                    21.0,
                    22.0,
                    21.5,
                    100.0,
                    101.0,
                    100.5,
                    102.0,
                    103.0,
                ],
            }
        )
        load_eod_analysis_history.return_value = (
            ["AAA", "BBB"],
            "SPY",
            history,
            history,
        )

        result = analyze_portfolio_risk(positions)

        self.assertEqual(result["history"].height, 5)
        self.assertEqual(result["metrics"].item(0, "Symbol"), "Portfolio")
        self.assertAlmostEqual(
            result["return_contributions"]
            .get_column("Return Contribution")
            .sum(),
            result["history"].item(-1, "Portfolio Value")
            / result["history"].item(0, "Portfolio Value")
            - 1.0,
        )
        self.assertAlmostEqual(
            result["risk_contributions"]
            .get_column("Variance Risk Contribution")
            .sum(),
            1.0,
        )
        self.assertEqual(result["correlations"].height, 4)

    @patch("quant.portfolio.load_eod_analysis_history")
    def test_rejects_incomplete_portfolio_risk_history(
        self, load_eod_analysis_history
    ) -> None:
        positions = pl.DataFrame(
            {"Symbol": ["AAA", "BBB"], "Quantity": [1.0, 1.0]}
        )
        history = pl.DataFrame(
            {
                "Date": [date(2026, 1, 1), date(2026, 1, 2)] * 2,
                "Symbol": ["AAA", "AAA", "SPY", "SPY"],
                "Adjusted Close": [10.0, 11.0, 100.0, 101.0],
            }
        )
        load_eod_analysis_history.return_value = (
            ["AAA", "BBB"],
            "SPY",
            history,
            history,
        )

        with self.assertRaisesRegex(RuntimeError, "BBB"):
            analyze_portfolio_risk(positions)

    @patch("quant.portfolio.load_eod_analysis_history")
    def test_requires_two_benchmark_observations(
        self, load_eod_analysis_history
    ) -> None:
        positions = pl.DataFrame({"Symbol": ["AAA"], "Quantity": [1.0]})
        history = pl.DataFrame(
            {
                "Date": [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 2)],
                "Symbol": ["AAA", "AAA", "SPY"],
                "Adjusted Close": [10.0, 11.0, 100.0],
            }
        )
        load_eod_analysis_history.return_value = (
            ["AAA"],
            "SPY",
            history,
            history,
        )

        with self.assertRaisesRegex(RuntimeError, "benchmark: SPY"):
            analyze_portfolio_risk(positions)

    @patch("quant.quotes.get_market_history")
    def test_values_benchmark_when_it_is_also_a_portfolio_holding(
        self, get_market_history
    ) -> None:
        today = date(2026, 9, 1)
        recent_dates = [
            today - timedelta(days=21 - index) for index in range(22)
        ]
        dates = [
            date(today.year - 1, 12, 31),
            date(today.year, 1, 2),
            *recent_dates,
        ]
        history = pl.DataFrame(
            {
                "Date": dates * 2,
                "Symbol": ["AAA"] * 24 + ["SPY"] * 24,
                "Close": [100.0 + index for index in range(24)]
                + [200.0 + index * 2 for index in range(24)],
                "Adjusted Close": [100.0 + index for index in range(24)]
                + [200.0 + index * 2 for index in range(24)],
            }
        )
        positions = pl.DataFrame(
            {"Symbol": ["aaa", "spy"], "Quantity": [1.0, 2.0]}
        )
        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save(history)

            with patch("quant.market_analysis.datetime") as current_datetime:
                current_datetime.now.return_value.date.return_value = today
                result = analyze_portfolio_risk(
                    positions, period="1mo", repository=repository
                )
                benchmark_only = analyze_portfolio_risk(
                    positions.filter(pl.col("Symbol") == "spy"),
                    period="1mo",
                    repository=repository,
                )

        get_market_history.assert_not_called()
        self.assertEqual(result["history"].height, 22)
        self.assertEqual(
            set(result["return_contributions"].get_column("Symbol")),
            {"AAA", "SPY"},
        )
        self.assertEqual(
            set(result["risk_contributions"].get_column("Symbol")),
            {"AAA", "SPY"},
        )
        self.assertAlmostEqual(benchmark_only["metrics"].item(0, "Beta"), 1.0)
        self.assertEqual(
            benchmark_only["return_contributions"].get_column("Symbol").to_list(),
            ["SPY"],
        )

    def test_rejects_non_string_portfolio_symbols(self) -> None:
        positions = pl.DataFrame({"Symbol": [123], "Quantity": [1.0]})

        with self.assertRaisesRegex(ValueError, "symbols must be strings"):
            analyze_portfolio_risk(positions)


if __name__ == "__main__":
    unittest.main()
