from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Annotated, Any, Generic, Literal, TypeVar

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from quant.dashboard.services import DashboardService
from quant.market_data import EASTERN_TIME


SYMBOL_PATTERN = re.compile(r"^[A-Z0-9^][A-Z0-9.^=_-]{0,31}$")
logger = logging.getLogger(__name__)


class ApiWarning(BaseModel):
    code: str
    message: str
    symbols: list[str] = Field(default_factory=list)


class Freshness(BaseModel):
    status: Literal["current", "stale", "unknown"]
    ageCalendarDays: int | None
    staleAfterCalendarDays: int | None
    latestCompletedSession: str | None = None
    missingSessions: int | None = None


class ApiMeta(BaseModel):
    apiVersion: Literal["alpha"] = "alpha"
    generatedAt: str
    asOf: str | None = None
    retrievedAt: str | None = None
    provider: str | None = None
    priceBasis: str | None = None
    freshness: Freshness
    methodologyVersion: str | None = None
    datasetSha256: str | None = None


Payload = TypeVar("Payload")


class ApiEnvelope(BaseModel, Generic[Payload]):
    data: Payload
    meta: ApiMeta
    warnings: list[ApiWarning] = Field(default_factory=list)


class ApiError(BaseModel):
    code: str
    message: str
    symbols: list[str] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)


class ApiErrorResponse(BaseModel):
    detail: ApiError


class HealthData(BaseModel):
    status: Literal["ok"]


class CoverageData(BaseModel):
    symbol: str
    firstSession: str | None
    lastSession: str | None
    sessionCount: int
    completeOhlcvSessionCount: int
    adjustedCloseSessionCount: int
    retrievedAt: str | None


class DataSummary(BaseModel):
    securityCount: int
    barCount: int
    completeOhlcvBarCount: int
    adjustedCloseBarCount: int
    firstSession: str | None
    lastSession: str | None
    retrievedAt: str | None


class DataStatusData(BaseModel):
    storage: Literal["sqlite"]
    provider: str
    summary: DataSummary
    coverage: list[CoverageData]


class SecurityData(BaseModel):
    symbol: str
    name: str


class SecuritiesData(BaseModel):
    query: str
    results: list[SecurityData]


class QuoteData(BaseModel):
    date: str
    symbol: str
    name: str
    price: float
    previousClose: float | None
    change: float | None
    changeReturn: float | None
    volume: int | None
    currency: Literal["USD"]
    provider: str
    retrievedAt: str


class QuotesData(BaseModel):
    quotes: list[QuoteData]
    missingSymbols: list[str]


class BarData(BaseModel):
    date: str
    open: float | None
    high: float | None
    low: float | None
    close: float
    adjustedClose: float | None
    volume: int | None
    provider: str
    retrievedAt: str


class PageData(BaseModel):
    limit: int
    returned: int
    total: int
    truncated: bool


class BarsData(BaseModel):
    symbol: str
    bars: list[BarData]
    page: PageData


class TechnicalPoint(BaseModel):
    date: str
    price: float
    dailyChange: float | None
    dailyReturn: float | None
    movingAverages: dict[str, float | None]
    rollingHighs: dict[str, float | None]
    rollingLows: dict[str, float | None]
    volumeAverages: dict[str, float | None]
    relativeVolumes: dict[str, float | None]


class TechnicalsData(BaseModel):
    symbol: str
    priceBasis: Literal["close", "adjustedClose"]
    windows: list[int]
    points: list[TechnicalPoint]


class RiskMetric(BaseModel):
    date: str
    symbol: str
    oneMonthReturn: float | None = None
    ytdReturn: float | None = None
    twelveOneMomentum: float | None = None
    cumulativeReturn: float | None = None
    annualizedVolatility: float | None = None
    sharpeRatio: float | None = None
    sortinoRatio: float | None = None
    maxDrawdown: float | None = None
    observations: int
    beta: float | None = None
    annualizedAlpha: float | None = None
    benchmarkObservations: int


class CorrelationData(BaseModel):
    symbol: str
    otherSymbol: str
    correlation: float | None
    observations: int


class RiskData(BaseModel):
    period: str
    benchmark: str
    metrics: list[RiskMetric]
    correlations: list[CorrelationData]
    unavailableSymbols: list[str]


