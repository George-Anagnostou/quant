import io
import json
import socket
import unittest
from http.client import BadStatusLine, IncompleteRead
from urllib.error import HTTPError, URLError
from unittest.mock import Mock

from quant.api_client import (
    MAX_RESPONSE_BYTES,
    ApiClient,
    ApiHttpError,
    ApiProtocolError,
    ApiTimeoutError,
    ApiTransportError,
    ClientConfig,
)


class FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.body = json.dumps(payload).encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def getcode(self) -> int:
        return self.status

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


def envelope(data: object | None = None) -> dict:
    return {
        "data": data,
        "meta": {
            "apiVersion": "alpha",
            "generatedAt": "2026-09-03T12:00:00Z",
            "freshness": {
                "status": "unknown",
                "ageCalendarDays": None,
                "staleAfterCalendarDays": None,
            },
        },
        "warnings": [],
    }


class ApiClientTests(unittest.TestCase):
    def test_rejects_ambiguous_or_non_finite_json(self) -> None:
        for raw_data in (b"NaN", b"Infinity", b"-Infinity", b"1e999", b'{"x":1,"x":2}'):
            with self.subTest(raw_data=raw_data):
                response = FakeResponse(envelope())
                response.body = response.body.replace(b'"data": null', b'"data": ' + raw_data)
                opener = Mock()
                opener.open.return_value = response
                with self.assertRaisesRegex(ApiProtocolError, "invalid JSON"):
                    ApiClient(ClientConfig("http://localhost/api/alpha"), opener).request("GET", "health")

    def test_malformed_http_error_json_still_produces_a_safe_http_error(self) -> None:
        opener = Mock()
        opener.open.side_effect = HTTPError(
            "http://localhost", 500, "error", {},
            io.BytesIO(b'{"detail":{"value":NaN}}'),
        )
        with self.assertRaises(ApiHttpError) as error:
            ApiClient(ClientConfig("http://localhost/api/alpha"), opener).request("GET", "health")
        self.assertEqual(error.exception.detail, {"message": "API request failed"})

    def test_malformed_status_is_a_transport_error(self) -> None:
        opener = Mock()
        opener.open.side_effect = BadStatusLine("garbage")
        with self.assertRaises(ApiTransportError):
            ApiClient(ClientConfig("http://localhost/api/alpha"), opener).request("GET", "health")

    def test_redirects_are_not_followed(self) -> None:
        opener = Mock()
        body = io.BytesIO(b"")
        opener.open.side_effect = HTTPError("http://localhost", 307, "redirect", {}, body)
        with self.assertRaisesRegex(ApiProtocolError, "redirects"):
            ApiClient(ClientConfig("http://localhost/api/alpha"), opener).request("GET", "health")
        self.assertTrue(body.closed)

    def test_rejects_url_whitespace_before_urlsplit_can_strip_it(self) -> None:
        for url in (
            "\nhttp://localhost/api/alpha", "http://local\thost/api/alpha",
            "http://localhost/api/alpha\n", "http://localhost/api alpha",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ClientConfig(url)

    def test_encodes_query_and_validates_alpha_envelope(self) -> None:
        opener = Mock()
        opener.open.return_value = FakeResponse(envelope({"ok": True}))
        client = ApiClient(ClientConfig("http://localhost:8001/api/alpha/", 4), opener)

        result = client.request(
            "GET", "market/quotes", query={"symbols": "BRK.B,AAPL", "skip": None}
        )

        self.assertTrue(result["data"]["ok"])
        request = opener.open.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://localhost:8001/api/alpha/market/quotes?symbols=BRK.B%2CAAPL",
        )
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 4)

    def test_rejects_invalid_configuration_and_envelopes(self) -> None:
        for timeout in (0, float("inf"), float("nan")):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(ValueError, "timeout"):
                    ClientConfig("http://localhost/api/alpha", timeout)
        with self.assertRaisesRegex(ValueError, "base URL"):
            ClientConfig("http://user:secret@localhost/api/alpha", 1)
        with self.assertRaisesRegex(ValueError, "invalid port"):
            ClientConfig("http://localhost:notaport/api/alpha", 1)
        with self.assertRaisesRegex(ValueError, "base URL"):
            ClientConfig("http://local host/api/alpha", 1)

        opener = Mock()
        opener.open.return_value = FakeResponse({"unrelated": True})
        with self.assertRaisesRegex(ApiProtocolError, "alpha envelope"):
            ApiClient(ClientConfig("http://localhost/api/alpha"), opener).request(
                "GET", "health"
            )

        malformed = envelope()
        malformed["meta"].pop("freshness")
        opener.open.return_value = FakeResponse(malformed)
        with self.assertRaisesRegex(ApiProtocolError, "alpha envelope"):
            ApiClient(ClientConfig("http://localhost/api/alpha"), opener).request(
                "GET", "health"
            )

    def test_maps_timeout_and_http_errors(self) -> None:
        opener = Mock()
        opener.open.side_effect = URLError(socket.timeout())
        with self.assertRaises(ApiTimeoutError):
            ApiClient(ClientConfig("http://localhost/api/alpha"), opener).request(
                "GET", "health"
            )

        body = io.BytesIO(
            json.dumps({"detail": {"code": "invalid_request"}}).encode()
        )
        opener.open.side_effect = HTTPError(
            "http://localhost/api/alpha/health", 422, "error", {}, body
        )
        with self.assertRaises(ApiHttpError) as error:
            ApiClient(ClientConfig("http://localhost/api/alpha"), opener).request(
                "GET", "health"
            )
        self.assertEqual(error.exception.status, 422)
        self.assertEqual(error.exception.detail["code"], "invalid_request")

    def test_rejects_oversized_response(self) -> None:
        opener = Mock()
        response = FakeResponse(envelope())
        response.body = b"x" * (MAX_RESPONSE_BYTES + 1)
        opener.open.return_value = response

        with self.assertRaisesRegex(ApiProtocolError, "exceeds"):
            ApiClient(ClientConfig("http://localhost/api/alpha"), opener).request(
                "GET", "health"
            )

    def test_maps_interrupted_response_to_transport_error(self) -> None:
        opener = Mock()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.getcode.return_value = 200
        response.read.side_effect = IncompleteRead(b"partial", 100)
        opener.open.return_value = response

        with self.assertRaises(ApiTransportError):
            ApiClient(ClientConfig("http://localhost/api/alpha"), opener).request(
                "GET", "health"
            )

    def test_maps_interrupted_http_error_body_to_transport_error(self) -> None:
        opener = Mock()
        body = Mock()
        body.read.side_effect = IncompleteRead(b"partial", 100)
        error = HTTPError(
            "http://localhost/api/alpha/health", 500, "error", {}, body
        )
        opener.open.side_effect = error

        with self.assertRaises(ApiTransportError):
            ApiClient(ClientConfig("http://localhost/api/alpha"), opener).request(
                "GET", "health"
            )
        body.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
