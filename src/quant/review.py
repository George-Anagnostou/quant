"""CLI portfolio review report. Reuses analysis, tax-lot fields, and risk helpers."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from quant.calendars import latest_completed_session
from quant.database import DEFAULT_DATABASE_PATH, LOCAL_ADMIN_USER_ID
from quant.market_store import MarketDataRepository
from quant.portfolio import analyze_portfolio_risk
from quant.portfolio_service import PortfolioService
from quant.user_data import UserDataRepository

# Descriptive IRS-style bucket only; this report does not compute tax liability.
LONG_TERM_HOLDING_DAYS = 365
DEFAULT_RISK_PERIOD = "1mo"
DEFAULT_REPORTS_DIR = Path("data/reports")


def _pct(fraction: float | None) -> str:
    if fraction is None:
        return "n/a"
    return f"{fraction * 100:.2f}%"


def _money(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.2f}"


def _term_buckets(lots: list[dict]) -> dict:
    short_gain = short_cost = long_gain = long_cost = 0.0
    short_lots = long_lots = unknown_lots = 0
    for lot in lots:
        days = lot.get("holdingDays")
        gain = lot.get("unrealizedGain")
        cost = float(lot.get("totalCost") or 0.0)
        if days is None or gain is None:
            unknown_lots += 1
            continue
        if int(days) > LONG_TERM_HOLDING_DAYS:
            long_lots += 1
            long_gain += float(gain)
            long_cost += cost
        else:
            short_lots += 1
            short_gain += float(gain)
            short_cost += cost
    return {
        "short": {"lots": short_lots, "unrealizedGain": short_gain, "totalCost": short_cost},
        "long": {"lots": long_lots, "unrealizedGain": long_gain, "totalCost": long_cost},
        "unknownLots": unknown_lots,
    }


def _metric_lines(metrics: dict) -> list[str]:
    def value(*names):
        for name in names:
            if name in metrics and metrics[name] is not None:
                return metrics[name]
        return None

    observations = value("Observations")
    cumulative = value("Cumulative Return")
    vol = value("Annualized Volatility")
    sharpe = value("Sharpe Ratio")
    sortino = value("Sortino Ratio")
    max_dd = value("Max Drawdown")
    beta = value("Beta")
    alpha = value("Annualized Alpha")
    lines = [f"- Observations: {observations if observations is not None else 'n/a'}"]
    lines.append(
        f"- Cumulative return (current-share): {_pct(float(cumulative)) if cumulative is not None else 'n/a'}"
    )
    lines.append(f"- Annualized volatility: {_pct(float(vol)) if vol is not None else 'n/a'}")
    lines.append(f"- Sharpe (rf=0): {f'{float(sharpe):.3f}' if sharpe is not None else 'n/a'}")
    lines.append(f"- Sortino (rf=0): {f'{float(sortino):.3f}' if sortino is not None else 'n/a'}")
    lines.append(f"- Max drawdown: {_pct(float(max_dd)) if max_dd is not None else 'n/a'}")
    lines.append(f"- Beta vs SPY: {f'{float(beta):.3f}' if beta is not None else 'n/a'}")
    lines.append(
        f"- Annualized alpha vs SPY: {_pct(float(alpha)) if alpha is not None else 'n/a'}"
    )
    return lines


def _risk_section(service: PortfolioService, analysis: dict, warnings: list[str]) -> list[str]:
    lines = [
        "## Risk vs SPY",
        "",
        f"Current-share backcast over stored prices (`period={DEFAULT_RISK_PERIOD}`).",
        "This is not actual account TWR/MWR performance.",
        "",
    ]
    positions = analysis.get("positions") or []
    if not positions:
        warnings.append("Risk vs SPY skipped: no security positions")
        lines.extend(["_No security positions to analyze._", ""])
        return lines

    frame = pl.DataFrame(
        {
            "Symbol": [row["symbol"] for row in positions],
            "Quantity": [float(row["quantity"]) for row in positions],
        }
    )
    try:
        result = analyze_portfolio_risk(
            frame,
            period=DEFAULT_RISK_PERIOD,
            benchmark_symbol="SPY",
            repository=MarketDataRepository(service.repository.path, read_only=True),
            refresh=False,
            fetch_missing=False,
        )
    except (RuntimeError, ValueError) as error:
        warnings.append(f"Risk vs SPY unavailable from stored prices: {error}")
        lines.extend([f"_Unavailable:_ {error}", ""])
        return lines

    metrics = result["metrics"].to_dicts()[0]
    lines.extend(_metric_lines(metrics))
    lines.append("")
    return lines


def _prior_revision_diff(repository: UserDataRepository) -> list[str]:
    lines = ["## Diff vs prior revision", ""]
    history = repository.portfolio_history(limit=1000, offset=0)
    if not history:
        lines.extend(["_No prior revision; portfolio has no applied changes yet._", ""])
        return lines

    latest = history[-1]
    before = latest.get("before") or {"revision": 0, "accounts": [], "lots": []}
    after = latest.get("after") or {"revision": 0, "accounts": [], "lots": []}

    before_lots = {lot["id"]: lot for lot in before.get("lots") or []}
    after_lots = {lot["id"]: lot for lot in after.get("lots") or []}
    added = sorted(set(after_lots) - set(before_lots))
    removed = sorted(set(before_lots) - set(after_lots))
    changed = [
        lot_id
        for lot_id in sorted(set(before_lots) & set(after_lots))
        if (
            before_lots[lot_id].get("quantity"),
            before_lots[lot_id].get("totalCost"),
            before_lots[lot_id].get("symbol"),
        )
        != (
            after_lots[lot_id].get("quantity"),
            after_lots[lot_id].get("totalCost"),
            after_lots[lot_id].get("symbol"),
        )
    ]

    before_cash = {account["name"]: account.get("cash") for account in before.get("accounts") or []}
    after_cash = {account["name"]: account.get("cash") for account in after.get("accounts") or []}
    cash_names = sorted(set(before_cash) | set(after_cash))

    lines.extend(
        [
            f"- Latest change importKey: `{latest.get('importKey')}`",
            f"- Revision {before.get('revision')} → {after.get('revision')}",
            f"- Lots added: {len(added)}; removed: {len(removed)}; quantity/cost changed: {len(changed)}",
            "",
            "### Cash by account",
            "",
        ]
    )
    if not cash_names:
        lines.append("_No accounts in prior or current state._")
    else:
        lines.extend(["| Account | Prior cash | Current cash |", "| --- | ---: | ---: |"])
        for name in cash_names:
            prior = before_cash.get(name)
            now = after_cash.get(name)
            prior_text = "unknown" if prior is None else _money(float(prior))
            now_text = "unknown" if now is None else _money(float(now))
            lines.append(f"| {name} | {prior_text} | {now_text} |")
    lines.append("")

    if added or removed or changed:
        lines.extend(["### Lot identity changes (ids)", ""])
        if added:
            shown = ", ".join(f"`{item}`" for item in added[:20])
            lines.append(f"- Added: {shown}")
            if len(added) > 20:
                lines.append(f"  - …and {len(added) - 20} more")
        if removed:
            lines.append("- Removed: " + ", ".join(f"`{item}`" for item in removed[:20]))
        if changed:
            lines.append("- Changed: " + ", ".join(f"`{item}`" for item in changed[:20]))
        lines.append("")
    else:
        lines.extend(["_No lot identity or quantity/cost changes in the latest applied batch._", ""])
    return lines


def build_review_report(
    *,
    database: Path = DEFAULT_DATABASE_PATH,
    as_of: date | None = None,
    user_id: str = LOCAL_ADMIN_USER_ID,
) -> tuple[str, dict]:
    """Build a markdown review from existing portfolio analysis / tax-lot / risk paths."""
    day = as_of or latest_completed_session()
    repository = UserDataRepository(database, user_id)
    service = PortfolioService(database, user_id)
    warnings: list[str] = []

    analysis = service.analysis(as_of=day)
    tax = service.tax_lots(as_of=day)
    warnings.extend(analysis.get("warnings") or [])
    warnings.extend(tax.get("warnings") or [])

    from quant.crypto_assets import is_crypto_symbol
    from quant.analysis import aggregate_portfolio_values, portfolio_value_summary
    import polars as pl

    raw_lots = tax.get("lots") or analysis.get("lots") or []
    crypto_lots = [lot for lot in raw_lots if is_crypto_symbol(str(lot.get("symbol", "")))]
    lots = [lot for lot in raw_lots if not is_crypto_symbol(str(lot.get("symbol", "")))]
    if crypto_lots:
        warnings.append(
            f"Omitted {len(crypto_lots)} crypto lot(s) from stock/ETF review; crypto review is a later Phase 1B step."
        )
    if lots:
        frame = pl.DataFrame(lots)
        # Rebuild equity-only summary/positions from the already-valued lot rows.
        accounts = analysis.get("accounts") or []
        summary = portfolio_value_summary(frame, accounts)
        positions = aggregate_portfolio_values(frame, "symbol", summary["accountValue"]).to_dicts()
        quote_fields = {
            row["symbol"]: row
            for row in frame.select("symbol", "lastPrice", "priceDate", "dayReturn").unique().to_dicts()
        }
        for position in positions:
            position.update(quote_fields.get(position["symbol"], {}))
    else:
        accounts = analysis.get("accounts") or []
        summary = {
            "totalCost": 0.0,
            "pricedSecuritiesValue": 0.0,
            "securitiesValue": 0.0,
            "knownCash": sum(float(a["cash"]) for a in accounts if a.get("cash") is not None),
            "cash": (
                sum(float(a["cash"]) for a in accounts if a.get("cash") is not None)
                if accounts and all(a.get("cash") is not None for a in accounts)
                else None
            ),
            "accountValue": None,
            "unrealizedGain": 0.0,
            "unrealizedReturn": None,
            "unpricedLotCount": 0,
        }
        if summary["cash"] is not None:
            summary["accountValue"] = float(summary["cash"])
            summary["securitiesValue"] = 0.0
        positions = []
    positions = sorted(
        positions,
        key=lambda row: (row.get("weight") is None, -(row.get("weight") or 0.0), row["symbol"]),
    )
    terms = _term_buckets(lots)

    lines: list[str] = [
        f"# Portfolio review — {day.isoformat()}",
        "",
        f"- Database revision: {analysis['revision']}",
        "- Currency: USD only",
        f"- Accounts: {len(accounts)}",
        f"- Active lots: {len(lots)}",
        f"- Security positions: {len(positions)}",
        "",
        "## Combined view",
        "",
        f"- Securities value: {_money(summary.get('securitiesValue'))}",
        f"- Known cash: {_money(summary.get('cash'))}",
        f"- Account value (securities + cash): {_money(summary.get('accountValue'))}",
        f"- Remaining cost basis: {_money(summary.get('totalCost'))}",
        f"- Unrealized gain: {_money(summary.get('unrealizedGain'))} ({_pct(summary.get('unrealizedReturn'))})",
        f"- Unpriced lots: {summary.get('unpricedLotCount', 0)}",
        "",
        "### Accounts",
        "",
        "| Account | Cash | As of | Type |",
        "| --- | ---: | --- | --- |",
    ]
    for account in accounts:
        cash = account.get("cash")
        lines.append(
            f"| {account['name']} | "
            f"{'unknown' if cash is None else _money(float(cash))} | "
            f"{account.get('asOf') or 'n/a'} | "
            f"{account.get('accountType') or ''} |"
        )
    lines.append("")

    lines.extend(["## Allocation and concentration", ""])
    if not positions:
        lines.extend(["_No priced security positions._", ""])
    else:
        lines.extend(
            [
                "Weights are fractions of securities + known cash (shown as %).",
                "",
                "| Symbol | Qty | Market value | Weight | Unrealized |",
                "| --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for row in positions[:15]:
            lines.append(
                f"| {row['symbol']} | {row['quantity']:g} | {_money(row.get('marketValue'))} | "
                f"{_pct(row.get('weight'))} | {_money(row.get('unrealizedGain'))} |"
            )
        if len(positions) > 15:
            lines.append(f"| … | | | | {len(positions) - 15} more positions |")
        lines.append("")
        top = positions[0]
        lines.append(
            f"- Largest position: **{top['symbol']}** at {_pct(top.get('weight'))} of account value"
        )
        lines.append("")
        by_account = analysis.get("allocations", {}).get("account") or []
        if by_account:
            lines.extend(
                ["### By account", "", "| Account | Weight | Market value |", "| --- | ---: | ---: |"]
            )
            for row in by_account:
                lines.append(
                    f"| {row['name']} | {_pct(row.get('weight'))} | {_money(row.get('marketValue'))} |"
                )
            lines.append("")

    short = terms["short"]
    long = terms["long"]
    lines.extend(
        [
            "## Short- vs long-term unrealized gains",
            "",
            f"Descriptive holding-period buckets using >{LONG_TERM_HOLDING_DAYS} days as long-term.",
            "Not a tax filing; no rates, wash sales, or liability.",
            "",
            f"- Short-term lots: {short['lots']}; unrealized {_money(short['unrealizedGain'])} on basis {_money(short['totalCost'])}",
            f"- Long-term lots: {long['lots']}; unrealized {_money(long['unrealizedGain'])} on basis {_money(long['totalCost'])}",
            f"- Lots missing holding days or gain: {terms['unknownLots']}",
            "",
        ]
    )

    equity_analysis = {**analysis, "positions": positions, "lots": lots, "summary": summary}
    lines.extend(_risk_section(service, equity_analysis, warnings))
    lines.extend(_prior_revision_diff(repository))

    unique_warnings: list[str] = []
    seen: set[str] = set()
    for item in warnings:
        if item not in seen:
            unique_warnings.append(item)
            seen.add(item)
    lines.extend(
        ["## Warnings", ""]
        + (["- (none)"] if not unique_warnings else [f"- {item}" for item in unique_warnings])
        + [""]
    )

    report = "\n".join(lines)
    meta = {
        "date": day.isoformat(),
        "revision": analysis["revision"],
        "warnings": unique_warnings,
        "accountValue": summary.get("accountValue"),
        "lotCount": len(lots),
        "positionCount": len(positions),
    }
    return report, meta


def write_review_report(
    *,
    database: Path = DEFAULT_DATABASE_PATH,
    as_of: date | None = None,
    output: Path | None = None,
    user_id: str = LOCAL_ADMIN_USER_ID,
) -> dict:
    report, meta = build_review_report(database=database, as_of=as_of, user_id=user_id)
    day = date.fromisoformat(meta["date"])
    destination = output or (DEFAULT_REPORTS_DIR / f"review-{day.isoformat()}.md")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(report)
    return {**meta, "output": str(destination), "bytes": len(report.encode())}
