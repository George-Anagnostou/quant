from collections.abc import Iterable
from datetime import date, datetime, time
import math
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from lxml import html as lxml_html
import polars as pl
import yfinance as yf


S_AND_P_500_CONSTITUENTS_URL = (
    "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
)
MARKET_DATA_SCHEMA = {
    "Date": pl.Date,
    "Symbol": pl.String,
    "Open": pl.Float64,
    "High": pl.Float64,
    "Low": pl.Float64,
    "Close": pl.Float64,
    "Adjusted Close": pl.Float64,
    "Last Price": pl.Float64,
    "Volume": pl.Int64,
}
EASTERN_TIME = ZoneInfo("America/New_York")
EOD_SETTLEMENT_TIME = time(20, 0)


def get_sp500_constituents() -> pl.DataFrame:
    request = Request(
        S_AND_P_500_CONSTITUENTS_URL,
        headers={"User-Agent": "quant/0.1 (portfolio research)"},
    )
    with urlopen(request, timeout=30) as response:
        html = response.read()

    document = lxml_html.fromstring(html)
    table = document.get_element_by_id("constituents")
    rows = []
    for row in table.xpath(".//tbody/tr"):
        cells = row.xpath("./td")
        if len(cells) < 2:
            continue
        rows.append(
            {
                "Symbol": "".join(cells[0].itertext()).strip(),
                "Company": "".join(cells[1].itertext()).strip(),
            }
        )

    return pl.DataFrame(
        rows,
        schema={"Symbol": pl.String, "Company": pl.String},
    )


def get_market_history(
    symbols: Iterable[str],
    start: date | None = None,
) -> pl.DataFrame:
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        return pl.DataFrame(schema=MARKET_DATA_SCHEMA)

    # Yahoo uses dashes for share classes while the index uses dots.
    yahoo_to_index_symbol = {symbol.replace(".", "-"): symbol for symbol in symbols}
    download_options = {
        "tickers": list(yahoo_to_index_symbol),
        "interval": "1d",
        "auto_adjust": False,
        "progress": False,
        "threads": True,
        "group_by": "column",
    }
    if start is None:
        download_options["period"] = "5d"
    else:
        download_options["start"] = start
    history = yf.download(**download_options)
    if history.empty:
        raise RuntimeError(f"No market data returned for: {', '.join(symbols)}")

    fields = {
        "Open": history["Open"],
        "High": history["High"],
        "Low": history["Low"],
        "Close": history["Close"],
        "Adjusted Close": history["Adj Close"],
        "Volume": history["Volume"],
    }
    closes = fields["Close"]
    rows = []
    for yahoo_symbol, index_symbol in yahoo_to_index_symbol.items():
        if yahoo_symbol not in closes.columns:
            continue

        prices = closes[yahoo_symbol].dropna()
        for trading_date, price in prices.items():
            session_date = (
                trading_date.date()
                if hasattr(trading_date, "date")
                else trading_date
            )
            if not _is_completed_daily_bar(session_date):
                continue
            values = {
                name: columns.at[trading_date, yahoo_symbol]
                for name, columns in fields.items()
            }
            rows.append(
                {
                    "Date": session_date,
                    "Symbol": index_symbol,
                    "Open": _optional_float(values["Open"]),
                    "High": _optional_float(values["High"]),
                    "Low": _optional_float(values["Low"]),
                    "Close": float(price),
                    "Adjusted Close": _optional_float(values["Adjusted Close"]),
                    "Last Price": float(price),
                    "Volume": (
                        None
                        if values["Volume"] is None
                        or math.isnan(float(values["Volume"]))
                        else int(values["Volume"])
                    ),
                }
            )

    return pl.DataFrame(
        rows,
        schema=MARKET_DATA_SCHEMA,
    ).sort(["Date", "Symbol"])


def get_latest_market_data(symbols: Iterable[str]) -> pl.DataFrame:
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        return pl.DataFrame(
            schema={
                "Symbol": pl.String,
                "Last Price": pl.Float64,
                "Volume": pl.Int64,
            }
        )

    history = get_market_history(symbols)
    latest = (
        history.sort("Date")
        .group_by("Symbol", maintain_order=True)
        .last()
        .drop("Date")
    )
    return pl.DataFrame({"Symbol": symbols}).join(latest, on="Symbol", how="left")


def latest_market_snapshot(history: pl.DataFrame) -> pl.DataFrame:
    return (
        history.sort(["Symbol", "Date"])
        .group_by("Symbol", maintain_order=True)
        .last()
        .select(history.columns)
    )


def get_sp500_market_data() -> pl.DataFrame:
    return latest_market_snapshot(get_sp500_market_history())


def get_sp500_market_history() -> pl.DataFrame:
    constituents = get_sp500_constituents()
    history = get_market_history(constituents.get_column("Symbol").to_list())
    return constituents.join(
        history,
        on="Symbol",
        how="left",
        validate="1:m",
    ).select(
        "Date",
        "Symbol",
        "Company",
        "Open",
        "High",
        "Low",
        "Close",
        "Adjusted Close",
        "Last Price",
        "Volume",
    )


def _optional_float(value: object) -> float | None:
    if value is None or math.isnan(float(value)):
        return None
    return float(value)


def _is_completed_daily_bar(
    session_date: date,
    now: datetime | None = None,
) -> bool:
    current = now.astimezone(EASTERN_TIME) if now else datetime.now(EASTERN_TIME)
    if session_date < current.date():
        return True
    return session_date == current.date() and current.time() >= EOD_SETTLEMENT_TIME
