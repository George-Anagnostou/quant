import unittest
from contextlib import redirect_stderr
from datetime import date
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from quant.cli import main


class CliTests(unittest.TestCase):
    @patch("quant.cli.query_main")
    def test_query_delegates_to_machine_readable_client(self, query_main) -> None:
        main(["query", "quotes", "AAPL", "--pretty"])

        query_main.assert_called_once_with(["quotes", "AAPL", "--pretty"])

    @patch("quant.cli.query_main")
    def test_data_status_uses_the_same_api_client(self, query_main) -> None:
        main(["data", "status", "AAPL", "MSFT"])

        query_main.assert_called_once_with(["status", "AAPL", "MSFT"])

    @patch("quant.cli.run_server")
    def test_serve_enables_startup_sync_by_default(self, run_server) -> None:
        main(["serve", "--database", "market.db"])

        run_server.assert_called_once_with(
            host="127.0.0.1",
            port=8001,
            database=Path("market.db"),
            sync_enabled=True,
            horizon=date(2025, 1, 1),
            batch_size=50,
        )

    @patch("quant.cli.run_server")
    def test_serve_accepts_explicit_runtime_options(self, run_server) -> None:
        main([
            "serve", "--host", "localhost", "--port", "9000",
            "--database", "test.db", "--no-sync",
            "--horizon", "2026-01-01", "--batch-size", "25",
        ])

        run_server.assert_called_once_with(
            host="localhost",
            port=9000,
            database=Path("test.db"),
            sync_enabled=False,
            horizon=date(2026, 1, 1),
            batch_size=25,
        )

    def test_old_commands_and_invalid_runtime_options_are_rejected(self) -> None:
        cases = [
            ["index"], ["market"], ["portfolio"],
            ["serve", "--port", "0"],
            ["serve", "--batch-size", "101"],
            ["serve", "--horizon", "not-a-date"],
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
                    main(arguments)
                self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
