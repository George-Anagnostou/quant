import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import polars as pl

from quant.market_store import MARKET_DATA_SCHEMA, MarketDataRepository
from quant.ingestion import IngestionService
from quant.user_data import UserDataRepository


def market_frame(symbols: list[str], session: date) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "Date": [session] * len(symbols),
            "Symbol": symbols,
            "Company": symbols,
            "Open": [100.0] * len(symbols),
            "High": [102.0] * len(symbols),
            "Low": [99.0] * len(symbols),
            "Close": [101.0] * len(symbols),
            "Adjusted Close": [101.0] * len(symbols),
            "Last Price": [101.0] * len(symbols),
            "Volume": [1_000] * len(symbols),
        },
        schema=MARKET_DATA_SCHEMA,
    )


class IngestionTests(unittest.TestCase):
    @patch("quant.ingestion.YahooProvider.history")
    @patch("quant.ingestion.YahooProvider.constituents")
    def test_syncs_universe_and_tracked_symbols_with_correction_overlap(
        self, constituents, download
    ) -> None:
        constituents.return_value = pl.DataFrame(
            {"Symbol": ["AAPL", "MSFT"], "Company": ["Apple", "Microsoft"]}
        )
        download.side_effect = lambda symbols, start: market_frame(
            symbols, date(2026, 8, 20)
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "quant.db"
            market = MarketDataRepository(path)
            market.save(market_frame(["AAPL"], date(2026, 8, 19)))
            users = UserDataRepository(path)
            users.add_watchlist("NVDA")
            users.add_position("GOOG", 1, 100)

            result = IngestionService(path, sleep=lambda _: None).synchronize(
                horizon=date(2025, 1, 1),
                batch_size=10,
                today=date(2026, 8, 21),
            )

            stored = set(market.load().get_column("Symbol").to_list())

        self.assertEqual(
            stored, {"AAPL", "MSFT", "NVDA", "GOOG", "SPY"}
        )
        self.assertEqual(len(result["jobs"]), 5)
        self.assertTrue(all(job["status"] == "complete" for job in result["jobs"]))
        starts = {symbol: start for call in download.call_args_list
                  for symbol in call.args[0] for start in [call.args[1]]}
        self.assertEqual(starts["AAPL"], date(2025, 1, 1))
        self.assertEqual(starts["MSFT"], date(2025, 1, 1))

    @patch("quant.ingestion.YahooProvider.history")
    @patch("quant.ingestion.YahooProvider.constituents")
    def test_splits_failed_batches_and_records_only_failed_symbols(
        self, constituents, download
    ) -> None:
        constituents.return_value = pl.DataFrame(
            {"Symbol": ["GOOD", "BAD"], "Company": ["Good", "Bad"]}
        )

        def provider(symbols, start):
            if "BAD" in symbols:
                raise RuntimeError("provider failed")
            return market_frame(symbols, date(2026, 8, 20))

        download.side_effect = provider
        with TemporaryDirectory() as directory:
            result = IngestionService(Path(directory) / "quant.db", sleep=lambda _: None).synchronize(
                benchmarks=(),
                horizon=date(2025, 1, 1),
                batch_size=10,
                today=date(2026, 8, 21),
            )

        self.assertEqual([job["symbol"] for job in result["jobs"] if job["status"] != "complete"], ["BAD"])
        self.assertGreaterEqual(download.call_count, 3)

    @patch("quant.ingestion.YahooProvider.history")
    @patch("quant.ingestion.YahooProvider.constituents")
    def test_uses_stored_universe_when_membership_refresh_fails(
        self, constituents, download
    ) -> None:
        constituents.side_effect = RuntimeError("offline")
        download.side_effect = lambda symbols, start: market_frame(
            symbols, date(2026, 8, 20)
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "quant.db"
            MarketDataRepository(path).save_universe(
                "sp500",
                pl.DataFrame({"Symbol": ["AAPL"], "Company": ["Apple"]}),
            )
            result = IngestionService(path, sleep=lambda _: None).synchronize(
                benchmarks=(),
                horizon=date(2025, 1, 1),
                today=date(2026, 8, 21),
            )

        self.assertEqual(result["universe_source"], "stored")
        self.assertTrue(all(job["status"] == "complete" for job in result["jobs"]))


if __name__ == "__main__":
    unittest.main()
