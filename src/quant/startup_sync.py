"""Compatibility entry point for the reusable durable ingestion service."""
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from quant.database import DEFAULT_DATABASE_PATH
from quant.market_data import get_market_history, get_sp500_constituents
from quant.ingestion import IngestionService

DEFAULT_HORIZON = None
DEFAULT_MARKET_SYMBOLS = ("SPY",)
CORRECTION_OVERLAP_DAYS = 14

@dataclass
class SyncResult:
    requestedSymbols: int
    downloadedRows: int = 0
    savedRows: int = 0
    failedSymbols: list[str] = field(default_factory=list)
    universeSource: str = "stored"
    runId: str | None = None

class _Provider:
    name = "yahoo"
    def constituents(self):
        return get_sp500_constituents()
    def history(self, symbols, start):
        return get_market_history(symbols, start)

def synchronize_on_startup(path: Path = DEFAULT_DATABASE_PATH, *, horizon: date | None = None,
                           batch_size=50, today=None):
    run = IngestionService(path, _Provider()).synchronize(
        horizon=horizon, batch_size=batch_size, today=today, benchmarks=DEFAULT_MARKET_SYMBOLS)
    rows = sum(job["saved_rows"] for job in run["jobs"])
    return SyncResult(len(run["jobs"]), rows, rows,
                      [j["symbol"] for j in run["jobs"] if j["status"] != "complete"],
                      run["universe_source"], run["id"])
