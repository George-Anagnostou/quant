from __future__ import annotations

import polars as pl

from quant.analysis import summarize_allocation, summarize_portfolio
from quant.market_analysis import analyze_symbols
from quant.market_data import latest_market_snapshot
from quant.market_store import MarketDataRepository
from quant.portfolio import analyze_positions
from quant.quotes import resolve_market_history
from quant.user_data import UserDataRepository


MAX_QUOTE_SYMBOLS = 20


class DashboardService:
    def __init__(
        self,
        repository: UserDataRepository | None = None,
        market_repository: MarketDataRepository | None = None,
    ) -> None:
        self.repository = repository or UserDataRepository()
        self.market_repository = market_repository or MarketDataRepository(
            self.repository.path
        )

    def watchlist(self) -> list[str]:
        return self.repository.list_watchlist()

    def add_watchlist(self, symbol: str) -> list[str]:
        return self.repository.add_watchlist(symbol)

    def remove_watchlist(self, symbol: str) -> list[str]:
        return self.repository.remove_watchlist(symbol)

    def quotes(self, symbols: list[str], refresh: bool = False) -> dict:
        symbols = list(
            dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip())
        )
        if not symbols:
            return {"quotes": []}
        if len(symbols) > MAX_QUOTE_SYMBOLS:
            raise ValueError(f"At most {MAX_QUOTE_SYMBOLS} symbols are allowed")
        resolve_market_history(
            symbols,
            self.market_repository,
            ["Date", "Symbol", "Close"],
            minimum_sessions=2,
            refresh=refresh,
            allow_missing=True,
        )
        history = self.market_repository.load(
            symbols,
            columns=["Date", "Symbol", "Company", "Close", "Volume"],
        ).sort(["Symbol", "Date"])
        history = history.with_columns(
            pl.col("Close").shift(1).over("Symbol").alias("Previous Close")
        )
        latest = {
            row["Symbol"]: row
            for row in latest_market_snapshot(history).to_dicts()
        }
        quotes = []
        for symbol in symbols:
            row = latest.get(symbol)
            if row is None:
                quotes.append({"symbol": symbol, "error": True})
                continue
            previous = row["Previous Close"]
            change = row["Close"] - previous if previous is not None else None
            change_percent = (
                change / previous * 100.0 if change is not None and previous else None
            )
            quotes.append(
                {
                    "symbol": symbol,
                    "name": row["Company"] or symbol,
                    "price": row["Close"],
                    "previousClose": previous,
                    "change": change,
                    "changePercent": change_percent,
                    "volume": row["Volume"],
                    "asOf": row["Date"].isoformat(),
                    "currency": "USD",
                }
            )
        return {"quotes": quotes}

    def quote(self, symbol: str, refresh: bool = False) -> dict:
        quotes = self.quotes([symbol], refresh)["quotes"]
        if not quotes:
            raise ValueError("Symbol is required")
        quote = quotes[0]
        if quote.get("error"):
            raise ValueError(f"Market data unavailable for {symbol.upper()}")
        return quote

    def holdings(self, refresh: bool = False) -> dict:
        frame = self.repository.positions_frame()
        if frame.is_empty():
            return {
                "holdings": [],
                "totals": {
                    "cost": 0.0,
                    "pricedCost": 0.0,
                    "value": 0.0,
                    "gain": 0.0,
                    "gainPercent": None,
                },
                "allocations": {
                    "account": [],
                    "assetClass": [],
                    "sector": [],
                },
                "unpricedSymbols": [],
            }

        analysis = analyze_positions(
            frame,
            self.market_repository,
            refresh,
        )
        summary = summarize_portfolio(analysis).row(0, named=True)
        holdings = [self._holding_response(row) for row in analysis.to_dicts()]
        unpriced_symbols = (
            analysis.filter(pl.col("Last Price").is_null())
            .get_column("Symbol")
            .unique(maintain_order=True)
            .to_list()
        )
        return {
            "holdings": holdings,
            "totals": {
                "cost": summary["Cost Basis"],
                "pricedCost": summary["Priced Cost Basis"],
                "value": summary["Market Value"],
                "gain": summary["Gain/Loss"],
                "gainPercent": summary["Gain/Loss %"],
            },
            "allocations": {
                "account": self._allocation_response(analysis, "Account"),
                "assetClass": self._allocation_response(analysis, "Asset Class"),
                "sector": self._allocation_response(analysis, "Sector"),
            },
            "unpricedSymbols": unpriced_symbols,
        }

    def add_holding(
        self,
        symbol: str,
        shares: float,
        cost_basis: float,
        account: str | None = None,
        asset_class: str | None = None,
        sector: str | None = None,
        acquired: str | None = None,
    ) -> dict:
        return self.repository.add_position(
            symbol,
            shares,
            cost_basis,
            account,
            asset_class,
            sector,
            acquired,
        )

    def remove_holding(self, holding_id: str) -> bool:
        return self.repository.remove_position(holding_id)

    def market_analysis(
        self,
        symbol: str,
        windows: list[int],
        price: str,
        refresh: bool = False,
    ) -> dict:
        price_column = "Adjusted Close" if price == "adjusted" else "Close"
        analysis = analyze_symbols(
            [symbol],
            windows,
            price_column,
            self.market_repository,
            refresh,
        )
        rows = []
        for row in analysis.sort("Date").to_dicts():
            rows.append(
                {
                    "date": row["Date"].isoformat(),
                    "price": row[price_column],
                    "dailyChange": row["Daily Change"],
                    "dailyChangePercent": row["Daily Change %"],
                    "movingAverages": {
                        str(window): row[f"SMA {window}"] for window in windows
                    },
                    "rollingHighs": {
                        str(window): row[f"Rolling High {window}"]
                        for window in windows
                    },
                    "rollingLows": {
                        str(window): row[f"Rolling Low {window}"]
                        for window in windows
                    },
                    "volumeAverages": {
                        str(window): row[f"Volume SMA {window}"]
                        for window in windows
                    },
                    "relativeVolumes": {
                        str(window): row[f"Relative Volume {window}"]
                        for window in windows
                    },
                }
            )
        return {
            "symbol": symbol.upper(),
            "priceBasis": price,
            "windows": windows,
            "rows": rows,
        }

    @staticmethod
    def _holding_response(row: dict) -> dict:
        as_of = row["As Of"]
        return {
            "id": row["ID"],
            "symbol": row["Symbol"],
            "shares": row["Quantity"],
            "costBasis": row["Average Cost"],
            "price": row["Last Price"],
            "marketValue": row["Market Value"],
            "costValue": row["Cost Basis"],
            "gain": row["Gain/Loss"],
            "gainPercent": row["Gain/Loss %"],
            "weightPercent": row["Weight %"],
            "asOf": as_of.isoformat() if as_of is not None else None,
            "marketDataAvailable": row["Last Price"] is not None,
            "account": row["Account"],
            "assetClass": row["Asset Class"],
            "sector": row["Sector"],
            "acquired": row["Acquired"],
        }

    @staticmethod
    def _allocation_response(analysis: pl.DataFrame, dimension: str) -> list[dict]:
        priced = analysis.filter(pl.col("Market Value").is_not_null())
        if priced.is_empty():
            return []
        allocation = summarize_allocation(priced, dimension)
        return [
            {
                "name": row[dimension],
                "cost": row["Cost Basis"],
                "value": row["Market Value"],
                "gain": row["Gain/Loss"],
                "weightPercent": row["Weight %"],
            }
            for row in allocation.to_dicts()
        ]
