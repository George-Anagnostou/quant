import json
import math
import threading
import unittest
from datetime import date, datetime

from quant.research import (
    BoundedTTLCache,
    CachedResearchService,
    YahooResearchProvider,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeProvider:
    def __init__(self) -> None:
        self.calls = []
        self.profile_value = {"name": "Apple", "sequence": 1}
        self.profile_error = None

    def search(self, query, limit=10):
        self.calls.append(("search", query, limit))
        return [{"query": query}]

    def profile(self, symbol):
        self.calls.append(("profile", symbol))
        if self.profile_error:
            raise self.profile_error
        return self.profile_value

    def analyst(self, symbol):
        self.calls.append(("analyst", symbol))
        return {"ratings": []}

    def earnings(self, symbol, limit=12):
        self.calls.append(("earnings", symbol, limit))
        return {"history": []}

    def options(self, symbol, expiration=None, limit=1_000):
        self.calls.append(("options", symbol, expiration, limit))
        return {"expiration": expiration, "calls": [], "puts": []}

    def news(self, symbol, limit=20):
        self.calls.append(("news", symbol, limit))
        return []

    def history(self, symbol, period="1y", interval="1d", limit=500):
        self.calls.append(("history", symbol, period, interval, limit))
        return []

    def intraday(self, symbol, period="5d", interval="5m", limit=500):
        self.calls.append(("intraday", symbol, period, interval, limit))
        return []


class CacheTests(unittest.TestCase):
    def test_ttl_hit_and_expiration(self) -> None:
        clock = FakeClock()
        cache = BoundedTTLCache(clock=clock)
        loads = []

        def load():
            loads.append(len(loads) + 1)
            return {"load": loads[-1]}

        self.assertEqual(cache.get_or_load("a", 10, load), {"load": 1})
        clock.advance(9.9)
        self.assertEqual(cache.get_or_load("a", 10, load), {"load": 1})
        clock.advance(0.1)
        self.assertEqual(cache.get_or_load("a", 10, load), {"load": 2})
        self.assertEqual(loads, [1, 2])

    def test_size_bound_evicts_least_recently_used(self) -> None:
        cache = BoundedTTLCache(max_entries=2)
        loads = []

        def load(key):
            loads.append(key)
            return key

        cache.get_or_load("a", 60, lambda: load("a"))
        cache.get_or_load("b", 60, lambda: load("b"))
        cache.get_or_load("a", 60, lambda: load("a"))
        cache.get_or_load("c", 60, lambda: load("c"))
        cache.get_or_load("b", 60, lambda: load("b"))

        self.assertEqual(loads, ["a", "b", "c", "b"])
        self.assertEqual(len(cache), 2)

    def test_expired_value_is_fallback_for_runtime_error(self) -> None:
        clock = FakeClock()
        cache = BoundedTTLCache(clock=clock)
        cache.get_or_load("a", 10, lambda: {"old": True})
        clock.advance(10)

        def fail():
            raise RuntimeError("offline")

        self.assertEqual(cache.get_or_load("a", 10, fail), {"old": True})

    def test_validation_errors_do_not_use_stale_value(self) -> None:
        clock = FakeClock()
        cache = BoundedTTLCache(clock=clock)
        cache.get_or_load("a", 10, lambda: "old")
        clock.advance(10)

        def fail():
            raise ValueError("invalid")

        with self.assertRaisesRegex(ValueError, "invalid"):
            cache.get_or_load("a", 10, fail)

    def test_same_key_calls_have_one_loader_in_flight(self) -> None:
        cache = BoundedTTLCache()
        started = threading.Event()
        release = threading.Event()
        calls = 0
        results = []

        def load():
            nonlocal calls
            calls += 1
            started.set()
            self.assertTrue(release.wait(2))
            return {"ok": True}

        def request():
            results.append(cache.get_or_load("same", 60, load))

        threads = [threading.Thread(target=request) for _ in range(6)]
        for thread in threads:
            thread.start()
        self.assertTrue(started.wait(2))
        release.set()
        for thread in threads:
            thread.join(2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(calls, 1)
        self.assertEqual(results, [{"ok": True}] * 6)

    def test_service_uses_injected_empty_cache(self) -> None:
        cache = BoundedTTLCache()
        service = CachedResearchService(FakeProvider(), cache=cache)

        service.profile("AAPL")

        self.assertEqual(len(cache), 1)


class CachedResearchServiceTests(unittest.TestCase):
    def test_normalizes_arguments_before_provider_and_cache(self) -> None:
        provider = FakeProvider()
        service = CachedResearchService(provider)

        first = service.profile(" brk.b ")
        second = service.profile("BRK-B")
        search = service.search("  apple   stock ", 4)

        self.assertEqual(first, second)
        self.assertEqual(search, [{"query": "apple stock"}])
        self.assertEqual(
            provider.calls,
            [("profile", "BRK-B"), ("search", "apple stock", 4)],
        )

    def test_profile_uses_24_hour_ttl(self) -> None:
        clock = FakeClock()
        provider = FakeProvider()
        service = CachedResearchService(provider, clock=clock)

        service.profile("AAPL")
        clock.advance(23 * 60 * 60)
        service.profile("AAPL")
        clock.advance(60 * 60)
        service.profile("AAPL")

        self.assertEqual(
            provider.calls,
            [("profile", "AAPL"), ("profile", "AAPL")],
        )

    def test_service_returns_stale_profile_when_provider_is_unavailable(self) -> None:
        clock = FakeClock()
        provider = FakeProvider()
        service = CachedResearchService(provider, clock=clock)
        expected = service.profile("AAPL")
        clock.advance(24 * 60 * 60)
        provider.profile_error = RuntimeError("offline")

        self.assertEqual(service.profile("AAPL"), expected)

    def test_rejects_invalid_inputs_without_calling_provider(self) -> None:
        provider = FakeProvider()
        service = CachedResearchService(provider)
        invalid_calls = [
            lambda: service.profile("AAPL $"),
            lambda: service.search("   "),
            lambda: service.search("x", 0),
            lambda: service.options("AAPL", "next friday"),
            lambda: service.history("AAPL", period="4y"),
            lambda: service.history("AAPL", interval="5m"),
            lambda: service.intraday("AAPL", interval="1d"),
        ]

        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(ValueError):
                    call()
        self.assertEqual(provider.calls, [])

    def test_cleans_injected_provider_output_and_protects_cached_value(self) -> None:
        provider = FakeProvider()
        provider.profile_value = {
            "as_of": date(2026, 8, 26),
            "missing": math.nan,
            "nested": (1, math.inf),
        }
        service = CachedResearchService(provider)

        result = service.profile("AAPL")
        result["nested"].append("mutation")
        again = service.profile("AAPL")

        self.assertEqual(
            again,
            {
                "as_of": "2026-08-26",
                "missing": None,
                "nested": [1, None],
            },
        )
        json.dumps(again)


class FakeFrame:
    def __init__(self, records) -> None:
        self.records = records

    def reset_index(self):
        return self

    def to_dict(self, orient):
        if orient != "records":
            raise AssertionError("unexpected orientation")
        return self.records


class FakeChain:
    def __init__(self) -> None:
        self.calls = FakeFrame([{"strike": 100.0}, {"strike": math.nan}])
        self.puts = FakeFrame([{"strike": 95.0}])
        self.underlying = {"quoteTime": datetime(2026, 8, 26, 14, 30)}


class FakeTicker:
    info = {"name": "Example", "value": math.nan}
    recommendations = FakeFrame([{"date": date(2026, 8, 1), "rating": "Buy"}])
    recommendations_summary = FakeFrame([])
    upgrades_downgrades = FakeFrame([{"action": "up"}])
    calendar = {"earningsDate": [date(2026, 10, 1)]}
    options = ("2026-09-18", "2026-10-16")

    def __init__(self) -> None:
        self.history_kwargs = None

    def get_earnings_dates(self, limit):
        return FakeFrame([{"date": date(2026, 7, 1), "eps": math.inf}])

    def option_chain(self, expiration):
        return FakeChain()

    def get_news(self, count):
        return [{"published": datetime(2026, 8, 26, 9, 0)}]

    def history(self, **kwargs):
        self.history_kwargs = kwargs
        return FakeFrame(
            [
                {"Date": date(2026, 8, 25), "Close": 100.0},
                {"Date": date(2026, 8, 26), "Close": math.nan},
            ]
        )


class FakeYahoo:
    def __init__(self) -> None:
        self.symbols = []
        self.ticker = FakeTicker()

    def Ticker(self, symbol):
        self.symbols.append(symbol)
        return self.ticker

    class Search:
        def __init__(self, query, max_results):
            self.quotes = [
                {"symbol": "AAPL", "score": math.nan, "query": query},
                {"symbol": "MSFT", "score": 1.0},
            ][:max_results]


class YahooResearchProviderTests(unittest.TestCase):
    def test_converts_all_yahoo_outputs_to_json_values(self) -> None:
        yahoo = FakeYahoo()
        provider = YahooResearchProvider(yahoo)
        outputs = [
            provider.search("apple", 1),
            provider.profile("brk.b"),
            provider.analyst("AAPL"),
            provider.earnings("AAPL", 5),
            provider.options("AAPL", "2026-09-18", 1),
            provider.news("AAPL", 1),
            provider.history("AAPL", "1mo", "1d", 1),
            provider.intraday("AAPL", "5d", "5m", 1),
        ]

        for output in outputs:
            json.dumps(output, allow_nan=False)
        self.assertEqual(yahoo.symbols[0], "BRK-B")
        self.assertIsNone(outputs[1]["value"])
        self.assertEqual(outputs[4]["calls"], [{"strike": 100.0}])
        self.assertEqual(
            outputs[6], [{"Date": "2026-08-26", "Close": None}]
        )
        self.assertFalse(yahoo.ticker.history_kwargs["auto_adjust"])
        self.assertFalse(yahoo.ticker.history_kwargs["actions"])

    def test_rejects_unavailable_option_expiration(self) -> None:
        provider = YahooResearchProvider(FakeYahoo())

        with self.assertRaisesRegex(ValueError, "not available"):
            provider.options("AAPL", "2026-11-20")


if __name__ == "__main__":
    unittest.main()
