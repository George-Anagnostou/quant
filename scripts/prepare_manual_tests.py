"""Create synthetic offline inputs in an empty directory, never the personal DB.

Only fixture setup uses repositories. The manual acceptance workflow exercises
the public API/CLI against the resulting isolated database.
"""
import argparse
from datetime import date
import json
from pathlib import Path

import polars as pl

from quant.calendars import sessions
from quant.contracts import SecurityMetadataInput, SnapshotInput
from quant.market_store import MarketDataRepository
from quant.platform_service import PlatformService


def prepare(directory: Path):
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError("Choose an empty directory; existing files will not be overwritten")
    database = directory / "manual-quant.db"
    days = sessions(date(2026, 6, 1), date(2026, 8, 31))
    rows = []
    last = len(days) - 1
    for index, day in enumerate(days):
        for symbol, terminal, step in [("AAPL", 150.0, 0.5), ("SPY", 600.0, 0.7)]:
            price = round(terminal - (last-index)*step + ((index % 3)-(last % 3))*0.1, 2)
            rows.append({"Date": day, "Symbol": symbol, "Open": price,
                         "High": price+1, "Low": price-1, "Close": price,
                         "Adjusted Close": price, "Volume": 1000})
    MarketDataRepository(database).save(pl.DataFrame(rows))
    platform = PlatformService(database)
    for symbol, kind in [("AAPL", "equity"), ("SPY", "etf")]:
        platform.security_metadata(SecurityMetadataInput(
            importKey=f"manual-metadata-{symbol}", symbol=symbol, currency="USD",
            calendar="XNYS", instrumentType=kind, source="Synthetic manual test fixture"))
    snapshot = SnapshotInput(
        importKey="manual-snapshot", date="2026-08-31", account="synthetic-account",
        source="Synthetic manual test fixture; not real prices or holdings", cash=500,
        positions=[{"symbol": "AAPL", "quantity": 10, "averageCost": 100,
                    "marketValue": 1500, "sector": "Technology", "assetClass": "equity"}],
    ).model_dump(mode="json")
    write(directory, "snapshot.json", snapshot)
    write(directory, "snapshot-conflict.json", {**snapshot, "cash": 501})
    write(directory, "snapshot-unknown-cash.json", {**snapshot, "importKey": "manual-unknown-cash", "cash": None})
    write(directory, "snapshot-invalid.json", {**snapshot, "positions": snapshot["positions"] * 2})
    write(directory, "metadata-change.json", {
        "importKey": "manual-metadata-change", "symbol": "AAPL", "currency": "EUR",
        "calendar": "XNYS", "instrumentType": "equity", "source": "Deliberate synthetic currency conflict"})
    write(directory, "metadata-restore.json", {
        "importKey": "manual-metadata-restore", "symbol": "AAPL", "currency": "USD",
        "calendar": "XNYS", "instrumentType": "equity", "source": "Restore verified synthetic metadata"})
    write(directory, "sync.json", {"importKey": "manual-sync", "symbols": ["AAPL", "SPY"], "horizon": "2026-06-01"})
    write(directory, "expected.json", {
        "synthetic": True, "snapshotDate": "2026-08-31", "aaplClose": 150,
        "spyClose": 600, "positionsValue": 1500, "cash": 500, "accountValue": 2000,
        "oneMonthRiskReturns": 21, "oneMonthRequiredPricePoints": 22,
        "oneYearRiskReadiness": "blocked", "oneMonthReadiness": "ready",
    })
    return {"directory": str(directory), "database": str(database), "synthetic": True}


def write(directory, filename, value):
    with (directory / filename).open("x") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True, help="empty directory for synthetic test files")
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.directory), indent=2))
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
