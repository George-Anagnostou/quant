"""Stored-data prerequisites for a dated portfolio investigation; no risk math."""
from datetime import date, datetime, timedelta
from typing import Literal

from quant.calendars import CALENDAR_VERSION, latest_completed_session, sessions
from quant.contracts import StrictModel
from quant.market_store import MarketDataRepository
from quant.workflows import PERIODS

Status = Literal["ready", "limited", "blocked", "not_applicable"]
Capability = Literal["valuation", "accountValue", "historicalRisk"]


class ReadinessIssue(StrictModel):
    code: str
    message: str
    symbols: list[str]
    affects: list[Capability]
    action: str


class CapabilityReadiness(StrictModel):
    status: Status
    issueCodes: list[str]


class SecurityReadiness(StrictModel):
    symbol: str
    roles: list[Literal["holding", "benchmark"]]
    currency: str | None
    calendar: str | None
    latestPriceDate: date | None
    usablePricePoints: int | None
    missingSessionCount: int | None
    missingSessionSample: list[date]


class ReadinessData(StrictModel):
    methodologyVersion: Literal["portfolio-readiness-v1"]
    calendarVersion: str
    snapshotId: str
    runId: str | None
    datasetSha256: str | None
    date: date
    period: str
    benchmark: str
    provider: Literal["yahoo"]
    priceBasis: str
    expectedSession: date | None
    historyStart: date | None
    requiredPricePoints: int
    status: Status
    checks: dict[Capability, CapabilityReadiness]
    securities: list[SecurityReadiness]
    warnings: list[ReadinessIssue]
    limitations: list[str]