class ScreenerRow(RiskMetric):
    latestPrice: float
    sma50: float | None = None
    sma200: float | None = None
    momentumScore: float | None = None
    returnScore: float | None = None
    riskScore: float | None = None
    trendScore: float | None = None
    coverage: float
    compositeScore: float | None = None
    signals: list[str]


class ScreenerData(BaseModel):
    period: str
    benchmark: str
    rows: list[ScreenerRow]
    unavailableSymbols: list[str]


class WatchlistData(BaseModel):
    symbols: list[str]


class PortfolioSummary(BaseModel):
    positionCount: int
    pricedPositionCount: int
    costValue: float
    pricedCostValue: float
    marketValue: float | None
    gainLoss: float | None
    gainReturn: float | None


class PositionData(BaseModel):
    date: str | None
    id: str
    symbol: str
    quantity: float
    averageCost: float
    lastPrice: float | None
    marketValue: float | None
    costValue: float
    gainLoss: float | None
    gainReturn: float | None
    weight: float | None
    marketDataAvailable: bool
    account: str | None
    assetClass: str | None
    sector: str | None
    acquired: str | None


class AllocationData(BaseModel):
    name: str
    costValue: float
    marketValue: float
    gainLoss: float
    weight: float


class AllocationsData(BaseModel):
    account: list[AllocationData]
    assetClass: list[AllocationData]
    sector: list[AllocationData]


class PortfolioData(BaseModel):
    currency: Literal["USD"]
    positions: list[PositionData]
    summary: PortfolioSummary
    allocations: AllocationsData
    unpricedSymbols: list[str]


class PortfolioRiskMetric(BaseModel):
    symbol: Literal["Portfolio"]
    cumulativeReturn: float | None
    annualizedVolatility: float | None
    sharpeRatio: float | None
    sortinoRatio: float | None
    maxDrawdown: float | None
    observations: int
    beta: float | None
    annualizedAlpha: float | None
    benchmarkObservations: int


class PortfolioHistoryPoint(BaseModel):
    date: str
    portfolioValue: float
    portfolioReturn: float | None


class ReturnContributionData(BaseModel):
    symbol: str
    quantity: float
    startDate: str
    endDate: str
    startValue: float
    endValue: float
    returnContribution: float


class RiskContributionData(BaseModel):
    symbol: str
    weight: float
    varianceRiskContribution: float | None


class PortfolioRiskData(BaseModel):
    period: str
    benchmark: str
    metrics: list[PortfolioRiskMetric]
    history: list[PortfolioHistoryPoint]
    returnContributions: list[ReturnContributionData]
    riskContributions: list[RiskContributionData]
    correlations: list[CorrelationData]
    unavailableSymbols: list[str]


DATA_RESPONSES = {
    409: {"model": ApiErrorResponse, "description": "Stored data is insufficient"},
    422: {"model": ApiErrorResponse, "description": "Request validation failed"},
}
VALIDATION_RESPONSES = {
    422: {"model": ApiErrorResponse, "description": "Request validation failed"},
}
class StrictQueryRoute(APIRoute):
    """Reject ignored/ambiguous query options before any service work."""

    def get_route_handler(self):
        handler = super().get_route_handler()
        allowed = {parameter.alias for parameter in self.dependant.query_params}

        async def strict_handler(request: Request):
            unknown = sorted(set(request.query_params) - allowed)
            repeated = sorted(
                key for key in request.query_params
                if len(request.query_params.getlist(key)) > 1
            )
            if unknown or repeated:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "invalid_request",
                        "message": "Unknown or repeated query parameters",
                        "errors": [
                            {"loc": ["query", key], "type": kind}
                            for kind, keys in (
                                ("extra_forbidden", unknown),
                                ("repeated_parameter", repeated),
                            )
                            for key in keys
                        ],
                    },
                )
            return await handler(request)

        return strict_handler


router = APIRouter(
    tags=["financial-data"],
    route_class=StrictQueryRoute,
    responses={
        **VALIDATION_RESPONSES,
        500: {"model": ApiErrorResponse, "description": "Internal server error"},
        503: {"model": ApiErrorResponse, "description": "Storage unavailable"},
    },
)
_service = DashboardService(read_only=True)


