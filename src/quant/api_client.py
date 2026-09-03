from __future__ import annotations

import json
import math
import socket
from http.client import HTTPException
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class ApiClientError(Exception):
    pass


class ApiTransportError(ApiClientError):
    pass


class ApiTimeoutError(ApiClientError):
    pass


class ApiProtocolError(ApiClientError):
    pass


class ApiHttpError(ApiClientError):
    def __init__(self, status: int, detail: Any) -> None:
        super().__init__(f"API request failed with HTTP {status}")
        self.status = status
        self.detail = detail


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class ClientConfig:
    base_url: str
    timeout: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.base_url, str) or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in self.base_url
        ):
            raise ValueError("API base URL cannot contain whitespace or control characters")
        parsed = urlsplit(self.base_url)
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError("API base URL contains an invalid port") from error
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or not parsed.hostname
            or any(character.isspace() for character in parsed.netloc)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or port is not None
            and not 1 <= port <= 65_535
        ):
            raise ValueError(
                "API base URL must be an absolute HTTP(S) URL without credentials, query, or fragment"
            )
        if not isinstance(self.timeout, (int, float)) or isinstance(
            self.timeout, bool
        ):
            raise ValueError("API timeout must be a positive number")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("API timeout must be a positive number")

    @property
    def normalized_base_url(self) -> str:
        parsed = urlsplit(self.base_url)
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
        )


class ApiClient:
    def __init__(self, config: ClientConfig, opener=None) -> None:
        self.config = config
        self._opener = opener or build_opener(_NoRedirect())

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, object] | None = None,
        body: object | None = None,
    ) -> Any:
        url = f"{self.config.normalized_base_url}/{path.lstrip('/')}"
        if query:
            encoded = urlencode(
                {key: value for key, value in query.items() if value is not None},
                doseq=True,
            )
            if encoded:
                url = f"{url}?{encoded}"
        payload = None
        headers = {"Accept": "application/json"}
        if body is not None:
            payload = json.dumps(
                body,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=payload, headers=headers, method=method.upper())
        try:
            response = self._opener.open(request, timeout=self.config.timeout)
            with response:
                status = response.getcode()
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            try:
                try:
                    raw = error.read(MAX_RESPONSE_BYTES + 1)
                except (TimeoutError, socket.timeout) as read_error:
                    raise ApiTimeoutError("API error response timed out") from read_error
                except (HTTPException, OSError) as read_error:
                    raise ApiTransportError(
                        f"API error response failed: {read_error}"
                    ) from read_error
            finally:
                error.close()
            if 300 <= error.code < 400:
                raise ApiProtocolError("API redirects are not allowed") from error
            raise ApiHttpError(error.code, _decode_error(raw)) from error
        except (TimeoutError, socket.timeout) as error:
            raise ApiTimeoutError("API request timed out") from error
        except URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise ApiTimeoutError("API request timed out") from error
            raise ApiTransportError(f"API connection failed: {error.reason}") from error
        except (HTTPException, OSError) as error:
            raise ApiTransportError(f"API response failed: {error}") from error
        if not 200 <= status < 300:
            raise ApiProtocolError(f"Unexpected API status: {status}")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ApiProtocolError("API response exceeds 16 MiB")
        if not raw:
            raise ApiProtocolError("API returned an empty response")
        try:
            payload = _load_json(raw)
        except (UnicodeDecodeError, ValueError, RecursionError) as error:
            raise ApiProtocolError("API returned invalid JSON") from error
        if (
            not isinstance(payload, dict)
            or not {"data", "meta", "warnings"}.issubset(payload)
            or not isinstance(payload["meta"], dict)
            or payload["meta"].get("apiVersion") != "v1"
            or not isinstance(payload["meta"].get("generatedAt"), str)
            or not isinstance(payload["meta"].get("freshness"), dict)
            or payload["meta"]["freshness"].get("status")
            not in {"current", "stale", "unknown"}
            or not isinstance(payload["warnings"], list)
            or any(
                not isinstance(warning, dict)
                or not isinstance(warning.get("code"), str)
                or not isinstance(warning.get("message"), str)
                or not isinstance(warning.get("symbols", []), list)
                or any(not isinstance(symbol, str) for symbol in warning.get("symbols", []))
                for warning in payload["warnings"]
            )
        ):
            raise ApiProtocolError("API returned an invalid v1 envelope")
        return payload


def _decode_error(raw: bytes) -> Any:
    if len(raw) > MAX_RESPONSE_BYTES:
        return {"message": "API error response exceeds 16 MiB"}
    try:
        payload = _load_json(raw)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return {"message": "API request failed"}
    return payload.get("detail", payload) if isinstance(payload, dict) else payload


def _load_json(raw: bytes) -> Any:
    """Only accept unambiguous JSON that can be emitted again as strict JSON."""
    def finite_float(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Non-finite JSON number")
        return number

    def reject_constant(value: str):
        raise ValueError(f"Invalid JSON constant: {value}")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON object key")
            result[key] = value
        return result

    return json.loads(
        raw,
        parse_float=finite_float,
        parse_constant=reject_constant,
        object_pairs_hook=unique_object,
    )
