import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import polars as pl

from quant.cli import (
    GREEN,
    RED,
    _build_parser,
    _colored_currency,
    _colored_decimal,
    _colored_percentage,
    main,
)


class CliTests(unittest.TestCase):
    def test_scopes_options_to_each_command(self) -> None:
        parser = _build_parser()

        index = parser.parse_args(["index", "--database", "market.db"])
        portfolio = parser.parse_args(["portfolio", "--database", "holdings.db"])
        market = parser.parse_args(
            [
                "market",
                "AAPL",
                "MSFT",
                "--windows",
                "5",
                "20",
                "--price",
                "adjusted",
            ]
        )

        self.assertEqual(index.database, Path("market.db"))
        self.assertEqual(portfolio.database, Path("holdings.db"))
        self.assertEqual(market.symbols, ["AAPL", "MSFT"])
        self.assertEqual(market.windows, [5, 20])
        self.assertEqual(market.price, "adjusted")

    @patch("quant.cli._run_market")
    def test_dispatches_market_analysis(self, run_market) -> None:
        main(["market", "AAPL", "--windows", "5", "--price", "close"])

        run_market.assert_called_once()
        args = run_market.call_args.args[0]
        self.assertEqual(args.symbols, ["AAPL"])

    @patch("quant.cli.analyze_symbols")
    @patch("quant.cli.get_index_symbols", return_value=["AAPL"])
    def test_allows_partial_provider_results_for_index_analysis(
        self,
        get_index_symbols,
        analyze_symbols,
    ) -> None:
        analyze_symbols.return_value = pl.DataFrame(
            schema={
                "Symbol": pl.String,
                "Date": pl.Date,
                "Close": pl.Float64,
                "Daily Change": pl.Float64,
                "Daily Change %": pl.Float64,
                "SMA 5": pl.Float64,
                "Rolling High 5": pl.Float64,
                "Rolling Low 5": pl.Float64,
                "Volume SMA 5": pl.Float64,
                "Relative Volume 5": pl.Float64,
            }
        )

        with redirect_stdout(StringIO()), patch("quant.cli._print_table"):
            main(["market", "--index", "--windows", "5", "--price", "close"])

        self.assertTrue(analyze_symbols.call_args.kwargs["allow_missing"])

    def test_requires_a_command(self) -> None:
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
            main([])

        self.assertEqual(error.exception.code, 2)

    def test_colors_positive_and_negative_changes(self) -> None:
        self.assertEqual(_colored_currency(12.5), f"{GREEN}$12.50\033[0m")
        self.assertEqual(_colored_percentage(-2.5), f"{RED}-2.50%\033[0m")
        self.assertEqual(_colored_decimal(0.0), "0.00")
        self.assertIsNone(_colored_percentage(None))


if __name__ == "__main__":
    unittest.main()
