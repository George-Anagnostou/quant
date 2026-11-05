"""Stable crypto asset IDs and Yahoo daily-bar mappings for Phase 1B groundwork.

Canonical IDs are ETH, SOL, BTC, and PSOL. Free yfinance pairs exist for
ETH-USD, SOL-USD, and BTC-USD. PSOL is an ID placeholder only in this PR
(liquid stake rate tracking comes later).
"""

from __future__ import annotations

from typing import TypedDict


class CryptoAsset(TypedDict):
    yahoo: str | None
    calendar: str
    currency: str
    instrumentType: str


CRYPTO_CALENDAR = "CRYPTO_UTC"

CRYPTO_ASSETS: dict[str, CryptoAsset] = {
    "BTC": {
        "yahoo": "BTC-USD",
        "calendar": CRYPTO_CALENDAR,
        "currency": "USD",
        "instrumentType": "crypto",
    },
    "ETH": {
        "yahoo": "ETH-USD",
        "calendar": CRYPTO_CALENDAR,
        "currency": "USD",
        "instrumentType": "crypto",
    },
    "SOL": {
        "yahoo": "SOL-USD",
        "calendar": CRYPTO_CALENDAR,
        "currency": "USD",
        "instrumentType": "crypto",
    },
    "PSOL": {
        "yahoo": None,
        "calendar": CRYPTO_CALENDAR,
        "currency": "USD",
        "instrumentType": "crypto",
    },
}

# Yahoo ticker -> canonical ID for download results.
YAHOO_TO_CANONICAL = {
    meta["yahoo"]: symbol
    for symbol, meta in CRYPTO_ASSETS.items()
    if meta["yahoo"] is not None
}


def is_crypto_symbol(symbol: str) -> bool:
    text = symbol.strip().upper()
    return text in CRYPTO_ASSETS or text in YAHOO_TO_CANONICAL


def canonical_crypto_symbol(symbol: str) -> str:
    """Map ETH-USD / eth to the stable ID ETH; non-crypto symbols unchanged."""
    text = symbol.strip().upper()
    if text in CRYPTO_ASSETS:
        return text
    return YAHOO_TO_CANONICAL.get(text, text)


def yahoo_symbol_for(symbol: str) -> str | None:
    """Provider ticker for a canonical crypto ID, or None when unsupported."""
    meta = CRYPTO_ASSETS.get(canonical_crypto_symbol(symbol))
    if meta is None:
        return None
    return meta["yahoo"]


def crypto_metadata(symbol: str) -> CryptoAsset | None:
    return CRYPTO_ASSETS.get(canonical_crypto_symbol(symbol))


def priced_crypto_symbols() -> list[str]:
    """Canonical IDs that have a free Yahoo daily pair in this release."""
    return sorted(
        symbol for symbol, meta in CRYPTO_ASSETS.items() if meta["yahoo"] is not None
    )
