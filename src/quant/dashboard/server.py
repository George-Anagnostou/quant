from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, FiniteFloat

from quant.dashboard.services import DashboardService
from quant.dashboard.api_v1 import app as api_v1_app


app = FastAPI(title="Quant Dashboard API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/api/v1", api_v1_app)

_dashboard_service = DashboardService()


class WatchlistAdd(BaseModel):
    symbol: str


class HoldingIn(BaseModel):
    symbol: str
    shares: FiniteFloat
    costBasis: FiniteFloat
    account: str | None = None
    assetClass: str | None = None
    sector: str | None = None
    acquired: str | None = None


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/quotes")
def quotes(
    symbols: str = Query(..., description="Comma-separated symbols"),
    refresh: bool = False,
) -> dict:
    parsed = [symbol.strip().upper() for symbol in symbols.split(",") if symbol.strip()]
    try:
        return _clean_json(_dashboard_service.quotes(parsed, refresh))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.get("/api/quote/{symbol}")
def quote(symbol: str, refresh: bool = False) -> dict:
    try:
        return _clean_json(_dashboard_service.quote(symbol, refresh))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.get("/api/watchlist")
def watchlist_get() -> dict:
    return {"symbols": _dashboard_service.watchlist()}


@app.post("/api/watchlist")
def watchlist_add(body: WatchlistAdd) -> dict:
    try:
        return {"symbols": _dashboard_service.add_watchlist(body.symbol)}
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.delete("/api/watchlist/{symbol}")
def watchlist_remove(symbol: str) -> dict:
    return {"symbols": _dashboard_service.remove_watchlist(symbol)}


@app.get("/api/holdings")
def holdings_list(refresh: bool = False) -> dict:
    try:
        return _clean_json(_dashboard_service.holdings(refresh))
    except (RuntimeError, ValueError) as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/holdings")
def holdings_add(body: HoldingIn) -> dict:
    try:
        return _dashboard_service.add_holding(
            body.symbol,
            body.shares,
            body.costBasis,
            body.account,
            body.assetClass,
            body.sector,
            body.acquired,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.delete("/api/holdings/{holding_id}")
def holdings_remove(holding_id: str) -> dict:
    return {"removed": int(_dashboard_service.remove_holding(holding_id))}


@app.get("/api/analysis/{symbol}")
def technical_analysis(
    symbol: str,
    windows: str = Query(..., description="Comma-separated trading sessions"),
    price: str = Query(..., pattern="^(close|adjusted)$"),
    refresh: bool = False,
) -> dict:
    try:
        parsed_windows = list(
            dict.fromkeys(int(value.strip()) for value in windows.split(","))
        )
        if not parsed_windows or any(window <= 0 for window in parsed_windows):
            raise ValueError("Windows must be positive integers")
        return _clean_json(
            _dashboard_service.market_analysis(
                symbol, parsed_windows, price, refresh
            )
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.get("/api/search")
def security_search(
    query: str,
    limit: int = 10,
    remote: bool = False,
) -> dict:
    return _service_response(
        lambda: _dashboard_service.security_search(query, limit, remote)
    )


@app.get("/api/risk")
def symbol_risk(
    symbols: str = Query(..., description="Comma-separated symbols"),
    period: str = "1y",
    benchmark: str = "SPY",
    refresh: bool = False,
) -> dict:
    parsed = [symbol.strip() for symbol in symbols.split(",") if symbol.strip()]
    return _service_response(
        lambda: _dashboard_service.symbol_risk(
            parsed, period, benchmark, refresh
        )
    )


@app.get("/api/portfolio/risk")
def portfolio_risk(
    period: str = "1y",
    benchmark: str = "SPY",
    refresh: bool = False,
) -> dict:
    return _service_response(
        lambda: _dashboard_service.portfolio_risk(period, benchmark, refresh)
    )


@app.get("/api/screener")
def screener(
    symbols: str | None = None,
    period: str = "1y",
    benchmark: str = "SPY",
    refresh: bool = False,
) -> dict:
    parsed = (
        [symbol.strip() for symbol in symbols.split(",") if symbol.strip()]
        if symbols is not None
        else None
    )
    return _service_response(
        lambda: _dashboard_service.screener(
            parsed, period, benchmark, refresh
        )
    )


@app.get("/api/research/{symbol}/profile")
def research_profile(symbol: str) -> dict:
    return _service_response(lambda: _dashboard_service.research_profile(symbol))


@app.get("/api/research/{symbol}/analyst")
def research_analyst(symbol: str) -> dict:
    return _service_response(lambda: _dashboard_service.research_analyst(symbol))


@app.get("/api/research/{symbol}/earnings")
def research_earnings(symbol: str, limit: int = 12) -> dict:
    return _service_response(
        lambda: _dashboard_service.research_earnings(symbol, limit)
    )


@app.get("/api/research/{symbol}/options")
def research_options(
    symbol: str,
    expiration: str | None = None,
    limit: int = 1_000,
) -> dict:
    return _service_response(
        lambda: _dashboard_service.research_options(
            symbol, expiration, limit
        )
    )


@app.get("/api/research/{symbol}/news")
def research_news(symbol: str, limit: int = 20) -> list[dict]:
    return _service_response(
        lambda: _dashboard_service.research_news(symbol, limit)
    )


@app.get("/api/research/{symbol}/history")
def research_history(
    symbol: str,
    period: str = "1y",
    interval: str = "1d",
    limit: int = 500,
) -> list[dict]:
    return _service_response(
        lambda: _dashboard_service.research_history(
            symbol, period, interval, limit
        )
    )


@app.get("/api/research/{symbol}/intraday")
def research_intraday(
    symbol: str,
    period: str = "5d",
    interval: str = "5m",
    limit: int = 500,
) -> list[dict]:
    return _service_response(
        lambda: _dashboard_service.research_intraday(
            symbol, period, interval, limit
        )
    )


def _service_response(operation):
    try:
        return _clean_json(operation())
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


def _clean_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, (int, str, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _clean_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_json(item) for item in value]
    return str(value)


STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


def run() -> None:
    import os

    import uvicorn

    uvicorn.run(
        "quant.dashboard.server:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8001")),
        reload=False,
    )
