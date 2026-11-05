"""JPMorgan tax-lot importer: quirks from PORTFOLIO_CASE_STUDY plus e2e preview."""

from __future__ import annotations

import hashlib
import json
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from quant.cli import main
from quant.importers import get_parser, list_brokers
from quant.importers.jpmorgan import JPMorganTaxLotParser
from quant.importers.service import run_import
from quant.user_data import UserDataRepository

FIXTURE = Path(__file__).parent / "fixtures" / "jpmorgan_taxlots_sample.csv"
ACCOUNT = "JPMorgan brokerage"


class BrokerRegistryTests(unittest.TestCase):
    def test_jpmorgan_is_registered(self) -> None:
        self.assertEqual(list_brokers(), ["jpmorgan"])
        self.assertIsInstance(get_parser("jpmorgan"), JPMorganTaxLotParser)

    def test_unknown_broker_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown broker"):
            get_parser("fidelity")


class JPMorganParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = JPMorganTaxLotParser()
        self.parsed = self.parser.parse(FIXTURE, ACCOUNT)

    def test_title_line_before_header_is_accepted(self) -> None:
        self.assertEqual(self.parsed.effective_date.isoformat(), "2026-09-01")
        self.assertEqual(self.parsed.broker, "jpmorgan")
        self.assertTrue(self.parsed.import_key.startswith("jpmorgan:"))

    def test_import_key_is_stable_content_digest(self) -> None:
        digest = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
        self.assertEqual(self.parsed.content_digest, digest)
        self.assertEqual(self.parsed.import_key, f"jpmorgan:{digest}")
        again = self.parser.parse(FIXTURE, ACCOUNT)
        self.assertEqual(again.import_key, self.parsed.import_key)

    def test_cash_row_without_ticker_becomes_cash_action(self) -> None:
        cash = [action for action in self.parsed.actions if action["action"] == "cash"]
        self.assertEqual(len(cash), 1)
        self.assertEqual(cash[0]["account"], ACCOUNT)
        self.assertEqual(Decimal(cash[0]["amount"]), Decimal("1500.00"))

    def test_lots_preserve_total_cost_over_unit_cost(self) -> None:
        lots = [action["lot"] for action in self.parsed.actions if action["action"] == "add"]
        apple_lots = [lot for lot in lots if lot["symbol"] == "AAPL"]
        self.assertEqual(len(apple_lots), 2)
        costs = sorted(Decimal(lot["totalCost"]) for lot in apple_lots)
        self.assertEqual(costs, [Decimal("300.01"), Decimal("700.00")])
        self.assertTrue(
            any("does not equal Cost" in warning for warning in self.parsed.warnings),
            self.parsed.warnings,
        )

    def test_used_outstanding_is_not_summed_as_quantity(self) -> None:
        lots = [action["lot"] for action in self.parsed.actions if action["action"] == "add"]
        apple = [lot for lot in lots if lot["symbol"] == "AAPL"]
        quantities = sorted(Decimal(lot["quantity"]) for lot in apple)
        self.assertEqual(quantities, [Decimal("3"), Decimal("7")])
        # Symbol-level Used/Outstanding on the fixture is 10; summing would be wrong.
        self.assertNotEqual(sum(quantities), Decimal("20"))

    def test_acquisition_dates_parse_mdy(self) -> None:
        lots = [action["lot"] for action in self.parsed.actions if action["action"] == "add"]
        by_symbol = {(lot["symbol"], lot["quantity"]): lot["acquired"] for lot in lots}
        self.assertEqual(by_symbol[("AAPL", "3")], "2025-08-20")
        self.assertEqual(by_symbol[("BRK-B", "1")], "2024-06-01")

    def test_damaged_cusips_preserved_raw_and_omitted_canonically(self) -> None:
        lots = [action["lot"] for action in self.parsed.actions if action["action"] == "add"]
        damaged = [lot for lot in lots if lot["symbol"] in {"BADCUSIP", "SCINOT"}]
        self.assertEqual(len(damaged), 2)
        for lot in damaged:
            self.assertNotIn("cusip", lot)
            self.assertIn("CUSIP", lot["original"])
        self.assertTrue(any("damaged CUSIP" in warning for warning in self.parsed.warnings))
        good = next(lot for lot in lots if lot["symbol"] == "AAPL")
        self.assertEqual(good["cusip"], "037833100")

    def test_source_lot_id_is_document_row_identity(self) -> None:
        lots = [action["lot"] for action in self.parsed.actions if action["action"] == "add"]
        ids = [lot["sourceLotId"] for lot in lots]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(item.startswith(f"jpmorgan:{self.parsed.content_digest[:12]}:row:") for item in ids))

    def test_sector_unknown_and_account_action_present(self) -> None:
        account_actions = [action for action in self.parsed.actions if action["action"] == "account"]
        self.assertEqual(len(account_actions), 1)
        self.assertEqual(account_actions[0]["account"]["name"], ACCOUNT)
        self.assertEqual(account_actions[0]["account"]["institution"], "JPMorgan")
        lots = [action["lot"] for action in self.parsed.actions if action["action"] == "add"]
        self.assertTrue(all("sector" not in lot for lot in lots))

    def test_non_usd_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "cad.csv"
            path.write_text(
                "Title\n"
                "Ticker,Quantity,Cost,Unit Cost,Acquisition Date,Account type,Base CCY,"
                "Description,CUSIP,Asset Class,Asset Strategy,As of,Price,Value,Pricing Date,"
                "Used/Outstanding\n"
                "SHOP,1,10,10,01/01/2025,Brokerage,CAD,Shopify,,,Equity,,09/01/2026,10,10,09/01/2026,1\n"
            )
            with self.assertRaisesRegex(ValueError, "only USD"):
                self.parser.parse(path, ACCOUNT)

    def test_options_asset_class_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "opt.csv"
            path.write_text(
                "Title\n"
                "Ticker,Quantity,Cost,Unit Cost,Acquisition Date,Account type,Base CCY,"
                "Description,CUSIP,Asset Class,Asset Strategy,As of,Price,Value,Pricing Date,"
                "Used/Outstanding\n"
                "AAPL,1,10,10,01/01/2025,Brokerage,USD,Call option,,Option,,09/01/2026,10,10,09/01/2026,1\n"
            )
            with self.assertRaisesRegex(ValueError, "unsupported Asset Class"):
                self.parser.parse(path, ACCOUNT)