def get_service() -> DashboardService:
    return _service


Service = Annotated[DashboardService, Depends(get_service)]

app = FastAPI(
    title="Quant Alpha Financial Data API",
    summary="Stored, cleaned, and prepared financial data for automation clients",
    version="alpha",
    docs_url="/docs",
    openapi_url="/openapi.json",
    redirect_slashes=False,
)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(
    request: Request, error: StarletteHTTPException
) -> JSONResponse:
    detail = error.detail
    fallback_code = {404: "not_found", 405: "method_not_allowed"}.get(
        error.status_code, "http_error"
    )
    if isinstance(detail, dict):
        code = detail.get("code")
        message = detail.get("message")
        symbols = detail.get("symbols", [])
        errors = detail.get("errors", [])
        detail = {
            "code": code if isinstance(code, str) else fallback_code,
            "message": message if isinstance(message, str) else "Request failed",
            "symbols": symbols if isinstance(symbols, list) else [],
            "errors": errors if isinstance(errors, list) else [],
        }
    else:
        detail = {"code": fallback_code, "message": str(detail)}
    return JSONResponse(
        status_code=error.status_code,
        content=ApiErrorResponse(detail=ApiError(**detail)).model_dump(),
        headers=error.headers,
    )


@app.exception_handler(sqlite3.Error)
async def storage_error_handler(request: Request, error: sqlite3.Error) -> JSONResponse:
    logger.error("Alpha API storage request failed", exc_info=error)
    return JSONResponse(
        status_code=503,
        content=ApiErrorResponse(
            detail=ApiError(
                code="storage_unavailable",
                message="Stored data is temporarily unavailable",
            )
        ).model_dump(),
    )


@app.exception_handler(Exception)
async def internal_error_handler(request: Request, error: Exception) -> JSONResponse:
    logger.error("Alpha API request failed", exc_info=error)
    return JSONResponse(
        status_code=500,
        content=ApiErrorResponse(
            detail=ApiError(
                code="internal_error", message="An unexpected server error occurred"
            )
        ).model_dump(),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, error: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=ApiErrorResponse(
            detail=ApiError(
                code="invalid_request",
                message="Request validation failed",
                errors=jsonable_encoder(error.errors()),
            )
        ).model_dump(),
    )


@router.get("/health", response_model=ApiEnvelope[HealthData])
def health_alpha() -> ApiEnvelope[HealthData]:
    return _envelope({"status": "ok"})


@router.get(
    "/data/status",
    response_model=ApiEnvelope[DataStatusData],
    responses=VALIDATION_RESPONSES,
)
def data_status_alpha(service: Service, symbols: str | None = None) -> ApiEnvelope:
    parsed = _parse_symbols(symbols, required=False, maximum=100)
    data = (
        _execute(lambda: service.data_status(parsed))
        if _market_database_ready(service)
        else _empty_data_status(parsed)
    )
    summary = data["summary"]
    return _envelope(
        data,
        as_of=summary["lastSession"],
        retrieved_at=summary["retrievedAt"],
        provider=data["provider"],
        warnings=_missing_warnings(data["coverage"]),
    )


@router.get(
    "/securities",
    response_model=ApiEnvelope[SecuritiesData],
    responses=VALIDATION_RESPONSES,
)
def securities_alpha(
    service: Service,
    query: str = Query(..., min_length=1, max_length=100),
    limit: int = Query(10, ge=1, le=50),
) -> ApiEnvelope:
    query = " ".join(query.split())
    if not query:
        raise HTTPException(status_code=422, detail={
            "code": "invalid_request", "message": "Security search query must not be blank"
        })
    result = (
        _execute(lambda: service.security_search(query, limit, False))
        if _market_database_ready(service)
        else {"local": [], "remote": []}
    )
    return _envelope({"query": " ".join(query.split()), "results": result["local"]})