def portfolio_readiness(path, snapshot, snapshot_id, period="1y", benchmark="SPY",
                        *, now: datetime | None = None, run_id=None, dataset_sha256=None):
    if period not in PERIODS:
        raise ValueError("Unsupported risk period")
    as_of = date.fromisoformat(snapshot["date"])
    holdings = {position["symbol"]: position for position in snapshot["positions"]}
    symbols = list(dict.fromkeys([*holdings, benchmark])) if holdings else []
    required = PERIODS[period] + 1
    issues = []
    states = {"valuation": "ready", "accountValue": "ready",
              "historicalRisk": "ready" if holdings else "not_applicable"}

    def issue(code, message, affected, action, selected=(), severity="blocked"):
        issues.append({"code": code, "message": message, "symbols": list(selected),
                       "affects": affected, "action": action})
        for check in affected:
            if states[check] != "blocked" and states[check] != "not_applicable":
                states[check] = severity

    expected = []
    try:
        end = min(as_of, latest_completed_session(now))
        start = max(date(2010, 1, 1), end - timedelta(days=required * 2 + 30))
        expected = sessions(start, end)[-required:]
        if len(expected) < required and holdings:
            issue("calendar_history_limit", "Requested history exceeds the supported calendar.",
                  ["historicalRisk"], "Choose a shorter period within the documented calendar bounds.")
    except ValueError:
        issue("unsupported_analysis_date", "The snapshot date is outside the supported calendar.",
              ["valuation", "accountValue", "historicalRisk"], "Use a supported date or add a verified calendar version.")
    metadata, frame = MarketDataRepository(path, read_only=True).readiness_inputs(
        symbols, expected[0] if expected else as_of, expected[-1] if expected else as_of)
    observations = {}
    for symbol, day in frame.iter_rows():
        observations.setdefault(symbol, set()).add(day)
    expected_set = set(expected)
    securities = []
    for symbol in symbols:
        meta = metadata.get(symbol, {})
        verified = meta.get("calendar") == "XNYS" and meta.get("currency") == "USD"
        missing = sorted(expected_set - observations.get(symbol, set()))
        latest = meta.get("latest_price_date")
        is_holding = symbol in holdings
        roles = (["holding"] if is_holding else []) + (["benchmark"] if symbol == benchmark else [])
        securities.append({"symbol": symbol, "roles": roles,
            "currency": meta.get("currency"), "calendar": meta.get("calendar"),
            "latestPriceDate": latest, "usablePricePoints": len(expected_set & observations.get(symbol, set())) if verified and expected else None,
            "missingSessionCount": len(missing) if verified and expected else None,
            "missingSessionSample": missing[:20] if verified else []})
        if not verified:
            issue("unverified_security_metadata", "Joint risk requires verified USD/XNYS metadata.",
                  ["historicalRisk"], "Verify currency and venue, then explicitly import security metadata.", [symbol])
            if is_holding:
                issue("unverified_valuation_metadata", "Price currency or the valuation calendar is unverified.",
                      ["valuation", "accountValue"], "Verify the security's price currency and venue before interpreting values.",
                      [symbol], "limited")
        if is_holding and (holdings[symbol]["currency"] != "USD" or
                           meta.get("currency") not in {None, holdings[symbol]["currency"]}):
            issue("unsupported_position_currency", "FX conversion or a currency correction is required.",
                  ["valuation", "accountValue", "historicalRisk"], "Correct conflicting metadata or add FX support.", [symbol])
        if is_holding and latest is None:
            issue("missing_valuation_price", "No stored price exists on or before the effective session.",
                  ["valuation", "accountValue"], "Request explicit synchronization and inspect its outcome.", [symbol])
        elif is_holding and verified and expected and latest < expected[-1].isoformat():
            issue("stale_valuation_price", "The latest stored price precedes the effective session.",
                  ["valuation", "accountValue"], "Synchronize recent sessions or explicitly acknowledge stale valuation.",
                  [symbol], "limited")
        if verified and missing:
            issue("missing_risk_sessions", "Requested risk needs a usable close and adjusted close for every session.",
                  ["historicalRisk"], "Synchronize the requested history; inspect gaps and shorter listing history if they persist.", [symbol])
    if snapshot["currency"] != "USD":
        issue("unsupported_account_currency", "Account analysis currently supports USD only.",
              ["valuation", "accountValue", "historicalRisk"], "Add explicit FX support before combining currencies.")
    if snapshot["cash"] is None:
        issue("unknown_cash", "Unknown cash prevents a total account valuation.", ["accountValue"],
              "Import a new dated snapshot with cash from the source; do not assume zero.")
    if len(holdings) > 50:
        issue("risk_symbol_limit", "Joint portfolio risk supports at most 50 holdings.",
              ["historicalRisk"], "Extend the bounded method before claiming full-account joint risk.")
    applicable = [state for state in states.values() if state != "not_applicable"]
    status = "ready" if all(state == "ready" for state in applicable) else (
        "limited" if any(state in {"ready", "limited"} for state in applicable) else "blocked")
    result = {"methodologyVersion": "portfolio-readiness-v1", "calendarVersion": CALENDAR_VERSION,
        "snapshotId": snapshot_id, "runId": run_id, "datasetSha256": dataset_sha256,
        "date": as_of, "period": period, "benchmark": benchmark, "provider": "yahoo",
        "priceBasis": "close_for_valuation_close_and_adjusted_close_for_risk_coverage",
        "expectedSession": expected[-1] if expected else None, "historyStart": expected[0] if expected else None,
        "requiredPricePoints": required, "status": status,
        "checks": {name: {"status": state, "issueCodes": sorted({i["code"] for i in issues if name in i["affects"]})}
                   for name, state in states.items()},
        "securities": securities, "warnings": issues,
        "limitations": ["Readiness checks data prerequisites, not investment suitability or statistical significance.",
                        "Risk readiness requires complete history through the effective session; a review may still return older, explicitly stale results.",
                        "Live readiness can change during later ingestion; a frozen-run check uses its retained dataset and creation time.",
                        "Historical risk is a holdings backcast, not actual account performance."]}
    return ReadinessData.model_validate(result).model_dump(mode="json")
