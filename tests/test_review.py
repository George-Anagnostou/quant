"""Portfolio review markdown report — synthetic lots and stored bars only."""

from __future__ import annotations

import json
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import polars as pl

from quant.calendars import sessions
from quant.cli import main
from quant.database import database_connection
from quant.market_store import MarketDataRepository
from quant.portfolio_contracts import PortfolioWrite
from quant.review import build_review_report, write_review_report
from quant.user_data import UserDataRepository


def _seed_bars(path: Path, symbols: list[str], end: date, sessions_needed: int = 30) -> list[date]:
    days = sessions(end - timedelta(days=sessions_needed * 2 + 10), end)
    days = days[-sessions_needed:]
    rows = []
    for index, day in enumerate(days):
        for offset, symbol in enumerate(symbols):
            price = 100.0 + index + offset * 10
            rows.append(
                {
                    "Date": day,
                    "Symbol": symbol,
                    "Close": price,
                    "Adjusted Close": price,
                    "High": price + 1,
                    "Low": price - 1,
                    "Volume": 1000 + index,
                }
            )
    MarketDataRepository(path).save(pl.DataFrame(rows))
    with database_connection(path) as db:
        db.execute("UPDATE securities SET currency='USD',calendar='XNYS'")
    return days


class ReviewReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "quant.db"
        self.repo = UserDataRepository(self.path)
        self.as_of = date(2026, 11, 4)  # Wednesday
        # Long-term lot (>365 days before as_of) and short-term lot.
        opening = PortfolioWrite(
            importKey="review-opening",
            expectedRevision=0,
            effectiveDate="2026-10-01",
            source="synthetic-review-fixture",
            reason="Seed review portfolio",
            actions=[
                {
                    "action": "account",
                    "account": {
                        "name": "JPMorgan brokerage",
                        "currency": "USD",
                        "accountType": "Brokerage",
                        "institution": "JPMorgan",
                    },
                },
                {"action": "cash", "account": "JPMorgan brokerage", "amount": "1000"},
                {
                    "action": "add",
                    "lot": {
                        "symbol": "AAPL",
                        "account": "JPMorgan brokerage",
                        "quantity": "10",
                        "totalCost": "1000",
                        "acquired": "2024-06-01",
                        "assetClass": "Equity",
                        "strategy": "Core",
                        "sourceLotId": "long-aapl",
                    },
                },
                {
                    "action": "add",
                    "lot": {
                        "symbol": "AAPL",
                        "account": "JPMorgan brokerage",
                        "quantity": "5",
                        "totalCost": "900",
                        "acquired": "2026-08-01",
                        "assetClass": "Equity",
                        "strategy": "Core",
                        "sourceLotId": "short-aapl",
                    },
                },
                {
                    "action": "add",
                    "lot": {
                        "symbol": "MSFT",
                        "account": "JPMorgan brokerage",
                        "quantity": "2",
                        "totalCost": "700",
                        "acquired": "2025-01-15",
                        "assetClass": "Equity",
                        "strategy": "Core",
                        "sourceLotId": "msft-1",
                    },
                },
            ],
        )
        self.repo.apply_portfolio(opening)
        self.days = _seed_bars(self.path, ["AAPL", "MSFT", "SPY"], self.as_of, sessions_needed=30)

    def test_report_contains_required_sections_and_terms(self) -> None:
        report, meta = build_review_report(database=self.path, as_of=self.as_of)
        for heading in (
            "# Portfolio review",
            "## Combined view",
            "## Allocation and concentration",
            "## Short- vs long-term unrealized gains",
            "## Risk vs SPY",
            "## Diff vs prior revision",
            "## Warnings",
        ):
            self.assertIn(heading, report)
        self.assertIn("JPMorgan brokerage", report)
        self.assertIn("Short-term lots:", report)
        self.assertIn("Long-term lots:", report)
        self.assertIn("Largest position:", report)
        self.assertGreaterEqual(meta["lotCount"], 3)
        self.assertEqual(meta["date"], self.as_of.isoformat())
        # Long-term AAPL (acquired 2024-06-01) and short-term AAPL (2026-08-01).
        self.assertRegex(report, r"Short-term lots: [1-9]")
        self.assertRegex(report, r"Long-term lots: [1-9]")

    def test_risk_succeeds_with_stored_history(self) -> None:
        with patch("quant.market_analysis.datetime") as current_datetime:
            current_datetime.now.return_value.date.return_value = self.as_of
            # EASTERN_TIME aware path uses datetime.now(EASTERN_TIME).date()
            from datetime import datetime
            from quant.market_data import EASTERN_TIME

            current_datetime.now.side_effect = lambda tz=None: datetime(
                self.as_of.year, self.as_of.month, self.as_of.day, 21, 0, tzinfo=EASTERN_TIME
            )
            report, meta = build_review_report(database=self.path, as_of=self.as_of)
        self.assertNotIn("Risk vs SPY unavailable", "\n".join(meta["warnings"]))
        self.assertIn("Beta vs SPY:", report)
        self.assertIn("Cumulative return (current-share):", report)

    def test_risk_degrades_when_history_missing(self) -> None:
        empty = Path(self.tmp.name) / "empty.db"
        UserDataRepository(empty).apply_portfolio(
            PortfolioWrite(
                importKey="cash-only",
                expectedRevision=0,
                effectiveDate="2026-10-01",
                source="fixture",
                reason="Cash only",
                actions=[
                    {"action": "account", "account": {"name": "Cash acct"}},
                    {"action": "cash", "account": "Cash acct", "amount": "50"},
                ],
            )
        )
        report, meta = build_review_report(database=empty, as_of=self.as_of)
        self.assertIn("Risk vs SPY", report)
        self.assertTrue(
            any("Risk vs SPY skipped" in item or "unavailable" in item for item in meta["warnings"])
            or "No security positions" in report
        )

    def test_diff_vs_prior_revision_after_second_batch(self) -> None:
        self.repo.apply_portfolio(
            PortfolioWrite(
                importKey="deposit-1",
                expectedRevision=1,
                effectiveDate="2026-10-02",
                source="fixture",
                reason="Deposit",
                actions=[{"action": "deposit", "account": "JPMorgan brokerage", "amount": "25"}],
            )
        )
        report, meta = build_review_report(database=self.path, as_of=self.as_of)
        self.assertIn("Revision 1 → 2", report)
        self.assertIn("deposit-1", report)
        self.assertIn("$1,000.00", report)
        self.assertIn("$1,025.00", report)

    def test_write_default_path_and_cli(self) -> None:
        out_dir = Path(self.tmp.name) / "reports"
        destination = out_dir / "review-custom.md"
        with patch("quant.market_analysis.datetime") as current_datetime:
            from datetime import datetime
            from quant.market_data import EASTERN_TIME

            current_datetime.now.side_effect = lambda tz=None: datetime(
                self.as_of.year, self.as_of.month, self.as_of.day, 21, 0, tzinfo=EASTERN_TIME
            )
            result = write_review_report(
                database=self.path, as_of=self.as_of, output=destination
            )
        self.assertTrue(destination.exists())
        self.assertEqual(result["output"], str(destination))
        text = destination.read_text()
        self.assertIn("Combined view", text)

        buffer = StringIO()
        default_out = Path(self.tmp.name) / "cli-out.md"
        with redirect_stdout(buffer):
            with patch("quant.market_analysis.datetime") as current_datetime:
                from datetime import datetime
                from quant.market_data import EASTERN_TIME

                current_datetime.now.side_effect = lambda tz=None: datetime(
                    self.as_of.year, self.as_of.month, self.as_of.day, 21, 0, tzinfo=EASTERN_TIME
                )
                main(
                    [
                        "review",
                        "--date",
                        self.as_of.isoformat(),
                        "--database",
                        str(self.path),
                        "--output",
                        str(default_out),
                        "--pretty",
                    ]
                )
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["date"], self.as_of.isoformat())
        self.assertTrue(default_out.exists())


if __name__ == "__main__":
    unittest.main()