@router.get(
    "/market/quotes",
    response_model=ApiEnvelope[QuotesData],
    responses=DATA_RESPONSES,
)
def quotes_alpha(service: Service, symbols: str) -> ApiEnvelope:
    parsed = _parse_symbols(symbols, maximum=20)
    result = (
        _execute(lambda: service.quotes(parsed, refresh=False, fetch_missing=False))
        if _market_database_ready(service)
        else {"quotes": [{"symbol": symbol, "error": True} for symbol in parsed]}
    )
    quotes = []
    missing = []
    for item in result["quotes"]:
        if item.get("error"):
            missing.append(item["symbol"])
            continue
        quotes.append(
            {
                "date": item["asOf"],
                "symbol": item["symbol"],
                "name": item["name"],
                "price": item["price"],
                "previousClose": item["previousClose"],
                "change": item["change"],
                "changeReturn": (
                    item["changePercent"] / 100.0
                    if item["changePercent"] is not None
                    else None
                ),
                "volume": item["volume"],
                "currency": item["currency"],
                "provider": item["provider"],
                "retrievedAt": item["retrievedAt"],
            }
        )
    if not quotes:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "insufficient_data",
                "message": "Stored market data is unavailable for requested symbols",
                "symbols": missing,
            },
        )
    return _envelope(
        {"quotes": quotes, "missingSymbols": missing},
        as_of=min(item["date"] for item in quotes),
        retrieved_at=min(item["retrievedAt"] for item in quotes),
        provider="yahoo",
        price_basis="close",
        warnings=(
            [
                ApiWarning(
                    code="partial_data",
                    message="Some requested symbols have no stored quote",
                    symbols=missing,
                )
            ]
            if missing
            else []
        ),
    )


@router.get(
    "/securities/{symbol}/bars",
    response_model=ApiEnvelope[BarsData],
    responses=DATA_RESPONSES,
)
def bars_alpha(
    symbol: str,
    service: Service,
    start: date | None = None,
    end: date | None = None,
    limit: int = Query(500, ge=1, le=5_000),
) -> ApiEnvelope:
    symbol = _normalize_symbol(symbol)
    if start is not None and end is not None and start > end:
        raise HTTPException(status_code=422, detail={
            "code": "invalid_request", "message": "Start date cannot be after end date"
        })
    _require_market_database(service)
    data = _execute(lambda: service.market_bars(symbol, start, end, limit))
    bars = data["bars"]
    warnings = []
    if data["page"]["truncated"]:
        warnings.append(
            ApiWarning(
                code="truncated",
                message="Only the latest matching bars were returned",
                symbols=[symbol],
            )
        )
    return _envelope(
        data,
        as_of=bars[-1]["date"],
        retrieved_at=max(item["retrievedAt"] for item in bars),
        provider=bars[-1]["provider"],
        price_basis="raw",
        warnings=warnings,
    )


@router.get(
    "/securities/{symbol}/technicals",
    response_model=ApiEnvelope[TechnicalsData],
    responses=DATA_RESPONSES,
)
def technicals_alpha(
    symbol: str,
    service: Service,
    windows: str = "20,50,200",
    priceBasis: str = Query("adjustedClose", pattern="^(close|adjustedClose)$"),
) -> ApiEnvelope:
    symbol = _normalize_symbol(symbol)
    parsed_windows = _parse_windows(windows)
    _require_market_database(service)
    legacy_basis = "adjusted" if priceBasis == "adjustedClose" else "close"
    result = _execute(
        lambda: service.market_analysis(
            symbol,
            parsed_windows,
            legacy_basis,
            refresh=False,
            fetch_missing=False,
        )
    )
    points = []
    for row in result["rows"]:
        points.append(
            {
                **row,
                "dailyReturn": (
                    row["dailyChangePercent"] / 100.0
                    if row["dailyChangePercent"] is not None
                    else None
                ),
            }
        )
        points[-1].pop("dailyChangePercent")
    return _envelope(
        {
            "symbol": result["symbol"],
            "priceBasis": priceBasis,
            "windows": parsed_windows,
            "points": points,
        },
        as_of=points[-1]["date"],
        retrieved_at=_retrieved_at(service, [symbol]),
        provider="yahoo",
        price_basis=priceBasis,
    )


