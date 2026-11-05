"""JPMorgan taxable brokerage tax-lot CSV parser.

Maps the export described in docs/PORTFOLIO_CASE_STUDY.md into the existing
portfolio batch payload (account / cash / add). USD stocks, ETFs, and cash only.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from quant.importers.base import ParsedBrokerImport, register_parser
from quant.market_data import normalize_symbol

PARSER_NAME = "jpmorgan"
CUSIP_PATTERN = re.compile(r"^[A-Z0-9]{9}$")
CASH_DESCRIPTIONS = {"US DOLLAR", "USD", "CASH"}
SUPPORTED_ASSET_CLASSES = {
    "",
    "EQUITY",
    "EQUITIES",
    "STOCK",
    "STOCKS",
    "ETF",
    "ETFS",
    "MUTUAL FUND",
    "MUTUAL FUNDS",
    "CASH",
    "CASH & CASH EQUIVALENTS",
    "CASH EQUIVALENT",
    "FIXED INCOME",  # warned/skipped unless it is the USD cash row
}


def _file_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _parse_mdy(value: str) -> date:
    text = value.strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date {value!r}; expected M/D/Y")


def _decimal(value: str, field_name: str) -> Decimal:
    text = value.strip().replace(",", "").replace("$", "")
    if text == "":
        raise ValueError(f"Missing {field_name}")
    try:
        amount = Decimal(text)
    except InvalidOperation as error:
        raise ValueError(f"Invalid {field_name}: {value!r}") from error
    return amount


def _find_header(rows: list[list[str]]) -> tuple[int, list[str]]:
    required = {"Ticker", "Quantity", "Cost", "Acquisition Date", "As of"}
    for index, row in enumerate(rows):
        cells = [cell.strip() for cell in row]
        if required.issubset(set(cells)):
            return index, cells
    raise ValueError(
        "JPMorgan tax-lot CSV is missing a header row with Ticker, Quantity, Cost, "
        "Acquisition Date, and As of"
    )


def _row_dict(header: list[str], row: list[str]) -> dict[str, str]:
    padded = list(row) + [""] * max(0, len(header) - len(row))
    return {name: (padded[i] if i < len(padded) else "").strip() for i, name in enumerate(header)}


def _is_cash_row(row: dict[str, str]) -> bool:
    ticker = row.get("Ticker", "").strip()
    description = row.get("Description", "").strip().upper()
    asset_class = row.get("Asset Class", "").strip().upper()
    if not ticker and description in CASH_DESCRIPTIONS:
        return True
    if not ticker and asset_class.startswith("CASH"):
        return True
    if description in CASH_DESCRIPTIONS and (not ticker or ticker.upper() in {"USD", "CASH"}):
        return True
    return False


def _trustworthy_cusip(raw: str) -> str | None:
    cleaned = raw.strip().upper()
    if CUSIP_PATTERN.fullmatch(cleaned):
        return cleaned
    return None


def _original_fields(row: dict[str, str]) -> dict[str, str]:
    keep = (
        "Ticker",
        "Quantity",
        "Cost",
        "Unit Cost",
        "Acquisition Date",
        "Account type",
        "Base CCY",
        "Description",
        "CUSIP",
        "Asset Class",
        "Asset Strategy",
        "As of",
        "Price",
        "Value",
        "Pricing Date",
        "Used/Outstanding",
    )
    return {key: row[key] for key in keep if key in row and row[key] != ""}


class JPMorganTaxLotParser:
    name = PARSER_NAME

    def parse(self, path: Path, account: str) -> ParsedBrokerImport:
        raw = path.read_bytes()
        digest = _file_digest(raw)
        text = raw.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text)))
        if not rows:
            raise ValueError("JPMorgan tax-lot CSV is empty")
        header_index, header = _find_header(rows)
        data_rows = [_row_dict(header, row) for row in rows[header_index + 1 :] if any(cell.strip() for cell in row)]
        if not data_rows:
            raise ValueError("JPMorgan tax-lot CSV has a header but no data rows")

        warnings: list[str] = []
        actions: list[dict] = [
            {
                "action": "account",
                "account": {
                    "name": account,
                    "currency": "USD",
                    "accountType": "Brokerage",
                    "institution": "JPMorgan",
                },
            }
        ]
        cash_amount: Decimal | None = None
        effective_dates: set[date] = set()
        lot_count = 0

        for offset, row in enumerate(data_rows):
            row_number = header_index + 2 + offset  # 1-based file line estimate
            source_lot_id = f"jpmorgan:{digest[:12]}:row:{row_number}"
            currency = (row.get("Base CCY") or "USD").strip().upper() or "USD"
            if currency != "USD":
                raise ValueError(
                    f"Row {row_number}: only USD holdings are supported in Phase 1 "
                    f"(found Base CCY {currency!r})"
                )

            as_of_raw = row.get("As of", "").strip()
            if as_of_raw:
                effective_dates.add(_parse_mdy(as_of_raw))

            if _is_cash_row(row):
                value_raw = row.get("Value") or row.get("Quantity") or ""
                amount = _decimal(value_raw, "cash Value")
                if amount < 0:
                    raise ValueError(f"Row {row_number}: cash Value cannot be negative")
                if cash_amount is not None:
                    warnings.append(f"Row {row_number}: multiple cash rows; summing USD cash balances")
                    cash_amount += amount
                else:
                    cash_amount = amount
                # Used/Outstanding and Unit Cost are intentionally ignored for cash.
                continue

            ticker = row.get("Ticker", "").strip()
            if not ticker:
                warnings.append(f"Row {row_number}: skipped row with no ticker (not recognized as cash)")
                continue

            asset_class = row.get("Asset Class", "").strip()
            asset_upper = asset_class.upper()
            if asset_upper and not any(
                asset_upper == allowed or asset_upper.startswith("CASH")
                for allowed in SUPPORTED_ASSET_CLASSES
                if allowed
            ) and asset_upper not in {"EQUITY", "EQUITIES", "ETF", "ETFS", "STOCK", "STOCKS", "MUTUAL FUND", "MUTUAL FUNDS"}:
                # Reject clearly unsupported classes (options, futures, crypto, etc.)
                if any(token in asset_upper for token in ("OPTION", "FUTURE", "CRYPTO", "DERIVATIVE", "FOREX", "FX")):
                    raise ValueError(
                        f"Row {row_number}: unsupported Asset Class {asset_class!r}; "
                        "Phase 1 accepts USD stocks/ETFs/cash only"
                    )
                warnings.append(
                    f"Row {row_number}: unusual Asset Class {asset_class!r}; importing as a security lot"
                )

            quantity = _decimal(row.get("Quantity", ""), "Quantity")
            if quantity <= 0:
                raise ValueError(f"Row {row_number}: Quantity must be positive")

            # Cost is authoritative; Unit Cost is evidence only (rounding may disagree).
            total_cost = _decimal(row.get("Cost", ""), "Cost")
            if total_cost < 0:
                raise ValueError(f"Row {row_number}: Cost cannot be negative")
            unit_cost_raw = row.get("Unit Cost", "").strip()
            if unit_cost_raw:
                try:
                    unit_cost = _decimal(unit_cost_raw, "Unit Cost")
                    implied = (unit_cost * quantity).quantize(Decimal("0.01"))
                    reported = total_cost.quantize(Decimal("0.01"))
                    if implied != reported:
                        warnings.append(
                            f"Row {row_number}: Unit Cost × Quantity ({implied}) does not equal "
                            f"Cost ({reported}); preserving source Cost"
                        )
                except ValueError:
                    warnings.append(f"Row {row_number}: could not parse Unit Cost {unit_cost_raw!r}")

            acquired_raw = row.get("Acquisition Date", "").strip()
            if not acquired_raw:
                raise ValueError(f"Row {row_number}: security lots require Acquisition Date")
            acquired = _parse_mdy(acquired_raw)

            cusip_raw = row.get("CUSIP", "").strip()
            cusip = _trustworthy_cusip(cusip_raw) if cusip_raw else None
            if cusip_raw and cusip is None:
                warnings.append(
                    f"Row {row_number}: damaged CUSIP {cusip_raw!r} preserved in original and omitted canonically"
                )

            original = _original_fields(row)
            # Never treat Used/Outstanding as lot quantity; Quantity is the lot size.
            if "Used/Outstanding" in original:
                # Keep as evidence only; do not sum across lots.
                pass

            description = row.get("Description") or None
            if description and len(description) > 200:
                warnings.append(f"Row {row_number}: description truncated to 200 characters")
                description = description[:200]
            strategy = row.get("Asset Strategy") or None
            if strategy and len(strategy) > 200:
                strategy = strategy[:200]
            asset_class_value = asset_class or None
            if asset_class_value and len(asset_class_value) > 200:
                asset_class_value = asset_class_value[:200]

            lot: dict = {
                "account": account,
                "symbol": normalize_symbol(ticker),
                "quantity": str(quantity),
                "totalCost": str(total_cost),
                "currency": "USD",
                "acquired": acquired.isoformat(),
                "description": description,
                "assetClass": asset_class_value,
                "strategy": strategy,
                "sector": None,
                "sourceLotId": source_lot_id,
                "original": original,
            }
            if cusip is not None:
                lot["cusip"] = cusip

            price_raw = row.get("Price", "").strip()
            value_raw = row.get("Value", "").strip()
            price_date_raw = row.get("Pricing Date", "").strip()
            if price_raw or value_raw:
                if not price_date_raw:
                    warnings.append(
                        f"Row {row_number}: reported Price/Value without Pricing Date; omitting reported valuation"
                    )
                else:
                    price_date = _parse_mdy(price_date_raw)
                    if price_raw:
                        lot["reportedPrice"] = str(_decimal(price_raw, "Price"))
                    if value_raw:
                        lot["reportedValue"] = str(_decimal(value_raw, "Value"))
                    lot["priceDate"] = price_date.isoformat()

            # Drop None optional fields so PortfolioWrite defaults apply cleanly.
            lot = {key: value for key, value in lot.items() if value is not None}
            actions.append({"action": "add", "lot": lot})
            lot_count += 1

        if not effective_dates:
            raise ValueError("JPMorgan tax-lot CSV has no As of date")
        if len(effective_dates) > 1:
            warnings.append(
                f"Multiple As of dates {sorted(d.isoformat() for d in effective_dates)}; "
                "using the latest as effectiveDate"
            )
        effective_date = max(effective_dates)
        if cash_amount is None:
            warnings.append("No USD cash row found; recording cash as 0.00")
            cash_amount = Decimal("0")
        actions.insert(1, {"action": "cash", "account": account, "amount": str(cash_amount)})

        if lot_count == 0 and cash_amount == 0:
            raise ValueError("JPMorgan tax-lot CSV produced no lots and no cash")

        import_key = f"jpmorgan:{digest}"
        source = f"jpmorgan tax-lot sha256:{digest}"
        return ParsedBrokerImport(
            broker=PARSER_NAME,
            account=account,
            effective_date=effective_date,
            source=source,
            import_key=import_key,
            actions=actions,
            warnings=warnings,
            content_digest=digest,
        )


register_parser(JPMorganTaxLotParser())
