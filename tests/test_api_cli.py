import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from quant.api_cli import main
from quant.api_client import (
    ApiHttpError, ApiProtocolError, ApiTimeoutError, ApiTransportError,
)


RESPONSE = {
    "data": {"quotes": []},
    "meta": {
        "apiVersion": "v1",
        "generatedAt": "2026-09-03T12:00:00Z",
        "freshness": {
            "status": "unknown",
            "ageCalendarDays": None,
            "staleAfterCalendarDays": None,
        },
    },
    "warnings": [],
}


class ApiCliTests(unittest.TestCase):
    @patch("quant.api_cli.ApiClient")
    def test_global_options_work_before_or_after_command(self, client_type) -> None:
        client_type.return_value.request.return_value = RESPONSE
        options = ["--base-url", "http://localhost:9000/api/v1", "--timeout", "2", "--pretty"]
        for argv in ([*options, "health"], ["health", *options]):
            with self.subTest(argv=argv), redirect_stdout(StringIO()) as output:
                main(argv)
                self.assertIn("\n  ", output.getvalue())
                self.assertEqual(client_type.call_args.args[0].timeout, 2)
                self.assertEqual(client_type.call_args.args[0].base_url, "http://localhost:9000/api/v1")

    @patch("quant.api_cli.ApiClient")
    def test_json_errors_and_exit_codes_leave_stdout_empty(self, client_type) -> None:
        for failure, expected, code in [
            (ApiTransportError("disconnected"), 3, "transport_error"),
            (ApiTimeoutError("timeout"), 4, "timeout"),
            (ApiHttpError(422, {"code": "invalid_request"}), 5, "api_error"),
            (ApiHttpError(503, {"code": "storage_unavailable"}), 6, "api_error"),
            (ApiProtocolError("bad envelope"), 7, "protocol_error"),
            (KeyboardInterrupt(), 130, "interrupted"),
        ]:
            with self.subTest(failure=failure):
                client_type.return_value.request.side_effect = failure
                with redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors:
                    with self.assertRaises(SystemExit) as exit_error:
                        main(["health"])
                self.assertEqual(exit_error.exception.code, expected)
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(json.loads(errors.getvalue())["error"]["code"], code)

    @patch("quant.api_cli.ApiClient")
    def test_usage_errors_do_not_contact_server(self, client_type) -> None:
        invalid_requests = [
            [], ["no-such-command"], ["--time", "1", "health"], ["quotes"],
            ["health", "--unknown"], ["quotes", "bad symbol"],
            ["search", "   "], ["search", "A", "--limit", "0"],
            ["bars", "AAPL", "--start", "2026-02-01", "--end", "2026-01-01"],
            ["bars", "AAPL", "--start", "not-a-date"],
            ["technical", "AAPL", "--windows", "0"],
            ["technical", "AAPL", "--windows", *map(str, range(1, 12))],
            ["risk", "SPY"], ["screener", "SPY"],
            ["quotes", *[f"S{index}" for index in range(21)]],
        ]
        for argv in invalid_requests:
            with self.subTest(argv=argv), redirect_stderr(StringIO()) as errors:
                with self.assertRaises(SystemExit) as exit_error:
                    main(argv)
                self.assertEqual(exit_error.exception.code, 2)
                self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "invalid_usage")
        client_type.assert_not_called()

    @patch("quant.api_cli.ApiClient")
    def test_closed_output_pipe_exits_without_traceback(self, client_type) -> None:
        client_type.return_value.request.return_value = RESPONSE
        with patch("quant.api_cli._write_json", side_effect=BrokenPipeError):
            with redirect_stdout(StringIO()), self.assertRaises(SystemExit) as error:
                main(["health"])
        self.assertEqual(error.exception.code, 0)

    @patch("quant.api_cli.ApiClient")
    def test_emits_compact_json_and_exact_risk_request(self, client_type) -> None:
        client = client_type.return_value
        client.request.return_value = RESPONSE
        output = StringIO()

        with redirect_stdout(output):
            main(
                [
                    "--base-url",
                    "http://localhost:9000/api/v1",
                    "risk",
                    "aapl",
                    "MSFT",
                    "aapl",
                    "--period",
                    "6mo",
                    "--benchmark",
                    "qqq",
                ]
            )

        self.assertEqual(output.getvalue(), json.dumps(RESPONSE, sort_keys=True, separators=(",", ":")) + "\n")
        client.request.assert_called_once_with(
            "GET",
            "market/risk",
            query={"symbols": "AAPL,MSFT", "period": "6mo", "benchmark": "QQQ"},
        )

    @patch("quant.api_cli.ApiClient")
    def test_encodes_symbol_path_and_bar_filters(self, client_type) -> None:
        client = client_type.return_value
        client.request.return_value = RESPONSE

        with redirect_stdout(StringIO()):
            main(
                [
                    "bars",
                    "BRK.B",
                    "--start",
                    "2026-01-01",
                    "--end",
                    "2026-02-01",
                    "--limit",
                    "20",
                ]
            )

        client.request.assert_called_once_with(
            "GET",
            "securities/BRK.B/bars",
            query={"start": "2026-01-01", "end": "2026-02-01", "limit": 20},
        )

    @patch("quant.api_cli.ApiClient")
    def test_emits_structured_http_error_and_exit_code(self, client_type) -> None:
        client_type.return_value.request.side_effect = ApiHttpError(
            409, {"code": "insufficient_data", "message": "no bars"}
        )
        error_output = StringIO()

        with redirect_stderr(error_output), self.assertRaises(SystemExit) as error:
            main(["quotes", "MISSING"])

        self.assertEqual(error.exception.code, 5)
        payload = json.loads(error_output.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["status"], 409)
        self.assertEqual(payload["error"]["details"]["code"], "insufficient_data")

    def test_rejects_non_finite_timeout_as_json(self) -> None:
        error_output = StringIO()

        with redirect_stderr(error_output), self.assertRaises(SystemExit) as error:
            main(["--timeout", "inf", "health"])

        self.assertEqual(error.exception.code, 2)
        self.assertEqual(
            json.loads(error_output.getvalue())["error"]["code"],
            "invalid_configuration",
        )


if __name__ == "__main__":
    unittest.main()