@router.get(
    "/market/risk",
    response_model=ApiEnvelope[RiskData],
    responses=DATA_RESPONSES,
)
def risk_alpha(
    service: Service,
    symbols: str,
    period: str = Query("1y", pattern="^(1mo|3mo|6mo|1y|2y|5y)$"),
    benchmark: str = "SPY",
) -> ApiEnvelope:
    parsed = _parse_symbols(symbols, maximum=20)
    benchmark = _normalize_symbol(benchmark)
    if all(symbol == benchmark for symbol in parsed):
        raise HTTPException(status_code=422, detail={
            "code": "invalid_request", "message": "Risk requires a non-benchmark symbol"
        })
    _require_market_database(service)
    data = _execute(
        lambda: service.symbol_risk(
            parsed,
            period,
            benchmark,
            refresh=False,
            fetch_missing=False,
        )
    )
    metrics = [_dated_metric(item) for item in data["metrics"]]
    data = {**data, "metrics": metrics}
    as_of = min(item["date"] for item in metrics)
    return _envelope(
        data,
        as_of=as_of,
        retrieved_at=_retrieved_at(service, [*parsed, benchmark]),
        provider="yahoo",
        price_basis="adjustedClose",
        warnings=_symbol_warnings(data["unavailableSymbols"]),
    )


@router.get(
    "/market/screener",
    response_model=ApiEnvelope[ScreenerData],
    responses=DATA_RESPONSES,
)
def screener_alpha(
    service: Service,
    symbols: str | None = None,
    period: str = Query("1y", pattern="^(1mo|3mo|6mo|1y|2y|5y)$"),
    benchmark: str = "SPY",
) -> ApiEnvelope:
    parsed = _parse_symbols(symbols, required=False, maximum=100)
    benchmark = _normalize_symbol(benchmark)
    if parsed is not None and parsed and all(
        symbol == benchmark for symbol in parsed
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_request",
                "message": "Screener requires a non-benchmark symbol",
            },
        )
    _require_market_database(service)
    data = _execute(
        lambda: service.screener(
            parsed,
            period,
            benchmark,
            refresh=False,
            fetch_missing=False,
        ),
        insufficient=True,
    )
    rows = [_dated_metric(item) for item in data["rows"]]
    data = {**data, "rows": rows}
    return _envelope(
        data,
        as_of=min(item["date"] for item in rows),
        retrieved_at=_retrieved_at(
            service,
            [*[item["symbol"] for item in data["rows"]], benchmark],
        ),
        provider="yahoo",
        price_basis="adjustedClose",
        warnings=_symbol_warnings(data["unavailableSymbols"]),
    )


@router.get("/watchlist", response_model=ApiEnvelope[WatchlistData])
def watchlist_alpha(service: Service) -> ApiEnvelope:
    symbols = (
        _execute(service.watchlist) if _user_database_ready(service) else []
    )
    return _envelope({"symbols": symbols})


@router.get("/portfolio", response_model=ApiEnvelope[PortfolioData])
def portfolio_alpha(service: Service) -> ApiEnvelope:
    result = (
        _execute(lambda: service.holdings(refresh=False, fetch_missing=False))
        if _user_database_ready(service)
        else _empty_holdings()
    )
    positions = []
    for item in result["holdings"]:
        positions.append(
            {
                "date": item["asOf"],
                "id": item["id"],
                "symbol": item["symbol"],
                "quantity": item["shares"],
                "averageCost": item["costBasis"],
                "lastPrice": item["price"],
                "marketValue": item["marketValue"],
                "costValue": item["costValue"],
                "gainLoss": item["gain"],
                "gainReturn": (
                    item["gainPercent"] / 100.0
                    if item["gainPercent"] is not None
                    else None
                ),
                "weight": (
                    item["weightPercent"] / 100.0
                    if item["weightPercent"] is not None
                    else None
                ),
                "marketDataAvailable": item["marketDataAvailable"],
                "account": item["account"],
                "assetClass": item["assetClass"],
                "sector": item["sector"],
                "acquired": item["acquired"],
            }
        )
    allocations = {
        key: [
            {
                "name": item["name"],
                "costValue": item["cost"],
                "marketValue": item["value"],
                "gainLoss": item["gain"],
                "weight": item["weightPercent"] / 100.0,
            }
            for item in values
        ]
        for key, values in result["allocations"].items()
    }
    totals = result["totals"]
    data = {
        "currency": "USD",
        "positions": positions,
        "summary": {
            "positionCount": len(positions),
            "pricedPositionCount": sum(
                item["marketDataAvailable"] for item in positions
            ),
            "costValue": totals["cost"],
            "pricedCostValue": totals["pricedCost"],
            "marketValue": totals["value"],
            "gainLoss": totals["gain"],
            "gainReturn": (
                totals["gainPercent"] / 100.0
                if totals["gainPercent"] is not None
                else None
            ),
        },
        "allocations": allocations,
        "unpricedSymbols": result["unpricedSymbols"],
    }
    as_of_values = [item["date"] for item in positions if item["date"]]
    return _envelope(
        data,
        as_of=min(as_of_values) if as_of_values else None,
        retrieved_at=(
            _retrieved_at(
                service,
                [
                    item["symbol"]
                    for item in positions
                    if item["marketDataAvailable"]
                ],
            )
            if as_of_values
            else None
        ),
        provider="yahoo" if as_of_values else None,
        price_basis="close" if as_of_values else None,
        warnings=(
            [
                ApiWarning(
                    code="unpriced_positions",
                    message="Portfolio totals exclude positions without stored quotes",
                    symbols=result["unpricedSymbols"],
                )
            ]
            if result["unpricedSymbols"]
            else []
        ),
    )


