import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

import polars as pl
from polars.testing import assert_frame_equal

from quant.market_store import MarketDataRepository


class MarketDataStorageTests(unittest.TestCase):
    def test_round_trips_market_data_through_sqlite(self) -> None:
        market_data = pl.DataFrame(
            {
                "Date": [date(2026, 8, 21)],
                "Symbol": ["AAPL"],
                "Company": ["Apple Inc."],
                "Open": [223.0],
                "High": [226.0],
                "Low": [222.0],
                "Close": [225.50],
                "Adjusted Close": [225.50],
                "Volume": [2_000_000],
            }
        )

        with TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "quant.db"
            repository = MarketDataRepository(path)
            repository.save(market_data)

            self.assertTrue(path.exists())
            assert_frame_equal(
                repository.load(columns=market_data.columns),
                market_data,
                check_dtypes=False,
            )

    def test_keeps_provider_observations_separate(self) -> None:
        market_data = pl.DataFrame(
            {
                "Date": [date(2026, 8, 21)],
                "Symbol": ["AAPL"],
                "Close": [225.50],
            }
        )

        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save(market_data, provider="yahoo")
            repository.save(
                market_data.with_columns(pl.lit(225.75).alias("Close")),
                provider="replacement",
            )

            self.assertEqual(repository.load(provider="yahoo")["Close"][0], 225.50)
            self.assertEqual(
                repository.load(provider="replacement")["Close"][0], 225.75
            )

    def test_records_ordered_universe_observations(self) -> None:
        market_data = pl.DataFrame(
            {
                "Date": [date(2026, 8, 21), date(2026, 8, 21)],
                "Symbol": ["MSFT", "AAPL"],
                "Close": [500.0, 225.50],
            }
        )

        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save(market_data, universe="sp500")

            self.assertEqual(
                repository.list_universe_symbols("sp500"),
                ["MSFT", "AAPL"],
            )


if __name__ == "__main__":
    unittest.main()
