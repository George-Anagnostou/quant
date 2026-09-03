import asyncio
import json
import sqlite3
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import polars as pl

from quant.dashboard import api_alpha
from quant.dashboard import server
from quant.dashboard.services import DashboardService
from quant.market_store import MarketDataRepository
from quant.user_data import UserDataRepository


class DashboardApiAlphaTests(unittest.TestCase):
    def test_daily_observation_models_use_one_date_field(self) -> None:
        models = (
            api_alpha.QuoteData,
            api_alpha.BarData,
            api_alpha.TechnicalPoint,
            api_alpha.RiskMetric,
            api_alpha.ScreenerRow,
            api_alpha.PositionData,
            api_alpha.PortfolioHistoryPoint,
        )

        for model in models:
            with self.subTest(model=model.__name__):
                self.assertIn("date", model.model_fields)
                self.assertNotIn("asOf", model.model_fields)
                self.assertNotIn("latestDate", model.model_fields)
                self.assertNotIn("quoteAsOf", model.model_fields)

        self.assertEqual(
            api_alpha._dated_metric(
                {"symbol": "AAPL", "latestDate": "2026-08-21"}
            ),
            {"symbol": "AAPL", "date": "2026-08-21"},
        )

    def test_exposes_an_isolated_typed_openapi_contract(self) -> None:
        schema = api_alpha.app.openapi()

        self.assertEqual(schema["info"]["version"], "alpha")
        self.assertEqual(
            set(schema["paths"]),
            {
                "/health",
                "/data/status",
                "/securities",
                "/market/quotes",
                "/securities/{symbol}/bars",
                "/securities/{symbol}/technicals",
                "/market/risk",
                "/market/screener",
                "/watchlist",
                "/portfolio",
                "/portfolio/risk",
            },
        )
        response = schema["paths"]["/market/quotes"]["get"]["responses"]["200"]
        response_schema = response["content"]["application/json"]["schema"]
        self.assertIn("ApiEnvelope_QuotesData", response_schema["$ref"])
        self.assertIn("409", schema["paths"]["/market/quotes"]["get"]["responses"])
        health_responses = schema["paths"]["/health"]["get"]["responses"]
        self.assertNotIn("409", health_responses)
        self.assertIn("422", health_responses)
        mount = next(
            route
            for route in server.app.routes
            if getattr(route, "path", None) == "/api/alpha"
        )
        self.assertIs(mount.app, api_alpha.app)

    def test_mounted_health_and_validation_error_use_alpha_wire_contract(self) -> None:
        status, health = _asgi_get(server.app, "/api/alpha/health")
        invalid_status, invalid = _asgi_get(
            server.app, "/api/alpha/securities/AAPL/bars", "limit=0"
        )

        self.assertEqual(status, 200)
        self.assertEqual(health["meta"]["apiVersion"], "alpha")
        self.assertEqual(invalid_status, 422)
        self.assertEqual(invalid["detail"]["code"], "invalid_request")

    def test_invalid_queries_fail_before_storage_checks(self) -> None:
        cases = [
            ("/health", "refresh=true"),
            ("/securities", "query=%20%20"),
            ("/securities/AAPL/bars", "start=2026-09-03&end=2026-01-01"),
            ("/securities/AAPL/technicals", "windows=bad"),
            ("/securities/AAPL/bars", "limit=1&limit=2"),
            ("/market/quotes", "symbols=AAPL&refresh=true"),
            ("/market/quotes", "symbols=AAPL,"),
            ("/data/status", "symbols="),
            ("/market/screener", "symbols="),
            ("/market/risk", "symbols=SPY"),
        ]
        with patch.object(api_alpha, "_market_database_ready") as ready:
            for path, query in cases:
                with self.subTest(path=path, query=query):
                    status, body = _asgi_get(server.app, "/api/alpha" + path, query)
                    self.assertEqual(status, 422)
                    self.assertEqual(body["detail"]["code"], "invalid_request")
            ready.assert_not_called()

    def test_routing_errors_have_structured_codes_and_do_not_redirect(self) -> None:
        for path, method, expected, code in [
            ("/api/alpha/unknown", "GET", 404, "not_found"),
            ("/api/alpha/health/", "GET", 404, "not_found"),
            ("/api/alpha/health", "POST", 405, "method_not_allowed"),
        ]:
            with self.subTest(path=path, method=method):
                status, body = _asgi_get(server.app, path, method=method)
                self.assertEqual(status, expected)
                self.assertEqual(body["detail"]["code"], code)

    def test_storage_errors_are_structured_and_do_not_leak_details(self) -> None:
        with patch.object(api_alpha._service, "market_database_ready", side_effect=
                          sqlite3.OperationalError("private database path")):
            with self.assertLogs(api_alpha.logger, level="ERROR"):
                status, body = _asgi_get(server.app, "/api/alpha/data/status")
        self.assertEqual(status, 503)
        self.assertEqual(body["detail"]["code"], "storage_unavailable")
        self.assertNotIn("private", json.dumps(body))

    @patch("quant.quotes.get_market_history", side_effect=AssertionError("network"))
    def test_every_alpha_read_leaves_prepared_storage_unchanged(self, provider) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "quant.db"
            market = MarketDataRepository(path)
            market.save(pl.DataFrame({
                "Date": [date(2026, 8, 20), date(2026, 8, 21)],
                "Symbol": ["AAPL", "AAPL"],
                "High": [125.0, 126.0], "Low": [122.0, 123.0],
                "Close": [124.0, 125.0], "Adjusted Close": [123.0, 124.0],
                "Volume": [1000, 1100],
            }))
            user = UserDataRepository(path)
            user.add_position("AAPL", 2, 100)
            user.add_watchlist("AAPL")
            service = DashboardService(
                UserDataRepository(path, read_only=True),
                MarketDataRepository(path, read_only=True),
            )
            before = path.read_bytes()
            cases = [
                ("health", "", 200), ("data/status", "", 200),
                ("securities", "query=AAPL", 200),
                ("market/quotes", "symbols=AAPL,MISSING", 200),
                ("securities/AAPL/bars", "limit=1", 200),
                ("securities/AAPL/technicals", "windows=2", 200),
                ("market/risk", "symbols=AAPL", 409),
                ("market/screener", "", 409),
                ("watchlist", "", 200), ("portfolio", "", 200),
                ("portfolio/risk", "", 409),
            ]
            with patch.object(api_alpha, "_service", service):
                with patch("quant.database.initialize_database", side_effect=AssertionError("write")):
                    for path_suffix, query, expected in cases:
                        with self.subTest(path=path_suffix):
                            status, _ = _asgi_get(server.app, "/api/alpha/" + path_suffix, query)
                            self.assertEqual(status, expected)
            self.assertEqual(path.read_bytes(), before)
            provider.assert_not_called()

    def test_missing_database_is_not_created_by_any_read(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "absent" / "quant.db"
            service = DashboardService(
                UserDataRepository(path, read_only=True),
                MarketDataRepository(path, read_only=True),
            )
            cases = [
                ("data/status", "", 200), ("securities", "query=AAPL", 200),
                ("market/quotes", "symbols=AAPL", 409),
                ("securities/AAPL/bars", "", 409),
                ("securities/AAPL/technicals", "", 409),
                ("market/risk", "symbols=AAPL", 409),
                ("market/screener", "", 409),
                ("watchlist", "", 200), ("portfolio", "", 200),
                ("portfolio/risk", "", 409),
            ]
            with patch.object(api_alpha, "_service", service):
                for suffix, query, expected in cases:
                    with self.subTest(path=suffix):
                        status, _ = _asgi_get(server.app, "/api/alpha/" + suffix, query)
                        self.assertEqual(status, expected)
            self.assertFalse(path.parent.exists())

    def test_quotes_use_fraction_returns_and_report_partial_data(self) -> None:
        service = Mock()
        service.quotes.return_value = {
            "quotes": [
                {
                    "symbol": "AAPL",
                    "name": "Apple Inc.",
                    "price": 125.0,
                    "previousClose": 100.0,
                    "change": 25.0,
                    "changePercent": 25.0,
                    "volume": 1_000,
                    "currency": "USD",
                    "asOf": "2026-08-21",
                    "provider": "yahoo",
                    "retrievedAt": "2026-08-22T00:00:00+00:00",
                },
                {"symbol": "MISSING", "error": True},
            ]
        }

        with patch("quant.dashboard.api_alpha._market_database_ready", return_value=True):
            result = api_alpha.quotes_alpha(service, "aapl,missing")

        service.quotes.assert_called_once_with(
            ["AAPL", "MISSING"], refresh=False, fetch_missing=False
        )
        self.assertEqual(result.data["quotes"][0]["changeReturn"], 0.25)
        self.assertEqual(result.data["quotes"][0]["date"], "2026-08-21")
        self.assertNotIn("asOf", result.data["quotes"][0])
        self.assertEqual(result.data["missingSymbols"], ["MISSING"])
        self.assertIn("partial_data", [warning.code for warning in result.warnings])
        api_alpha.ApiEnvelope[api_alpha.QuotesData].model_validate(result.model_dump())

    @patch("quant.quotes.get_market_history")
    def test_alpha_quote_reads_never_call_the_provider(self, get_market_history) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "quant.db"
            service = DashboardService(
                UserDataRepository(path), MarketDataRepository(path)
            )

            with self.assertRaises(api_alpha.HTTPException) as error:
                api_alpha.quotes_alpha(service, "MISSING")

            self.assertFalse(path.exists())

        self.assertEqual(error.exception.status_code, 409)
        get_market_history.assert_not_called()

    def test_bars_expose_storage_provenance(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "quant.db"
            repository = MarketDataRepository(path)
            repository.save(
                pl.DataFrame(
                    {
                        "Date": [date(2026, 8, 21)],
                        "Symbol": ["AAPL"],
                        "Open": [100.0],
                        "High": [126.0],
                        "Low": [99.0],
                        "Close": [125.0],
                        "Adjusted Close": [124.0],
                        "Volume": [1_000],
                    }
                )
            )
            service = DashboardService(UserDataRepository(path), repository)

            result = api_alpha.bars_alpha("aapl", service, limit=500)

        bar = result.data["bars"][0]
        self.assertEqual(bar["date"], "2026-08-21")
        self.assertEqual(bar["provider"], "yahoo")
        self.assertIsNotNone(bar["retrievedAt"])
        self.assertEqual(result.meta.priceBasis, "raw")
        api_alpha.ApiEnvelope[api_alpha.BarsData].model_validate(result.model_dump())

    def test_derived_analytics_expose_storage_retrieval_time(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "quant.db"
            repository = MarketDataRepository(path)
            repository.save(
                pl.DataFrame(
                    {
                        "Date": [date(2026, 8, 20), date(2026, 8, 21)],
                        "Symbol": ["AAPL", "AAPL"],
                        "High": [125.0, 126.0],
                        "Low": [122.0, 123.0],
                        "Close": [124.0, 125.0],
                        "Adjusted Close": [123.0, 124.0],
                        "Volume": [1_000, 1_100],
                    }
                )
            )
            service = DashboardService(UserDataRepository(path), repository)

            result = api_alpha.technicals_alpha(
                "aapl", service, windows="2", priceBasis="adjustedClose"
            )

        self.assertIsNotNone(result.meta.retrievedAt)
        self.assertEqual(result.data["points"][-1]["date"], "2026-08-21")

    def test_empty_portfolio_matches_its_documented_contract(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "quant.db"
            service = DashboardService(
                UserDataRepository(path), MarketDataRepository(path)
            )

            result = api_alpha.portfolio_alpha(service)

        validated = api_alpha.ApiEnvelope[api_alpha.PortfolioData].model_validate(
            result.model_dump()
        )
        self.assertEqual(validated.data.summary.positionCount, 0)
        self.assertEqual(validated.data.positions, [])


def _asgi_get(application, path: str, query: str = "", *, method: str = "GET") -> tuple[int, dict]:
    messages = []
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8001),
    }
    asyncio.run(application(scope, receive, send))
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return status, json.loads(body)


if __name__ == "__main__":
    unittest.main()