@router.get(
    "/portfolio/risk",
    response_model=ApiEnvelope[PortfolioRiskData],
    responses=DATA_RESPONSES,
)
def portfolio_risk_alpha(
    service: Service,
    period: str = Query("1y", pattern="^(1mo|3mo|6mo|1y|2y|5y)$"),
    benchmark: str = "SPY",
) -> ApiEnvelope:
    benchmark = _normalize_symbol(benchmark)
    if not _user_database_ready(service) or not _market_database_ready(service):
        _raise_insufficient("Portfolio risk requires initialized stored data")
    data = _execute(
        lambda: service.portfolio_risk(
            period,
            benchmark,
            refresh=False,
            fetch_missing=False,
        ),
        insufficient=True,
    )
    warnings = [
        ApiWarning(
            code="current_holdings_backcast",
            message="Historical values assume current shares were held for the full period",
        ),
        ApiWarning(
            code="static_weight_risk",
            message="Variance risk attribution uses static latest-date weights",
        ),
    ]
    return _envelope(
        data,
        as_of=data["history"][-1]["date"],
        retrieved_at=_retrieved_at(
            service,
            [
                *[item["symbol"] for item in data["returnContributions"]],
                benchmark,
            ],
        ),
        provider="yahoo",
        price_basis="adjustedClose",
        warnings=warnings,
    )


def _execute(operation: Callable[[], Any], *, insufficient: bool = False) -> Any:
    try:
        return operation()
    except ValueError as error:
        if insufficient:
            _raise_insufficient(str(error))
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_request", "message": str(error)},
        ) from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=409,
            detail={"code": "insufficient_data", "message": str(error)},
        ) from error


def _dated_metric(item: dict[str, Any]) -> dict[str, Any]:
    dated = {**item, "date": item["latestDate"]}
    dated.pop("latestDate")
    return dated


def _retrieved_at(service: DashboardService, symbols: list[str]) -> str | None:
    normalized = list(dict.fromkeys(symbols))
    if not normalized:
        return None
    timestamps = []
    for offset in range(0, len(normalized), 100):
        batch = normalized[offset : offset + 100]
        status = _execute(lambda batch=batch: service.data_status(batch))
        if retrieved_at := status["summary"]["retrievedAt"]:
            timestamps.append(retrieved_at)
    return min(timestamps, default=None)


def _envelope(
    data: Any,
    *,
    as_of: str | None = None,
    retrieved_at: str | None = None,
    provider: str | None = None,
    price_basis: str | None = None,
    warnings: list[ApiWarning] | None = None,
) -> ApiEnvelope:
    freshness = _freshness(as_of)
    response_warnings = list(warnings or [])
    if freshness.status == "stale":
        response_warnings.append(
            ApiWarning(
                code="stale_data",
                message="Latest stored observation precedes the latest completed XNYS session",
            )
        )
    return ApiEnvelope(
        data=data,
        meta=ApiMeta(
            generatedAt=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            asOf=as_of,
            retrievedAt=retrieved_at,
            provider=provider,
            priceBasis=price_basis,
            freshness=freshness,
        ),
        warnings=response_warnings,
    )


