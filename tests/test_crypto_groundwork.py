"""Phase 1B groundwork: FIFO sells, CRYPTO_UTC calendar, crypto asset IDs + bars."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import polars as pl

from quant.calendars import (
    CALENDAR_VERSION,
    freshness,
    latest_completed_session,
    sessions,
)
from quant.crypto_assets import (
    CRYPTO_ASSETS,
    canonical_crypto_symbol,
    is_crypto_symbol,
    priced_crypto_symbols,
    yahoo_symbol_for,
)
from quant.database import database_connection, initialize_database
from quant.ingestion import IngestionService
from quant.market_data import normalize_symbol
from quant.market_store import MarketDataRepository
from quant.portfolio_contracts import PortfolioWrite, SellAction
from quant.review import build_review_report
from quant.user_data import UserDataRepository


UTC = ZoneInfo("UTC")


class CryptoCalendarTests(unittest.TestCase):
    def test_crypto_utc_includes_weekends_and_holidays(self) -> None:
        days = sessions(date(2026, 7, 3), date(2026, 7, 5), "CRYPTO_UTC")
        self.assertEqual(days, [date(2026, 7, 3), date(2026, 7, 4), date(2026, 7, 5)])
        xnys = sessions(date(2026, 7, 3), date(2026, 7, 5), "XNYS")
        self.assertNotIn(date(2026, 7, 4), xnys)  # US Independence Day observed

    def test_latest_completed_crypto_day_is_yesterday_utc(self) -> None:
        now = datetime(2026, 11, 5, 15, 30, tzinfo=UTC)
        self.assertEqual(latest_completed_session(now, "CRYPTO_UTC"), date(2026, 11, 4))

    def test_freshness_for_crypto_calendar(self) -> None:
        now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
        current = freshness("2026-11-04", "CRYPTO_UTC", now=now)
        self.assertEqual(current["status"], "current")
        stale = freshness("2026-11-02", "CRYPTO_UTC", now=now)
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["missingSessions"], 2)

    def test_unsupported_calendar_still_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported calendar"):
            sessions(date(2026, 1, 1), date(2026, 1, 2), "LSE")


class CryptoAssetIdTests(unittest.TestCase):
    def test_stable_ids_and_yahoo_pairs(self) -> None:
        self.assertEqual(set(CRYPTO_ASSETS), {"BTC", "ETH", "SOL", "PSOL"})
        self.assertEqual(priced_crypto_symbols(), ["BTC", "ETH", "SOL"])
        self.assertEqual(yahoo_symbol_for("ETH"), "ETH-USD")
        self.assertIsNone(yahoo_symbol_for("PSOL"))
        self.assertEqual(canonical_crypto_symbol("eth-usd"), "ETH")
        self.assertEqual(normalize_symbol("ETH-USD"), "ETH")
        self.assertTrue(is_crypto_symbol("SOL"))
        self.assertFalse(is_crypto_symbol("AAPL"))
        self.assertIn("crypto-utc", CALENDAR_VERSION)


class FifoSellTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "quant.db"
        self.repo = UserDataRepository(self.path)
        opening = PortfolioWrite(
            importKey="fifo-open",
            expectedRevision=0,
            effectiveDate="2026-09-01",
            source="fixture",
            reason="Seed FIFO lots",
            actions=[
                {"action": "account", "account": {"name": "Brokerage"}},
                {"action": "cash", "account": "Brokerage", "amount": "0"},
                {
                    "action": "add",
                    "lot": {
                        "symbol": "ETH",
                        "account": "Brokerage",
                        "quantity": "2",
                        "totalCost": "2000",
                        "acquired": "2024-01-01",
                        "sourceLotId": "old",
                    },
                },
                {
                    "action": "add",
                    "lot": {
                        "symbol": "ETH",
                        "account": "Brokerage",
                        "quantity": "3",
                        "totalCost": "4500",
                        "acquired": "2025-06-01",
                        "sourceLotId": "new",
                    },
                },
            ],
        )
        self.repo.apply_portfolio(opening)

    def test_fifo_consumes_oldest_lots_with_partial_split(self) -> None:
        body = PortfolioWrite(
            importKey="fifo-sell",
            expectedRevision=1,
            effectiveDate="2026-09-02",
            source="fixture",
            reason="FIFO sell",
            actions=[
                {
                    "action": "sell",
                    "account": "Brokerage",
                    "symbol": "ETH",
                    "quantity": "3",
                    "unitPrice": "1200",
                    "fees": "3",
                }
            ],
        )
        result = self.repo.apply_portfolio(body)
        sells = [change for change in result["changes"] if change["action"] == "sell"]
        self.assertEqual(len(sells), 2)
        self.assertTrue(all(change.get("fifo") for change in sells))
        # Oldest lot fully closed (qty 2), newer lot partially sold (qty 1 of 3).
        self.assertEqual(sells[0]["after"], None)
        self.assertEqual(Decimal(sells[0]["quantity"]), Decimal("2"))
        self.assertEqual(Decimal(sells[0]["removedCost"]), Decimal("2000"))
        self.assertEqual(Decimal(sells[1]["quantity"]), Decimal("1"))
        self.assertEqual(Decimal(sells[1]["removedCost"]), Decimal("1500"))
        remaining = result["after"]["lots"]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["symbol"], "ETH")
        self.assertEqual(Decimal(remaining[0]["quantity"]), Decimal("2"))
        self.assertEqual(Decimal(remaining[0]["totalCost"]), Decimal("3000"))
        # Proceeds: 3*1200 - 3 fees = 3597; realized = 3597 - 3500 = 97.
        total_proceeds = sum(Decimal(change["proceeds"]) for change in sells)
        total_removed = sum(Decimal(change["removedCost"]) for change in sells)
        total_gain = sum(Decimal(change["realizedGain"]) for change in sells)
        self.assertEqual(total_proceeds, Decimal("3597"))
        self.assertEqual(total_removed, Decimal("3500"))
        self.assertEqual(total_gain, Decimal("97"))
        self.assertEqual(Decimal(result["after"]["accounts"][0]["cash"]), Decimal("3597"))

    def test_explicit_lot_sell_unchanged(self) -> None:
        lots = self.repo.portfolio_state()["lots"]
        newer = next(lot for lot in lots if lot.get("sourceLotId") == "new")
        body = PortfolioWrite(
            importKey="explicit-sell",
            expectedRevision=1,
            effectiveDate="2026-09-02",
            source="fixture",
            reason="Explicit lot sell",
            actions=[
                {
                    "action": "sell",
                    "lotId": newer["id"],
                    "quantity": "1",
                    "unitPrice": "1500",
                    "fees": "0",
                }
            ],
        )
        result = self.repo.apply_portfolio(body)
        sells = [change for change in result["changes"] if change["action"] == "sell"]
        self.assertEqual(len(sells), 1)
        self.assertNotIn("fifo", sells[0])
        self.assertEqual(sells[0]["lotId"], newer["id"])

    def test_sell_action_contract_accepts_fifo_shape(self) -> None:
        SellAction(
            action="sell",
            account="Brokerage",
            symbol="eth-usd",
            quantity="1",
            unitPrice="10",
        )
        with self.assertRaises(ValueError):
            SellAction(action="sell", quantity="1", unitPrice="10")


class CryptoIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "quant.db"
        initialize_database(self.path)

    def test_plan_and_audit_use_crypto_utc_not_xnys(self) -> None:
        days = [date(2026, 7, 3) + timedelta(days=i) for i in range(5)]  # includes weekend
        rows = []
        for index, day in enumerate(days):
            if day == date(2026, 7, 5):  # leave a UTC gap (Sunday)
                continue
            price = 3000 + index
            rows.append(
                {
                    "Date": day,
                    "Symbol": "ETH",
                    "Open": price,
                    "High": price + 1,
                    "Low": price - 1,
                    "Close": price,
                    "Adjusted Close": price,
                    "Volume": 1000,
                }
            )
        MarketDataRepository(self.path).save(pl.DataFrame(rows))
        with database_connection(self.path) as db:
            db.execute(
                "UPDATE securities SET currency='USD',calendar='CRYPTO_UTC',instrument_type='crypto' WHERE symbol='ETH'"
            )
            db.execute(
                "INSERT INTO sync_coverage(symbol,requested_start,checked_at) VALUES (?,?,?)",
                ("ETH", days[0].isoformat(), datetime.now(timezone.utc).isoformat()),
            )
        service = IngestionService(self.path)
        _, reasons = service.plan("ETH", days[0], days[-1])
        self.assertIn("internal_gap", reasons)
        # XNYS would not treat Jul 4-5 the same way; crypto expects every UTC day.
        service._audit_gaps("ETH", days[-1])
        with database_connection(self.path, read_only=True) as db:
            gaps = [
                row[0]
                for row in db.execute(
                    "SELECT session_date FROM data_issues WHERE symbol='ETH' AND code='missing_session' AND resolved_at IS NULL"
                )
            ]
        self.assertIn(date(2026, 7, 5).isoformat(), gaps)

    def test_publish_sets_crypto_metadata_and_yahoo_provider_symbol(self) -> None:
        days = [date(2026, 8, 1) + timedelta(days=i) for i in range(3)]
        frame = pl.DataFrame(
            [
                {
                    "Date": day,
                    "Symbol": "ETH",
                    "Open": 3000.0,
                    "High": 3010.0,
                    "Low": 2990.0,
                    "Close": 3005.0,
                    "Adjusted Close": 3005.0,
                    "Volume": 10,
                }
                for day in days
            ]
        )

        class FakeYahoo:
            name = "yahoo"

            def constituents(self):
                return pl.DataFrame({"Symbol": ["AAPL"], "Company": ["Apple"]})

            def history(self, symbols, start):
                self.last = (symbols, start)
                return frame.filter(pl.col("Symbol").is_in(symbols) & (pl.col("Date") >= start))

        provider = FakeYahoo()
        service = IngestionService(self.path, provider, sleep=lambda _: None)
        service.known_us_symbols = set()
        saved = service._publish("ETH", frame, days[-1])
        self.assertGreater(saved, 0)
        with database_connection(self.path, read_only=True) as db:
            row = db.execute(
                "SELECT currency, calendar, instrument_type FROM securities WHERE symbol='ETH'"
            ).fetchone()
            self.assertEqual(tuple(row), ("USD", "CRYPTO_UTC", "crypto"))
            provider_symbol = db.execute(
                "SELECT provider_symbol FROM provider_symbols p "
                "JOIN securities s ON s.id=p.security_id WHERE s.symbol='ETH' AND provider='yahoo'"
            ).fetchone()[0]
        self.assertEqual(provider_symbol, "ETH-USD")
        stored = MarketDataRepository(self.path).load(["ETH"])
        self.assertEqual(stored["Symbol"].unique().to_list(), ["ETH"])


class ReviewExcludesCryptoTests(unittest.TestCase):
    def test_stock_review_omits_crypto_lots(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "quant.db"
            repo = UserDataRepository(path)
            as_of = date(2026, 11, 4)
            repo.apply_portfolio(
                PortfolioWrite(
                    importKey="mix",
                    expectedRevision=0,
                    effectiveDate="2026-10-01",
                    source="fixture",
                    reason="mixed",
                    actions=[
                        {"action": "account", "account": {"name": "Main"}},
                        {"action": "cash", "account": "Main", "amount": "100"},
                        {
                            "action": "add",
                            "lot": {
                                "symbol": "AAPL",
                                "account": "Main",
                                "quantity": "1",
                                "totalCost": "100",
                                "acquired": "2025-01-01",
                            },
                        },
                        {
                            "action": "add",
                            "lot": {
                                "symbol": "ETH",
                                "account": "Main",
                                "quantity": "1",
                                "totalCost": "2000",
                                "acquired": "2025-01-01",
                            },
                        },
                    ],
                )
            )
            MarketDataRepository(path).save(
                pl.DataFrame(
                    {
                        "Date": [as_of - timedelta(days=1), as_of],
                        "Symbol": ["AAPL", "AAPL"],
                        "Close": [150.0, 160.0],
                        "Adjusted Close": [150.0, 160.0],
                        "High": [151.0, 161.0],
                        "Low": [149.0, 159.0],
                        "Volume": [1000, 1100],
                    }
                )
            )
            with database_connection(path) as db:
                db.execute("UPDATE securities SET currency='USD',calendar='XNYS' WHERE symbol='AAPL'")
            report, meta = build_review_report(database=path, as_of=as_of)
            self.assertIn("Omitted 1 crypto lot", "\n".join(meta["warnings"]))
            self.assertIn("AAPL", report)
            self.assertNotIn("| ETH |", report)
            self.assertEqual(meta["lotCount"], 1)


if __name__ == "__main__":
    unittest.main()
