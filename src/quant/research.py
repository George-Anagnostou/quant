from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Hashable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import math
import re
from threading import Event, Lock
import time
from typing import Protocol, TypeVar

import yfinance as yf


JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

SEARCH_LIMIT = 25
NEWS_LIMIT = 100
EARNINGS_LIMIT = 100
CANDLE_LIMIT = 5_000
OPTION_LIMIT = 5_000
MAX_QUERY_LENGTH = 100

HISTORY_PERIODS = frozenset(
    {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"}
)
DAILY_INTERVALS = frozenset({"1d", "5d", "1wk", "1mo", "3mo"})
INTRADAY_PERIODS = frozenset({"1d", "5d", "1mo", "3mo"})
INTRADAY_INTERVALS = frozenset(
    {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h"}
)

CACHE_TTLS = {
    "profile": 24 * 60 * 60,
    "analyst": 60 * 60,
    "earnings": 60 * 60,
    "search": 5 * 60,
    "news": 5 * 60,
    "options": 60,
    "history": 30,
    "intraday": 30,
}

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9^][A-Z0-9^=_-]{0,19}$")


class ResearchProvider(Protocol):
    def search(self, query: str, limit: int = 10) -> list[dict[str, JsonValue]]: ...

    def profile(self, symbol: str) -> dict[str, JsonValue]: ...

    def analyst(self, symbol: str) -> dict[str, JsonValue]: ...

    def earnings(
        self, symbol: str, limit: int = 12
    ) -> dict[str, JsonValue]: ...

    def options(
        self,
        symbol: str,
        expiration: str | None = None,
        limit: int = 1_000,
    ) -> dict[str, JsonValue]: ...

    def news(
        self, symbol: str, limit: int = 20
    ) -> list[dict[str, JsonValue]]: ...

    def history(
        self,
        symbol: str,
        period: str = "1y",
        interval: str = "1d",
        limit: int = 500,
    ) -> list[dict[str, JsonValue]]: ...

    def intraday(
        self,
        symbol: str,
        period: str = "5d",
        interval: str = "5m",
        limit: int = 500,
    ) -> list[dict[str, JsonValue]]: ...


class YahooResearchProvider:
    """A yfinance dependency boundary that returns only JSON-compatible values."""

    def __init__(self, client: object = yf) -> None:
        self._client = client

    def search(self, query: str, limit: int = 10) -> list[dict[str, JsonValue]]:
        query = _normalize_query(query)
        limit = _validate_limit(limit, SEARCH_LIMIT)
        result = self._request(
            "Yahoo search request failed",
            lambda: self._client.Search(query, max_results=limit).quotes,
        )
        return _json_records(result, limit)

    def profile(self, symbol: str) -> dict[str, JsonValue]:
        ticker = self._ticker(symbol)
        result = self._request(
            "Yahoo profile request failed",
            lambda: ticker.info,
        )
        return _json_dict(result)

    def analyst(self, symbol: str) -> dict[str, JsonValue]:
        ticker = self._ticker(symbol)
        result = self._request(
            "Yahoo analyst request failed",
            lambda: {
                "ratings": _frame_records(ticker.recommendations),
                "summary": _frame_records(ticker.recommendations_summary),
                "upgrades_downgrades": _frame_records(
                    ticker.upgrades_downgrades
                ),
            },
        )
        return _json_dict(result)

    def earnings(
        self, symbol: str, limit: int = 12
    ) -> dict[str, JsonValue]:
        limit = _validate_limit(limit, EARNINGS_LIMIT)
        ticker = self._ticker(symbol)
        result = self._request(
            "Yahoo earnings request failed",
            lambda: {
                "calendar": _clean_json(ticker.calendar),
                "history": _frame_records(
                    ticker.get_earnings_dates(limit=limit), limit
                ),
            },
        )
        return _json_dict(result)

    def options(
        self,
        symbol: str,
        expiration: str | None = None,
        limit: int = 1_000,
    ) -> dict[str, JsonValue]:
        normalized_expiration = _normalize_expiration(expiration)
        limit = _validate_limit(limit, OPTION_LIMIT)
        ticker = self._ticker(symbol)
        expirations_value = self._request(
            "Yahoo options request failed",
            lambda: ticker.options,
        )
        expirations = [str(value) for value in expirations_value]
        if not expirations:
            raise RuntimeError(f"No option expirations returned for {symbol}")
        selected = normalized_expiration or expirations[0]
        if selected not in expirations:
            raise ValueError(f"option expiration is not available: {selected}")
        chain = self._request(
            "Yahoo options request failed",
            lambda: ticker.option_chain(selected),
        )
        return {
            "expiration": selected,
            "expirations": expirations,
            "calls": _frame_records(chain.calls, limit),
            "puts": _frame_records(chain.puts, limit),
            "underlying": _clean_json(getattr(chain, "underlying", {})),
        }

    def news(
        self, symbol: str, limit: int = 20
    ) -> list[dict[str, JsonValue]]:
        limit = _validate_limit(limit, NEWS_LIMIT)
        ticker = self._ticker(symbol)
        result = self._request(
            "Yahoo news request failed",
            lambda: ticker.get_news(count=limit),
        )
        return _json_records(result, limit)

    def history(
        self,
        symbol: str,
        period: str = "1y",
        interval: str = "1d",
        limit: int = 500,
    ) -> list[dict[str, JsonValue]]:
        period = _validate_choice("period", period, HISTORY_PERIODS)
        interval = _validate_choice("interval", interval, DAILY_INTERVALS)
        limit = _validate_limit(limit, CANDLE_LIMIT)
        return self._history(symbol, period, interval, limit)

    def intraday(
        self,
        symbol: str,
        period: str = "5d",
        interval: str = "5m",
        limit: int = 500,
    ) -> list[dict[str, JsonValue]]:
        period = _validate_choice("period", period, INTRADAY_PERIODS)
        interval = _validate_choice("interval", interval, INTRADAY_INTERVALS)
        limit = _validate_limit(limit, CANDLE_LIMIT)
        return self._history(symbol, period, interval, limit)

    def _ticker(self, symbol: str):
        symbol = _normalize_symbol(symbol)
        return self._request(
            "Yahoo ticker request failed",
            lambda: self._client.Ticker(symbol),
        )

    def _history(
        self, symbol: str, period: str, interval: str, limit: int
    ) -> list[dict[str, JsonValue]]:
        ticker = self._ticker(symbol)
        result = self._request(
            "Yahoo history request failed",
            lambda: ticker.history(
                period=period,
                interval=interval,
                auto_adjust=False,
                actions=False,
            ),
        )
        return _frame_records(result, limit, from_end=True)

    @staticmethod
    def _request(message: str, operation: Callable[[], object]) -> object:
        try:
            return operation()
        except Exception as error:
            raise RuntimeError(message) from error


T = TypeVar("T")


@dataclass
class _CacheEntry:
    value: object
    expires_at: float


@dataclass
class _Flight:
    event: Event
    result: object | None = None
    error: BaseException | None = None


class BoundedTTLCache:
    """Thread-safe LRU TTL cache with per-key single-flight loading."""

    def __init__(
        self,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int):
            raise ValueError("max_entries must be an integer")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[Hashable, _CacheEntry] = OrderedDict()
        self._flights: dict[Hashable, _Flight] = {}
        self._lock = Lock()

    def get_or_load(
        self,
        key: Hashable,
        ttl: float,
        loader: Callable[[], T],
    ) -> T:
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
            raise ValueError("ttl must be a positive finite number")
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("ttl must be a positive finite number")

        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and self._clock() < entry.expires_at:
                self._entries.move_to_end(key)
                return deepcopy(entry.value)  # type: ignore[return-value]
            stale = entry
            flight = self._flights.get(key)
            if flight is None:
                flight = _Flight(Event())
                self._flights[key] = flight
                owns_flight = True
            else:
                owns_flight = False

        if not owns_flight:
            flight.event.wait()
            if flight.error is not None:
                raise flight.error
            return deepcopy(flight.result)  # type: ignore[return-value]

        try:
            try:
                value = loader()
            except RuntimeError:
                if stale is None:
                    raise
                value = stale.value
                with self._lock:
                    if key in self._entries:
                        self._entries.move_to_end(key)
            else:
                cached_value = deepcopy(value)
                with self._lock:
                    self._entries[key] = _CacheEntry(
                        cached_value,
                        self._clock() + ttl,
                    )
                    self._entries.move_to_end(key)
                    while len(self._entries) > self._max_entries:
                        self._entries.popitem(last=False)
                value = cached_value
            flight.result = value
            return deepcopy(value)
        except BaseException as error:
            flight.error = error
            raise
        finally:
            with self._lock:
                self._flights.pop(key, None)
                flight.event.set()

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class CachedResearchService:
    def __init__(
        self,
        provider: ResearchProvider,
        *,
        cache: BoundedTTLCache | None = None,
        max_cache_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = provider
        self._cache = (
            cache
            if cache is not None
            else BoundedTTLCache(max_cache_entries, clock)
        )

    def search(self, query: str, limit: int = 10) -> list[dict[str, JsonValue]]:
        query = _normalize_query(query)
        limit = _validate_limit(limit, SEARCH_LIMIT)
        return self._cached(
            "search", (query.casefold(), limit), lambda: self._provider.search(query, limit)
        )

    def profile(self, symbol: str) -> dict[str, JsonValue]:
        symbol = _normalize_symbol(symbol)
        return self._cached(
            "profile", (symbol,), lambda: self._provider.profile(symbol)
        )

    def analyst(self, symbol: str) -> dict[str, JsonValue]:
        symbol = _normalize_symbol(symbol)
        return self._cached(
            "analyst", (symbol,), lambda: self._provider.analyst(symbol)
        )

    def earnings(
        self, symbol: str, limit: int = 12
    ) -> dict[str, JsonValue]:
        symbol = _normalize_symbol(symbol)
        limit = _validate_limit(limit, EARNINGS_LIMIT)
        return self._cached(
            "earnings",
            (symbol, limit),
            lambda: self._provider.earnings(symbol, limit),
        )

    def options(
        self,
        symbol: str,
        expiration: str | None = None,
        limit: int = 1_000,
    ) -> dict[str, JsonValue]:
        symbol = _normalize_symbol(symbol)
        expiration = _normalize_expiration(expiration)
        limit = _validate_limit(limit, OPTION_LIMIT)
        return self._cached(
            "options",
            (symbol, expiration, limit),
            lambda: self._provider.options(symbol, expiration, limit),
        )

    def news(
        self, symbol: str, limit: int = 20
    ) -> list[dict[str, JsonValue]]:
        symbol = _normalize_symbol(symbol)
        limit = _validate_limit(limit, NEWS_LIMIT)
        return self._cached(
            "news", (symbol, limit), lambda: self._provider.news(symbol, limit)
        )

    def history(
        self,
        symbol: str,
        period: str = "1y",
        interval: str = "1d",
        limit: int = 500,
    ) -> list[dict[str, JsonValue]]:
        symbol = _normalize_symbol(symbol)
        period = _validate_choice("period", period, HISTORY_PERIODS)
        interval = _validate_choice("interval", interval, DAILY_INTERVALS)
        limit = _validate_limit(limit, CANDLE_LIMIT)
        return self._cached(
            "history",
            (symbol, period, interval, limit),
            lambda: self._provider.history(symbol, period, interval, limit),
        )

    def intraday(
        self,
        symbol: str,
        period: str = "5d",
        interval: str = "5m",
        limit: int = 500,
    ) -> list[dict[str, JsonValue]]:
        symbol = _normalize_symbol(symbol)
        period = _validate_choice("period", period, INTRADAY_PERIODS)
        interval = _validate_choice("interval", interval, INTRADAY_INTERVALS)
        limit = _validate_limit(limit, CANDLE_LIMIT)
        return self._cached(
            "intraday",
            (symbol, period, interval, limit),
            lambda: self._provider.intraday(symbol, period, interval, limit),
        )

    def _cached(self, method: str, arguments: tuple[object, ...], loader: Callable[[], T]) -> T:
        return self._cache.get_or_load(
            (method, *arguments),
            CACHE_TTLS[method],
            lambda: _clean_json(loader()),
        )


def _normalize_symbol(symbol: str) -> str:
    if not isinstance(symbol, str):
        raise ValueError("symbol must be a string")
    normalized = symbol.strip().upper().replace(".", "-")
    if not _SYMBOL_PATTERN.fullmatch(normalized):
        raise ValueError("symbol must be 1-20 valid Yahoo symbol characters")
    return normalized


def _normalize_query(query: str) -> str:
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    normalized = " ".join(query.split())
    if not normalized:
        raise ValueError("query must not be empty")
    if len(normalized) > MAX_QUERY_LENGTH:
        raise ValueError(f"query must not exceed {MAX_QUERY_LENGTH} characters")
    return normalized


def _normalize_expiration(expiration: str | None) -> str | None:
    if expiration is None:
        return None
    if not isinstance(expiration, str):
        raise ValueError("option expiration must be an ISO date")
    try:
        parsed = date.fromisoformat(expiration)
    except ValueError as error:
        raise ValueError("option expiration must be an ISO date") from error
    if parsed.isoformat() != expiration:
        raise ValueError("option expiration must be an ISO date")
    return expiration


def _validate_limit(limit: int, maximum: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if not 1 <= limit <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return limit


def _validate_choice(name: str, value: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    normalized = value.strip().lower()
    if normalized not in allowed:
        raise ValueError(f"unsupported {name}: {value}")
    return normalized


def _frame_records(
    value: object,
    limit: int | None = None,
    *,
    from_end: bool = False,
) -> list[dict[str, JsonValue]]:
    if value is None or type(value).__name__ in {"NAType", "NaTType"}:
        return []
    try:
        records = value.reset_index().to_dict(orient="records")
    except Exception as error:
        raise RuntimeError("Yahoo returned an invalid table") from error
    if limit is not None:
        records = records[-limit:] if from_end else records[:limit]
    return _json_records(records)


def _json_records(
    value: object, limit: int | None = None
) -> list[dict[str, JsonValue]]:
    cleaned = _clean_json(value)
    if not isinstance(cleaned, list):
        raise RuntimeError("Yahoo returned an invalid record collection")
    if limit is not None:
        cleaned = cleaned[:limit]
    if not all(isinstance(row, dict) for row in cleaned):
        raise RuntimeError("Yahoo returned an invalid record collection")
    return cleaned  # type: ignore[return-value]


def _json_dict(value: object) -> dict[str, JsonValue]:
    cleaned = _clean_json(value)
    if not isinstance(cleaned, dict):
        raise RuntimeError("Yahoo returned an invalid object")
    return cleaned


def _clean_json(value: object) -> JsonValue:
    if value is None or type(value).__name__ in {"NAType", "NaTType"}:
        return None
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _clean_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_clean_json(item) for item in value]

    item_method = getattr(value, "item", None)
    if callable(item_method):
        try:
            item = item_method()
        except (TypeError, ValueError):
            pass
        else:
            if item is not value:
                return _clean_json(item)

    list_method = getattr(value, "tolist", None)
    if callable(list_method):
        try:
            listed = list_method()
        except (TypeError, ValueError):
            pass
        else:
            if listed is not value:
                return _clean_json(listed)

    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return str(isoformat())
        except (TypeError, ValueError):
            pass
    return str(value)