def _freshness(as_of: str | None) -> Freshness:
    if as_of is None:
        return Freshness(
            status="unknown", ageCalendarDays=None, staleAfterCalendarDays=None
        )
    from quant.calendars import freshness
    age = (datetime.now(EASTERN_TIME).date() - date.fromisoformat(as_of)).days
    try:
        state = freshness(as_of)
    except ValueError:
        state = {"status":"unknown","latestCompletedSession":None,"missingSessions":None}
    return Freshness(
        status=state["status"],
        ageCalendarDays=age if age >= 0 else None,
        staleAfterCalendarDays=None,
        latestCompletedSession=state["latestCompletedSession"],
        missingSessions=state["missingSessions"],
    )


def _parse_symbols(
    value: str | None,
    *,
    required: bool = True,
    maximum: int,
) -> list[str] | None:
    if value is None:
        if required:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_request", "message": "Symbols are required"},
            )
        return None
    symbols = list(
        dict.fromkeys(
            _normalize_symbol(symbol) for symbol in value.split(",")
        )
    )
    if not symbols:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_request", "message": "Symbols are required"},
        )
    if len(symbols) > maximum:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_request",
                "message": f"At most {maximum} symbols are allowed",
            },
        )
    return symbols


def _normalize_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_request", "message": "Invalid symbol"},
        )
    return symbol


def _parse_windows(value: str) -> list[int]:
    try:
        windows = list(
            dict.fromkeys(int(item.strip()) for item in value.split(","))
        )
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_request",
                "message": "Windows must be comma-separated integers",
            },
        ) from error
    if (
        not windows
        or len(windows) > 10
        or any(not 1 <= item <= 2_520 for item in windows)
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_request",
                "message": "Provide 1-10 windows between 1 and 2520",
            },
        )
    return windows


def _symbol_warnings(symbols: list[str]) -> list[ApiWarning]:
    if not symbols:
        return []
    return [
        ApiWarning(
            code="partial_data",
            message="Some requested symbols lack complete stored history",
            symbols=symbols,
        )
    ]


def _missing_warnings(coverage: list[dict]) -> list[ApiWarning]:
    missing = [item["symbol"] for item in coverage if item["sessionCount"] == 0]
    return _symbol_warnings(missing)


def _market_database_ready(service: DashboardService) -> bool:
    return service.market_database_ready()


def _user_database_ready(service: DashboardService) -> bool:
    return service.user_database_ready()


def _require_market_database(service: DashboardService) -> None:
    if not _market_database_ready(service):
        _raise_insufficient("Market database is not initialized")


def _raise_insufficient(message: str) -> None:
    raise HTTPException(
        status_code=409,
        detail={"code": "insufficient_data", "message": message},
    )


def _empty_data_status(symbols: list[str] | None) -> dict:
    return {
        "storage": "sqlite",
        "provider": "yahoo",
        "summary": {
            "securityCount": 0,
            "barCount": 0,
            "completeOhlcvBarCount": 0,
            "adjustedCloseBarCount": 0,
            "firstSession": None,
            "lastSession": None,
            "retrievedAt": None,
        },
        "coverage": [
            {
                "symbol": symbol,
                "firstSession": None,
                "lastSession": None,
                "sessionCount": 0,
                "completeOhlcvSessionCount": 0,
                "adjustedCloseSessionCount": 0,
                "retrievedAt": None,
            }
            for symbol in symbols or []
        ],
    }


def _empty_holdings() -> dict:
    return {
        "holdings": [],
        "totals": {
            "cost": 0.0,
            "pricedCost": 0.0,
            "value": 0.0,
            "gain": 0.0,
            "gainPercent": None,
        },
        "allocations": {"account": [], "assetClass": [], "sector": []},
        "unpricedSymbols": [],
    }


app.include_router(router)


def configure_service(service: DashboardService) -> None:
    global _service
    _service = service


from quant.dashboard.api_research import router as research_router
app.include_router(research_router)
