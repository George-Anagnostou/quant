import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

import polars as pl

from quant.market_analysis import get_index_symbols
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


if __name__ == "__main__":
    unittest.main()
