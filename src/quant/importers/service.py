"""Run a broker import through the existing portfolio preview/apply path."""

from __future__ import annotations

from pathlib import Path

from quant.database import DEFAULT_DATABASE_PATH, LOCAL_ADMIN_USER_ID, is_database_initialized
from quant.importers.base import ParsedBrokerImport, get_parser
from quant.portfolio_contracts import PortfolioWrite
from quant.portfolio_service import PortfolioService
from quant.record_store import RecordRepository
from quant.user_data import UserDataRepository


def parse_broker_csv(path: Path, account: str, broker: str = "jpmorgan") -> ParsedBrokerImport:
    parser = get_parser(broker)
    return parser.parse(path, account)


def run_import(
    path: Path,
    account: str,
    *,
    broker: str = "jpmorgan",
    apply: bool = False,
    database: Path = DEFAULT_DATABASE_PATH,
    user_id: str = LOCAL_ADMIN_USER_ID,
) -> dict:
    """Parse a broker CSV and preview or apply it against the portfolio store.

    Default is preview (no writes). Apply uses the same ``PortfolioWrite`` path as
    ``quant portfolio apply``. The ``importKey`` is a digest of the file bytes so
    retrying the identical file is idempotent.
    """
    parsed = parse_broker_csv(path, account, broker=broker)
    repository = UserDataRepository(database, user_id)
    prior = None
    if is_database_initialized(database):
        prior = RecordRepository(database, user_id).by_key("portfolio_write", parsed.import_key)

    if prior is not None:
        # Replay the exact stored payload so content identity matches.
        body = PortfolioWrite.model_validate(prior["payload"])
        result = repository.preview_portfolio(body)
    else:
        state = repository.portfolio_state()
        body = parsed.to_write(expected_revision=state["revision"])
        service = PortfolioService(database, user_id)
        result = service.apply(body) if apply else service.preview(body)

    already_applied = bool(result.get("alreadyApplied"))
    return {
        "broker": parsed.broker,
        "account": parsed.account,
        "importKey": parsed.import_key,
        "effectiveDate": parsed.effective_date.isoformat(),
        "contentDigest": parsed.content_digest,
        "warnings": list(parsed.warnings) + list(result.get("warnings") or []),
        "applied": bool(apply) and not already_applied,
        "alreadyApplied": already_applied,
        "result": result,
        "batch": body.model_dump(mode="json"),
    }
