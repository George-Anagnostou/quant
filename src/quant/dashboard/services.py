from __future__ import annotations

import re
from datetime import date, datetime

import polars as pl

from quant.analysis import summarize_allocation, summarize_portfolio
from quant.database import is_database_initialized
from quant.market_analysis import (
    analyze_symbol_risk,
    analyze_symbols,
    screen_symbols_eod,
)
from quant.market_data import latest_market_snapshot
from quant.market_store import MarketDataRepository
from quant.portfolio import analyze_portfolio_risk, analyze_positions
from quant.quotes import resolve_market_history
from quant.research import CachedResearchService, YahooResearchProvider
from quant.user_data import UserDataRepository


MAX_QUOTE_SYMBOLS = 20
MAX_SCREENER_SYMBOLS = 100


class DashboardService:
    def __init__(
        self,
        repository: UserDataRepository | None = None,
        market_repository: MarketDataRepository | None = None,
        research_service: CachedResearchService | None = None,
        *,
        read_only: bool = False,
    ) -> None:
        self.repository = repository or UserDataRepository(read_only=read_only)
        self.market_repository = market_repository or MarketDataRepository(
            self.repository.path, read_only=read_only
        )
        self.research_service = research_service or CachedResearchService(
            YahooResearchProvider()
        )

    def market_database_ready(self) -> bool:
        return is_database_initialized(self.market_repository.path)

    def user_database_ready(self) -> bool:
        return is_database_initialized(self.repository.path)

    def _writer(self):
        return UserDataRepository(self.repository.path, self.repository.user_id)

    def quotes(
        self,
        symbols: list[str],
        refresh: bool = False,
        fetch_missing: bool = False,
    ) -> dict:
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
            fetch_missing=fetch_missing,
        )
        history = self.market_repository.load(
            symbols,
            columns=[
                "Date",
                "Symbol",
                "Company",
                "Close",
                "Volume",
                "Provider",
                "Retrieved At",
            ],
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
                    "provider": row["Provider"],
                    "retrievedAt": row["Retrieved At"],
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

    def market_bars(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
        limit: int = 500,
    ) -> dict:
        symbol = symbol.strip().upper()
        if not symbol:
            raise ValueError("Symbol is required")
        if start is not None and end is not None and start > end:
            raise ValueError("Start date cannot be after end date")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 5_000
        ):
            raise ValueError("Bar limit must be between 1 and 5000")
        history = self.market_repository.load(
            [symbol],
            start=start,
            end=end,
            columns=[
                "Date",
                "Symbol",
                "Open",
                "High",
                "Low",
                "Close",
                "Adjusted Close",
                "Volume",
                "Provider",
                "Retrieved At",
            ],
        )
        if history.is_empty():
            raise RuntimeError(f"Stored market data unavailable for {symbol}")
        total = history.height
        history = history.tail(limit)
        return {
            "symbol": symbol,
            "bars": _frame_records(history.drop("Symbol")),
            "page": {
                "limit": limit,
                "returned": history.height,
                "total": total,
                "truncated": total > history.height,
            },
        }

    def data_status(self, symbols: list[str] | None = None) -> dict:
        normalized = None
        if symbols is not None:
            normalized = list(
                dict.fromkeys(
                    symbol.strip().upper()
                    for symbol in symbols
                    if isinstance(symbol, str) and symbol.strip()
                )
            )
            if len(normalized) > MAX_SCREENER_SYMBOLS:
                raise ValueError(
                    f"Data status accepts at most {MAX_SCREENER_SYMBOLS} symbols"
                )
        coverage = self.market_repository.coverage(normalized)
        records = {row["Symbol"]: row for row in coverage.to_dicts()}
        ordered_symbols = normalized or coverage.get_column("Symbol").to_list()
        rows = []
        for symbol in ordered_symbols:
            row = records.get(symbol)
            rows.append(
                {
                    "symbol": symbol,
                    "firstSession": (
                        row["First Session"].isoformat() if row else None
                    ),
                    "lastSession": row["Last Session"].isoformat() if row else None,
                    "sessionCount": row["Session Count"] if row else 0,
                    "completeOhlcvSessionCount": (
                        row["Complete OHLCV Count"] if row else 0
                    ),
                    "adjustedCloseSessionCount": (
                        row["Adjusted Close Count"] if row else 0
                    ),
                    "retrievedAt": row["Retrieved At"] if row else None,
                }
            )
        available = [row for row in rows if row["sessionCount"]]
        return {
            "storage": "sqlite",
            "provider": "yahoo",
            "summary": {
                "securityCount": len(available),
                "barCount": sum(row["sessionCount"] for row in available),
                "completeOhlcvBarCount": sum(
                    row["completeOhlcvSessionCount"] for row in available
                ),
                "adjustedCloseBarCount": sum(
                    row["adjustedCloseSessionCount"] for row in available
                ),
                "firstSession": min(
                    (row["firstSession"] for row in available), default=None
                ),
                "lastSession": min(
                    (row["lastSession"] for row in available), default=None
                ),
                "retrievedAt": min(
                    (row["retrievedAt"] for row in available), default=None
                ),
            },
            "coverage": rows,
        }

    def holdings(
        self, refresh: bool = False, fetch_missing: bool = False
    ) -> dict:
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
            fetch_missing,
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
        return self._writer().add_position(
            symbol,
            shares,
            cost_basis,
            account,
            asset_class,
            sector,
            acquired,
        )

    def remove_holding(self, holding_id: str) -> bool:
        return self._writer().remove_position(holding_id)

    def market_analysis(
        self,
        symbol: str,
        windows: list[int],
        price: str,
        refresh: bool = False,
        fetch_missing: bool = False,
    ) -> dict:
        if price not in {"close", "adjusted"}:
            raise ValueError("Price must be close or adjusted")
        price_column = "Adjusted Close" if price == "adjusted" else "Close"
        analysis = analyze_symbols(
            [symbol],
            windows,
            price_column,
            self.market_repository,
            refresh,
            fetch_missing=fetch_missing,
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
        unavailable_windows = [
            window for window in windows if window > analysis.height
        ]
        warnings = []
        if unavailable_windows:
            warnings.append(
                {
                    "code": "partial_data",
                    "message": "Some requested technical indicators require more stored history",
                    "symbols": [symbol.upper()],
                    "details": [
                        {
                            "reason": "insufficient_history",
                            "requestedWindow": window,
                            "availableObservations": analysis.height,
                            "firstSession": rows[0]["date"],
                            "lastSession": rows[-1]["date"],
                            "fields": [
                                f"movingAverages.{window}",
                                f"rollingHighs.{window}",
                                f"rollingLows.{window}",
                                f"volumeAverages.{window}",
                                f"relativeVolumes.{window}",
                            ],
                        }
                        for window in unavailable_windows
                    ],
                }
            )
        return {
            "symbol": symbol.upper(),
            "priceBasis": price,
            "windows": windows,
            "rows": rows,
            "warnings": warnings,
        }

    def security_search(
        self,
        query: str,
        limit: int = 10,
        remote: bool = False,
    ) -> dict:
        local = self.market_repository.search_securities(query, limit)
        result = {
            "local": [
                {
                    "symbol": row["Symbol"],
                    "name": row["Company"] or row["Symbol"],
                }
                for row in local.to_dicts()
            ],
            "remote": [],
        }
        if remote:
            result["remote"] = self.research_service.search(query, limit)
        return result

    def symbol_risk(
        self,
        symbols: list[str],
        period: str = "1y",
        benchmark: str = "SPY",
        refresh: bool = False,
        fetch_missing: bool = False,
    ) -> dict:
        result = analyze_symbol_risk(
            symbols,
            period,
            benchmark,
            self.market_repository,
            refresh,
            fetch_missing,
        )
        return {
            "period": period.lower(),
            "benchmark": benchmark.upper(),
            "metrics": _frame_records(result["metrics"]),
            "correlations": _frame_records(result["correlations"]),
            "unavailableSymbols": [
                symbol
                for symbol in dict.fromkeys(
                    value.strip().upper() for value in symbols if value.strip()
                )
                if symbol
                not in set(result["metrics"].get_column("Symbol").to_list())
                and symbol != benchmark.strip().upper()
            ],
        }

    def portfolio_risk(
        self,
        period: str = "1y",
        benchmark: str = "SPY",
        refresh: bool = False,
        fetch_missing: bool = False,
    ) -> dict:
        result = analyze_portfolio_risk(
            self.repository.positions_frame(),
            period,
            benchmark,
            self.market_repository,
            refresh,
            fetch_missing,
        )
        return {
            "period": period.lower(),
            "benchmark": benchmark.upper(),
            "metrics": _frame_records(result["metrics"]),
            "history": _frame_records(result["history"]),
            "returnContributions": _frame_records(
                result["return_contributions"]
            ),
            "riskContributions": _frame_records(
                result["risk_contributions"]
            ),
            "correlations": _frame_records(result["correlations"]),
            "unavailableSymbols": result["unavailable_symbols"],
        }

    def screener(
        self,
        symbols: list[str] | None = None,
        period: str = "1y",
        benchmark: str = "SPY",
        refresh: bool = False,
        fetch_missing: bool = False,
    ) -> dict:
        if symbols is None:
            positions = self.repository.positions_frame()
            position_symbols = (
                positions.get_column("Symbol").to_list()
                if not positions.is_empty()
                else []
            )
            symbols = position_symbols
        symbols = list(
            dict.fromkeys(
                symbol.strip().upper()
                for symbol in symbols
                if isinstance(symbol, str) and symbol.strip()
            )
        )
        if not symbols:
            raise ValueError("Screener requires at least one symbol")
        if len(symbols) > MAX_SCREENER_SYMBOLS:
            raise ValueError(
                f"Screener accepts at most {MAX_SCREENER_SYMBOLS} symbols"
            )
        screen = screen_symbols_eod(
            symbols,
            period,
            benchmark,
            self.market_repository,
            refresh,
            fetch_missing,
        )
        return {
            "period": period.lower(),
            "benchmark": benchmark.upper(),
            "rows": _frame_records(screen),
            "unavailableSymbols": [
                symbol
                for symbol in symbols
                if symbol not in set(screen.get_column("Symbol").to_list())
                and symbol != benchmark.strip().upper()
            ],
        }

    def research_profile(self, symbol: str) -> dict:
        return self.research_service.profile(symbol)

    def research_analyst(self, symbol: str) -> dict:
        return self.research_service.analyst(symbol)

    def research_earnings(self, symbol: str, limit: int = 12) -> dict:
        return self.research_service.earnings(symbol, limit)

    def research_options(
        self,
        symbol: str,
        expiration: str | None = None,
        limit: int = 1_000,
    ) -> dict:
        return self.research_service.options(symbol, expiration, limit)

    def research_news(self, symbol: str, limit: int = 20) -> list[dict]:
        return self.research_service.news(symbol, limit)

    def research_history(
        self,
        symbol: str,
        period: str = "1y",
        interval: str = "1d",
        limit: int = 500,
    ) -> list[dict]:
        return self.research_service.history(symbol, period, interval, limit)

    def research_intraday(
        self,
        symbol: str,
        period: str = "5d",
        interval: str = "5m",
        limit: int = 500,
    ) -> list[dict]:
        return self.research_service.intraday(symbol, period, interval, limit)

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


def _frame_records(frame: pl.DataFrame) -> list[dict]:
    return [
        {
            _response_key(key): _response_value(value)
            for key, value in row.items()
        }
        for row in frame.to_dicts()
    ]


def _response_key(value: str) -> str:
    parts = re.findall(r"[A-Za-z0-9]+", value)
    if not parts:
        return value
    return parts[0].lower() + "".join(part.title() for part in parts[1:])


def _response_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value
