from __future__ import annotations

import math
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl

from quant.database import (
    DEFAULT_DATABASE_PATH,
    database_connection,
    initialize_database,
    is_database_initialized,
)


DEFAULT_PROVIDER = "yahoo"
MARKET_DATA_SCHEMA = {
    "Date": pl.Date,
    "Symbol": pl.String,
    "Company": pl.String,
    "Open": pl.Float64,
    "High": pl.Float64,
    "Low": pl.Float64,
    "Close": pl.Float64,
    "Adjusted Close": pl.Float64,
    "Last Price": pl.Float64,
    "Volume": pl.Int64,
}
MARKET_STORAGE_SCHEMA = {
    **MARKET_DATA_SCHEMA,
    "Provider": pl.String,
    "Retrieved At": pl.String,
}


class MarketDataRepository:
    def __init__(
        self, path: Path = DEFAULT_DATABASE_PATH, *, read_only: bool = False
    ) -> None:
        self.path = path
        self.read_only = read_only

    def initialize(self) -> None:
        if self.read_only:
            if not is_database_initialized(self.path):
                raise RuntimeError("Market database is not initialized")
        else:
            initialize_database(self.path)

    def _connect(self):
        return database_connection(self.path, read_only=self.read_only)

    def save(
        self,
        market_data: pl.DataFrame,
        provider: str = DEFAULT_PROVIDER,
    ) -> int:
        self.initialize()
        if market_data.is_empty():
            return 0
        provider = provider.strip().lower()
        if not provider:
            raise ValueError("Provider is required")
        _require_columns(market_data, {"Date", "Symbol"})
        if (
            "Close" not in market_data.columns
            and "Last Price" not in market_data.columns
        ):
            raise ValueError("Market data requires Close or Last Price")

        market_data = _normalize_symbols(market_data)
        duplicate_keys = market_data.group_by("Date", "Symbol").len().filter(
            pl.col("len") > 1
        )
        if not duplicate_keys.is_empty():
            raise ValueError("Market data contains duplicate symbol-date rows")

        retrieved_at = datetime.now(timezone.utc).isoformat()
        company = (
            pl.col("Company")
            if "Company" in market_data.columns
            else pl.lit(None).alias("Company")
        )
        symbols = market_data.select("Symbol", company).unique(
            subset="Symbol", keep="last", maintain_order=True
        )

        bars = []
        for row in market_data.to_dicts():
            close = row.get("Close")
            if close is None:
                close = row.get("Last Price")
            if close is None:
                raise ValueError(f"Missing close price for {row['Symbol']}")
            open_price = _positive_optional(row.get("Open"), "open")
            high = _positive_optional(row.get("High"), "high")
            low = _positive_optional(row.get("Low"), "low")
            close = _positive(close, "close")
            adjusted_close = _positive_optional(
                row.get("Adjusted Close"), "adjusted close"
            )
            volume = _volume(row.get("Volume"))
            _validate_ohlc(open_price, high, low, close)
            bars.append(
                (
                    row["Symbol"],
                    _date_string(row["Date"]),
                    provider,
                    open_price,
                    high,
                    low,
                    close,
                    adjusted_close,
                    volume,
                    retrieved_at,
                )
            )

        with self._connect() as connection:
            security_ids = self._upsert_securities(connection, symbols)
            rows = [(security_ids[bar[0]], *bar[1:]) for bar in bars]
            connection.executemany(
                """
                INSERT INTO daily_bars(
                    security_id, session_date, provider, open, high, low,
                    close, adjusted_close, volume, retrieved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(security_id, session_date, provider) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    adjusted_close = excluded.adjusted_close,
                    volume = excluded.volume,
                    retrieved_at = excluded.retrieved_at
                """,
                rows,
            )
        return len(rows)

    def load(
        self,
        symbols: list[str] | None = None,
        start: date | None = None,
        end: date | None = None,
        provider: str = DEFAULT_PROVIDER,
        columns: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        newest: bool = False,
    ) -> pl.DataFrame:
        self.initialize()
        selected = columns or list(MARKET_DATA_SCHEMA)
        unknown = set(selected).difference(MARKET_STORAGE_SCHEMA)
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"Unknown market data columns: {names}")
        if symbols is not None and not symbols:
            return _empty_market_data(selected)

        conditions = ["b.provider = ?"]
        parameters: list[object] = [provider.strip().lower()]
        if symbols is not None:
            symbols = list(
                dict.fromkeys(
                    symbol.strip().upper() for symbol in symbols if symbol.strip()
                )
            )
            if not symbols:
                return _empty_market_data(selected)
            placeholders = ", ".join("?" for _ in symbols)
            conditions.append(f"s.symbol IN ({placeholders})")
            parameters.extend(symbols)
        if start is not None:
            conditions.append("b.session_date >= ?")
            parameters.append(start.isoformat())
        if end is not None:
            conditions.append("b.session_date <= ?")
            parameters.append(end.isoformat())

        if limit is not None and (isinstance(limit, bool) or not 1 <= limit <= 100_000):
            raise ValueError("Market row limit must be 1-100000")
        if offset < 0:
            raise ValueError("Offset must be nonnegative")
        sql_columns = {
            "Date": "b.session_date", "Symbol": "s.symbol", "Company": "s.company",
            "Open": "b.open", "High": "b.high", "Low": "b.low", "Close": "b.close",
            "Adjusted Close": "b.adjusted_close", "Last Price": "b.close", "Volume": "b.volume",
            "Provider": "b.provider", "Retrieved At": "b.retrieved_at",
        }
        projection = ", ".join(f'{sql_columns[name]} AS "{name}"' for name in selected)
        order = "DESC" if newest else "ASC"
        pagination = " LIMIT ? OFFSET ?" if limit is not None else ""
        if limit is not None:
            parameters.extend([limit, offset])
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {projection} FROM daily_bars b JOIN securities s ON s.id=b.security_id "
                f"WHERE {' AND '.join(conditions)} ORDER BY b.session_date {order},s.symbol"+pagination,
                parameters).fetchall()
        if not rows:
            return _empty_market_data(selected)
        values = [dict(row) for row in rows]
        if "Date" in selected:
            for row in values:
                row["Date"] = date.fromisoformat(row["Date"])
        frame = pl.DataFrame(values, schema={name: MARKET_STORAGE_SCHEMA[name] for name in selected})
        if newest and "Date" in selected:
            frame = frame.sort([name for name in ("Date", "Symbol") if name in selected])
        return frame

    def coverage(
        self,
        symbols: list[str] | None = None,
        provider: str = DEFAULT_PROVIDER,
    ) -> pl.DataFrame:
        self.initialize()
        provider = provider.strip().lower()
        if not provider:
            raise ValueError("Provider is required")
        conditions = ["b.provider = ?"]
        parameters: list[object] = [provider]
        if symbols is not None:
            symbols = list(
                dict.fromkeys(
                    symbol.strip().upper() for symbol in symbols if symbol.strip()
                )
            )
            if not symbols:
                return _empty_coverage()
            placeholders = ", ".join("?" for _ in symbols)
            conditions.append(f"s.symbol IN ({placeholders})")
            parameters.extend(symbols)
        with self._connect() as connection:
            records = connection.execute(
                f"""
                SELECT s.symbol, MIN(b.session_date) AS first_session,
                       MAX(b.session_date) AS last_session,
                       COUNT(*) AS session_count,
                       SUM(CASE WHEN b.open IS NOT NULL
                                     AND b.high IS NOT NULL
                                     AND b.low IS NOT NULL
                                     AND b.volume IS NOT NULL
                                THEN 1 ELSE 0 END) AS complete_ohlcv_count,
                       SUM(CASE WHEN b.adjusted_close IS NOT NULL
                                THEN 1 ELSE 0 END) AS adjusted_close_count,
                       MAX(b.retrieved_at) AS retrieved_at
                FROM daily_bars AS b
                JOIN securities AS s ON s.id = b.security_id
                WHERE {' AND '.join(conditions)}
                GROUP BY s.symbol
                ORDER BY s.symbol
                """,
                parameters,
            ).fetchall()
        if not records:
            return _empty_coverage()
        return pl.DataFrame(
            {
                "Symbol": [row["symbol"] for row in records],
                "First Session": [
                    date.fromisoformat(row["first_session"]) for row in records
                ],
                "Last Session": [
                    date.fromisoformat(row["last_session"]) for row in records
                ],
                "Session Count": [row["session_count"] for row in records],
                "Complete OHLCV Count": [
                    row["complete_ohlcv_count"] for row in records
                ],
                "Adjusted Close Count": [
                    row["adjusted_close_count"] for row in records
                ],
                "Retrieved At": [row["retrieved_at"] for row in records],
            },
            schema=_coverage_schema(),
        )

    def search_securities(
        self,
        query: str,
        limit: int = 10,
    ) -> pl.DataFrame:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Security search query must not be blank")
        if len(query.strip()) > 100:
            raise ValueError("Security search query cannot exceed 100 characters")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 50
        ):
            raise ValueError("Security search limit must be between 1 and 50")

        self.initialize()
        query = query.strip().lower()
        with self._connect() as connection:
            records = connection.execute(
                """
                SELECT symbol, company
                FROM securities
                WHERE instr(lower(symbol), ?) > 0
                   OR instr(lower(COALESCE(company, '')), ?) > 0
                ORDER BY
                    CASE
                        WHEN lower(symbol) = ? THEN 0
                        WHEN instr(lower(symbol), ?) = 1
                          OR instr(lower(COALESCE(company, '')), ?) = 1
                        THEN 1
                        ELSE 2
                    END,
                    symbol COLLATE NOCASE,
                    symbol
                LIMIT ?
                """,
                (query, query, query, query, query, limit),
            ).fetchall()
        return pl.DataFrame(
            {
                "Symbol": [row["symbol"] for row in records],
                "Company": [row["company"] for row in records],
            },
            schema={"Symbol": pl.String, "Company": pl.String},
        )

    def save_universe(
        self,
        universe: str,
        constituents: pl.DataFrame,
        observed_on: date | None = None,
    ) -> None:
        self.initialize()
        _require_columns(constituents, {"Symbol"})
        universe = universe.strip()
        if not universe:
            raise ValueError("Universe is required")
        constituents = _normalize_symbols(constituents)
        company = (
            pl.col("Company")
            if "Company" in constituents.columns
            else pl.lit(None).alias("Company")
        )
        symbols = constituents.select("Symbol", company).unique(
            subset="Symbol", keep="last", maintain_order=True
        )
        with self._connect() as connection:
            security_ids = self._upsert_securities(connection, symbols)
            self._save_universe(
                connection,
                universe,
                security_ids,
                observed_on or date.today(),
            )

    def list_universe_symbols(self, universe: str) -> list[str]:
        self.initialize()
        with self._connect() as connection:
            observed_on = connection.execute(
                """
                SELECT MAX(observed_on)
                FROM universe_memberships
                WHERE universe_id = ?
                """,
                (universe,),
            ).fetchone()[0]
            if observed_on is None:
                return []
            rows = connection.execute(
                """
                SELECT s.symbol
                FROM universe_memberships AS membership
                JOIN securities AS s ON s.id = membership.security_id
                WHERE membership.universe_id = ? AND membership.observed_on = ?
                ORDER BY membership.sort_order
                """,
                (universe, observed_on),
            ).fetchall()
        return [row["symbol"] for row in rows]

    @staticmethod
    def _upsert_securities(
        connection,
        symbols: pl.DataFrame,
    ) -> dict[str, str]:
        security_ids = {}
        for row in symbols.to_dicts():
            symbol = row["Symbol"].strip().upper()
            security_id = connection.execute(
                """
                INSERT INTO securities(id, symbol, company)
                VALUES (?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    company = COALESCE(excluded.company, securities.company)
                RETURNING id
                """,
                (str(uuid.uuid4()), symbol, row["Company"]),
            ).fetchone()["id"]
            security_ids[row["Symbol"]] = security_id
        return security_ids

    @staticmethod
    def _save_universe(
        connection,
        universe: str,
        security_ids: dict[str, str],
        observed_on: date,
    ) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO universes(id) VALUES (?)",
            (universe,),
        )
        connection.execute(
            """
            DELETE FROM universe_memberships
            WHERE universe_id = ? AND observed_on = ?
            """,
            (universe, observed_on.isoformat()),
        )
        connection.executemany(
            """
            INSERT INTO universe_memberships(
                universe_id, security_id, observed_on, sort_order
            ) VALUES (?, ?, ?, ?)
            """,
            [
                (universe, security_id, observed_on.isoformat(), order)
                for order, security_id in enumerate(security_ids.values())
            ],
        )


