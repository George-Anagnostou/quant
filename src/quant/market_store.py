from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl

from quant.database import (
    DEFAULT_DATABASE_PATH,
    database_connection,
    initialize_database,
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


class MarketDataRepository:
    def __init__(self, path: Path = DEFAULT_DATABASE_PATH) -> None:
        self.path = path

    def initialize(self) -> None:
        initialize_database(self.path)

    def save(
        self,
        market_data: pl.DataFrame,
        provider: str = DEFAULT_PROVIDER,
        universe: str | None = None,
        observed_on: date | None = None,
    ) -> int:
        self.initialize()
        if market_data.is_empty():
            return 0
        _require_columns(market_data, {"Date", "Symbol"})
        if (
            "Close" not in market_data.columns
            and "Last Price" not in market_data.columns
        ):
            raise ValueError("Market data requires Close or Last Price")

        retrieved_at = datetime.now(timezone.utc).isoformat()
        company = (
            pl.col("Company")
            if "Company" in market_data.columns
            else pl.lit(None).alias("Company")
        )
        symbols = market_data.select("Symbol", company).unique(
            subset="Symbol", keep="last", maintain_order=True
        )

        with database_connection(self.path) as connection:
            security_ids = self._upsert_securities(connection, symbols, provider)
            rows = []
            for row in market_data.to_dicts():
                close = row.get("Close", row.get("Last Price"))
                if close is None:
                    raise ValueError(f"Missing close price for {row['Symbol']}")
                rows.append(
                    (
                        security_ids[row["Symbol"]],
                        _date_string(row["Date"]),
                        provider,
                        row.get("Open"),
                        row.get("High"),
                        row.get("Low"),
                        close,
                        row.get("Adjusted Close"),
                        row.get("Volume"),
                        retrieved_at,
                    )
                )
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
            if universe is not None:
                self._save_universe(
                    connection,
                    universe,
                    security_ids,
                    observed_on or date.today(),
                )
        return len(rows)

    def load(
        self,
        symbols: list[str] | None = None,
        start: date | None = None,
        end: date | None = None,
        provider: str = DEFAULT_PROVIDER,
        columns: list[str] | None = None,
    ) -> pl.DataFrame:
        self.initialize()
        selected = columns or list(MARKET_DATA_SCHEMA)
        unknown = set(selected).difference(MARKET_DATA_SCHEMA)
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"Unknown market data columns: {names}")
        if symbols is not None and not symbols:
            return _empty_market_data(selected)

        conditions = ["b.provider = ?"]
        parameters: list[object] = [provider]
        if symbols is not None:
            placeholders = ", ".join("?" for _ in symbols)
            conditions.append(f"s.symbol IN ({placeholders})")
            parameters.extend(symbol.upper() for symbol in symbols)
        if start is not None:
            conditions.append("b.session_date >= ?")
            parameters.append(start.isoformat())
        if end is not None:
            conditions.append("b.session_date <= ?")
            parameters.append(end.isoformat())

        with database_connection(self.path) as connection:
            records = connection.execute(
                f"""
                SELECT b.session_date, s.symbol, s.company, b.open, b.high,
                       b.low, b.close, b.adjusted_close, b.volume
                FROM daily_bars AS b
                JOIN securities AS s ON s.id = b.security_id
                WHERE {' AND '.join(conditions)}
                ORDER BY b.session_date, s.symbol
                """,
                parameters,
            ).fetchall()
        if not records:
            return _empty_market_data(selected)

        frame = pl.DataFrame(
            {
                "Date": [date.fromisoformat(row["session_date"]) for row in records],
                "Symbol": [row["symbol"] for row in records],
                "Company": [row["company"] for row in records],
                "Open": [row["open"] for row in records],
                "High": [row["high"] for row in records],
                "Low": [row["low"] for row in records],
                "Close": [row["close"] for row in records],
                "Adjusted Close": [row["adjusted_close"] for row in records],
                "Last Price": [row["close"] for row in records],
                "Volume": [row["volume"] for row in records],
            },
            schema=MARKET_DATA_SCHEMA,
        )
        return frame.select(selected)

    def save_universe(
        self,
        universe: str,
        constituents: pl.DataFrame,
        observed_on: date | None = None,
    ) -> None:
        self.initialize()
        _require_columns(constituents, {"Symbol"})
        company = (
            pl.col("Company")
            if "Company" in constituents.columns
            else pl.lit(None).alias("Company")
        )
        symbols = constituents.select("Symbol", company).unique(
            subset="Symbol", keep="last", maintain_order=True
        )
        with database_connection(self.path) as connection:
            security_ids = self._upsert_securities(connection, symbols)
            self._save_universe(
                connection,
                universe,
                security_ids,
                observed_on or date.today(),
            )

    def list_universe_symbols(self, universe: str) -> list[str]:
        self.initialize()
        with database_connection(self.path) as connection:
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
        provider: str | None = None,
    ) -> dict[str, str]:
        security_ids = {}
        for row in symbols.to_dicts():
            symbol = row["Symbol"].strip().upper()
            existing = connection.execute(
                "SELECT id FROM securities WHERE symbol = ?", (symbol,)
            ).fetchone()
            security_id = existing["id"] if existing else str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO securities(id, symbol, company)
                VALUES (?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    company = COALESCE(excluded.company, securities.company),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (security_id, symbol, row["Company"]),
            )
            if provider is not None:
                connection.execute(
                    """
                    INSERT INTO provider_symbols(
                        provider, provider_symbol, security_id
                    ) VALUES (?, ?, ?)
                    ON CONFLICT(provider, provider_symbol) DO UPDATE SET
                        security_id = excluded.security_id,
                        active = 1
                    """,
                    (provider, symbol, security_id),
                )
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
            "INSERT OR IGNORE INTO universes(id, name) VALUES (?, ?)",
            (universe, universe),
        )
        connection.executemany(
            """
            INSERT OR IGNORE INTO universe_memberships(
                universe_id, security_id, observed_on, sort_order
            ) VALUES (?, ?, ?, ?)
            """,
            [
                (universe, security_id, observed_on.isoformat(), order)
                for order, security_id in enumerate(security_ids.values())
            ],
        )


def _empty_market_data(columns: list[str]) -> pl.DataFrame:
    schema = {column: MARKET_DATA_SCHEMA[column] for column in columns}
    return pl.DataFrame(schema=schema)


def _require_columns(frame: pl.DataFrame, required: set[str]) -> None:
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Missing market data columns: {', '.join(sorted(missing))}")


def _date_string(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(value).isoformat()