class BrokerImportEndToEndTests(unittest.TestCase):
    def test_preview_against_temp_database_does_not_write(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "quant.db"
            outcome = run_import(
                FIXTURE,
                ACCOUNT,
                broker="jpmorgan",
                apply=False,
                database=database,
            )
            self.assertFalse(outcome["applied"])
            self.assertFalse(database.exists())
            self.assertEqual(outcome["result"]["after"]["revision"], 1)
            lots = outcome["result"]["after"]["lots"]
            self.assertEqual(len(lots), 7)
            cash = outcome["result"]["after"]["accounts"][0]["cash"]
            self.assertEqual(Decimal(cash), Decimal("1500.00"))
            self.assertEqual(outcome["account"], ACCOUNT)

    def test_apply_is_idempotent_via_import_key(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "quant.db"
            first = run_import(FIXTURE, ACCOUNT, apply=True, database=database)
            self.assertTrue(first["applied"])
            self.assertFalse(first["alreadyApplied"])
            self.assertEqual(UserDataRepository(database).portfolio_state()["revision"], 1)
            second = run_import(FIXTURE, ACCOUNT, apply=True, database=database)
            self.assertTrue(second["alreadyApplied"])
            self.assertFalse(second["applied"])
            state = UserDataRepository(database).portfolio_state()
            self.assertEqual(state["revision"], 1)
            self.assertEqual(len(state["lots"]), 7)
            self.assertEqual(second["importKey"], first["importKey"])

    def test_cli_preview_defaults_and_pretty_json(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "quant.db"
            buffer = StringIO()
            with redirect_stdout(buffer):
                main(
                    [
                        "import",
                        str(FIXTURE),
                        "--account",
                        ACCOUNT,
                        "--broker",
                        "jpmorgan",
                        "--database",
                        str(database),
                        "--pretty",
                    ]
                )
            payload = json.loads(buffer.getvalue())
            self.assertFalse(payload["applied"])
            self.assertEqual(payload["broker"], "jpmorgan")
            self.assertEqual(payload["account"], ACCOUNT)
            self.assertEqual(payload["lotCount"], 7)
            self.assertEqual(payload["batch"]["actions"][0]["action"], "account")
            self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