def _empty_market_data(columns: list[str]) -> pl.DataFrame:
    schema = {column: MARKET_STORAGE_SCHEMA[column] for column in columns}
    return pl.DataFrame(schema=schema)


def _coverage_schema() -> dict[str, pl.DataType]:
    return {
        "Symbol": pl.String,
        "First Session": pl.Date,
        "Last Session": pl.Date,
        "Session Count": pl.Int64,
        "Complete OHLCV Count": pl.Int64,
        "Adjusted Close Count": pl.Int64,
        "Retrieved At": pl.String,
    }


def _empty_coverage() -> pl.DataFrame:
    return pl.DataFrame(schema=_coverage_schema())


def _require_columns(frame: pl.DataFrame, required: set[str]) -> None:
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Missing market data columns: {', '.join(sorted(missing))}")


def _normalize_symbols(frame: pl.DataFrame) -> pl.DataFrame:
    normalized = frame.with_columns(
        pl.col("Symbol").str.strip_chars().str.to_uppercase()
    )
    invalid = normalized.filter(
        pl.col("Symbol").is_null()
        | (pl.col("Symbol").str.len_chars() == 0)
        | (pl.col("Symbol").str.len_chars() > 32)
    )
    if not invalid.is_empty():
        raise ValueError("Symbols must contain between 1 and 32 characters")
    return normalized


def _positive(value: object, label: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"Market data {label} must be finite and positive")
    return number


def _positive_optional(value: object | None, label: str) -> float | None:
    return None if value is None else _positive(value, label)


def _volume(value: object | None) -> int | None:
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        raise ValueError("Market data volume must be a non-negative integer")
    return int(number)


def _validate_ohlc(
    open_price: float | None,
    high: float | None,
    low: float | None,
    close: float,
) -> None:
    if high is not None and low is not None and high < low:
        raise ValueError("Market data high cannot be below low")
    if high is not None and (
        (open_price is not None and high < open_price) or high < close
    ):
        raise ValueError("Market data high cannot be below open or close")
    if low is not None and (
        (open_price is not None and low > open_price) or low > close
    ):
        raise ValueError("Market data low cannot be above open or close")


def _date_string(value: date | datetime | str) -> str:
    try:
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid market data date: {value}") from error
