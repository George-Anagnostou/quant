"""Small broker-parser registry. Add a second broker by registering another parser."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol

from quant.portfolio_contracts import PortfolioWrite


@dataclass(frozen=True)
class ParsedBrokerImport:
    """Normalized tax-lot export mapped into a portfolio write batch."""

    broker: str
    account: str
    effective_date: date
    source: str
    import_key: str
    actions: list[dict]
    warnings: list[str] = field(default_factory=list)
    content_digest: str = ""

    def to_write(self, expected_revision: int, reason: str | None = None) -> PortfolioWrite:
        return PortfolioWrite(
            importKey=self.import_key,
            expectedRevision=expected_revision,
            effectiveDate=self.effective_date,
            source=self.source,
            reason=reason or f"Import {self.broker} tax-lot export for {self.account}",
            actions=self.actions,
        )


class BrokerParser(Protocol):
    name: str

    def parse(self, path: Path, account: str) -> ParsedBrokerImport:
        """Parse a broker export into portfolio actions for the named account."""


BROKERS: dict[str, BrokerParser] = {}


def register_parser(parser: BrokerParser) -> BrokerParser:
    BROKERS[parser.name] = parser
    return parser


def get_parser(name: str) -> BrokerParser:
    try:
        return BROKERS[name]
    except KeyError as error:
        known = ", ".join(sorted(BROKERS)) or "(none)"
        raise ValueError(f"Unknown broker {name!r}; supported: {known}") from error


def list_brokers() -> list[str]:
    return sorted(BROKERS)


def _ensure_jpmorgan_registered() -> None:
    # Import side effect registers the built-in parser.
    from quant.importers import jpmorgan as _jpmorgan  # noqa: F401


_ensure_jpmorgan_registered()
