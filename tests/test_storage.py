import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

import polars as pl
from polars.testing import assert_frame_equal

from quant.database import database_connection
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

    def test_records_yahoo_share_class_symbol(self) -> None:
        market_data = pl.DataFrame(
            {
                "Date": [date(2026, 8, 21)],
                "Symbol": ["BRK.B"],
                "Close": [500.0],
            }
        )

        with TemporaryDirectory() as directory:
            path = Path(directory) / "quant.db"
            MarketDataRepository(path).save(market_data)

            with database_connection(path) as connection:
                provider_symbol = connection.execute(
                    "SELECT provider_symbol FROM provider_symbols"
                ).fetchone()[0]
            self.assertEqual(provider_symbol, "BRK-B")

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

            repository.save_universe(
                "sp500",
                market_data.filter(pl.col("Symbol") == "AAPL"),
            )
            self.assertEqual(repository.list_universe_symbols("sp500"), ["AAPL"])

    def test_concurrent_first_save_uses_the_persisted_security_id(self) -> None:
        market_data = pl.DataFrame(
            {
                "Date": [date(2026, 8, 21)],
                "Symbol": ["AAPL"],
                "Close": [225.50],
            }
        )

        with TemporaryDirectory() as directory:
            path = Path(directory) / "quant.db"

            def save() -> None:
                MarketDataRepository(path).save(market_data)

            with ThreadPoolExecutor(max_workers=12) as executor:
                futures = [executor.submit(save) for _ in range(24)]
                for future in futures:
                    future.result()

            self.assertEqual(MarketDataRepository(path).load().height, 1)


if __name__ == "__main__":
    unittest.main()
