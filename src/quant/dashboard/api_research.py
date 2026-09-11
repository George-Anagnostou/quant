"""Typed local research API adapter. All work is delegated to application services."""
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field

from quant.contracts import (StrictModel, Text, Symbol, SnapshotInput, RunInput, ReportInput,
    EvidenceInput, ThesisInput, LedgerInput, ScenarioInput, ExperimentInput, EvaluationInput, SecurityMetadataInput, ValuationInput)
from quant.dashboard.api_alpha import ApiEnvelope, ApiWarning, Service, StrictQueryRoute, _envelope, _execute
from quant.platform_service import PlatformService
from quant.readiness import ReadinessData

router=APIRouter(route_class=StrictQueryRoute,tags=["research-engine"])


def platform(service:Service):
    return PlatformService(service.market_repository.path)


Platform=Annotated[PlatformService,Depends(platform)]


def local_write(request:Request):
    if request.client is None or request.client.host not in {"127.0.0.1","::1"} or request.url.hostname not in {"127.0.0.1","localhost","::1"}:
        raise HTTPException(403,detail={"code":"local_write_required","message":"Writes require loopback access"})
    origin=request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403,detail={"code":"invalid_origin","message":"Cross-origin writes are not allowed"})


Write=Annotated[None,Depends(local_write)]


class RecordData(StrictModel):
    id:str
    kind:str
    importKey:str
    digest:str
    createdAt:str
    payload:dict[str,Any]


class SyncInput(StrictModel):
    importKey:Text
    symbols:Annotated[list[Symbol],Field(min_length=1,max_length=2000)] | None=None
    horizon:date | None=None


class SECInput(StrictModel):
    symbol:Symbol
    cik:Annotated[str,Field(pattern="^[0-9]{1,10}$")]


class LiveProfileInput(StrictModel):
    symbol:Symbol


def respond(operation):
    data = _execute(operation)
    payload = data.get("payload",data) if isinstance(data,dict) else {}
    result = payload.get("result",payload)
    warnings = []
    for warning in result.get("warnings",[]):
        if isinstance(warning,dict):
            warnings.append(ApiWarning(code=warning["code"],message=warning.get("message",warning["code"]),symbols=warning.get("symbols",[])))
        else:
            warnings.append(ApiWarning(code="method_limitation",message=warning))
    envelope = _envelope(data,warnings=warnings)
    envelope.meta.methodologyVersion = result.get("methodologyVersion")
    envelope.meta.datasetSha256 = payload.get("datasetSha256")
    envelope.meta.asOf = result.get("date")
    envelope.meta.provider = result.get("provider")
    envelope.meta.priceBasis = result.get("priceBasis")
    return envelope


@router.get("/capabilities",response_model=ApiEnvelope[dict[str,Any]])
def capabilities(service:Platform):
    return respond(service.capabilities)


@router.get("/calendars/{calendar}/sessions",response_model=ApiEnvelope[dict[str,Any]])
def calendar_sessions(calendar:str,service:Platform,start:date,end:date):
    return respond(lambda:service.calendar(calendar,start,end))


@router.get("/data/gaps",response_model=ApiEnvelope[dict[str,Any]])
def gaps(service:Platform,limit:int=Query(100,ge=1,le=1000),offset:int=Query(0,ge=0)):
    return respond(lambda:service.gaps(limit,offset))


@router.get("/data/quality",response_model=ApiEnvelope[dict[str,Any]])
def quality(service:Platform):
    return respond(service.quality)


@router.post("/data/sync",response_model=ApiEnvelope[RecordData])
def sync_request(body:SyncInput,service:Platform,write:Write):
    return respond(lambda:service.request_sync(body.importKey,body.symbols,body.horizon))


@router.get("/data/ingestion-runs/{identifier}",response_model=ApiEnvelope[dict[str,Any]])
def ingestion_run(identifier:str,service:Platform):
    return respond(lambda:service.run(identifier))


@router.post("/portfolio/snapshots",response_model=ApiEnvelope[RecordData])
def snapshot_import(body:SnapshotInput,service:Platform,write:Write):
    return respond(lambda:service.research.import_snapshot(body))


@router.get("/portfolio/snapshots",response_model=ApiEnvelope[list[RecordData]])
def snapshots(service:Platform,limit:int=Query(100,ge=1,le=1000),offset:int=Query(0,ge=0)):
    return respond(lambda:service.records.list("snapshot",limit,offset))


@router.get("/portfolio/snapshots/{identifier}/review",response_model=ApiEnvelope[dict[str,Any]])
def review(identifier:str,service:Platform,period:Literal["1mo","3mo","6mo","1y","2y","5y"]="1y",benchmark:Symbol="SPY"):
    return respond(lambda:service.research.review(identifier,period,benchmark))


@router.get("/portfolio/snapshots/{identifier}/readiness",response_model=ApiEnvelope[ReadinessData])
def readiness(identifier:str,service:Platform,period:Literal["1mo","3mo","6mo","1y","2y","5y"]="1y",benchmark:Symbol="SPY"):
    return respond(lambda:service.readiness(identifier,period,benchmark))


@router.get("/research/runs/{identifier}/readiness",response_model=ApiEnvelope[ReadinessData])
def run_readiness(identifier:str,service:Platform):
    return respond(lambda:service.run_readiness(identifier))


@router.post("/research/runs",response_model=ApiEnvelope[dict[str,Any]])
def create_run(body:RunInput,service:Platform,write:Write):
    return respond(lambda:service.start_run(body))


@router.get("/research/runs/{identifier}",response_model=ApiEnvelope[dict[str,Any]])
def run_summary(identifier:str,service:Platform):
    return respond(lambda:service.run_summary(identifier))


@router.get("/research/runs/{identifier}/replay",response_model=ApiEnvelope[dict[str,Any]])
def replay(identifier:str,service:Platform):
    return respond(lambda:service.research.replay(identifier))


@router.post("/research/reports",response_model=ApiEnvelope[RecordData])
def report(body:ReportInput,service:Platform,write:Write):
    return respond(lambda:service.research.save_report(body))


@router.get("/research/records/{kind}",response_model=ApiEnvelope[list[RecordData]])
def research_records(kind:Literal["run","report","evidence","thesis","ledger","experiment","evaluation","sync_request"],service:Platform,limit:int=Query(100,ge=1,le=1000),offset:int=Query(0,ge=0)):
    return respond(lambda:service.records.list(kind,limit,offset))


@router.get("/research/records/{kind}/{identifier}",response_model=ApiEnvelope[RecordData])
def research_record(kind:Literal["snapshot","run","report","evidence","thesis","ledger","experiment","evaluation","sync_request"],identifier:str,service:Platform):
    return respond(lambda:service.records.get(kind,identifier))


@router.post("/research/evidence",response_model=ApiEnvelope[RecordData])
def evidence(body:EvidenceInput,service:Platform,write:Write):
    return respond(lambda:service.fundamentals.save(body))


@router.post("/research/sec",response_model=ApiEnvelope[dict[str,Any]])
def sec(body:SECInput,service:Platform,write:Write):
    return respond(lambda:service.fundamentals.ingest_sec(body.symbol,body.cik))


@router.get("/research/fundamentals/{symbol}",response_model=ApiEnvelope[dict[str,Any]])
def fundamentals(symbol:Symbol,service:Platform,asOf:str | None=None):
    return respond(lambda:service.fundamentals.summary(symbol,asOf))


@router.post("/research/theses",response_model=ApiEnvelope[RecordData])
def thesis(body:ThesisInput,service:Platform,write:Write):
    return respond(lambda:service.fundamentals.thesis(body))


@router.get("/portfolio/snapshots/{identifier}/look-through",response_model=ApiEnvelope[dict[str,Any]])
def look_through(identifier:str,service:Platform):
    return respond(lambda:service.fundamentals.fund_overlap(service.records.get("snapshot",identifier)["payload"]))


@router.post("/portfolio/ledger",response_model=ApiEnvelope[RecordData])
def ledger(body:LedgerInput,service:Platform,write:Write):
    return respond(lambda:service.ledger.import_ledger(body))


@router.get("/portfolio/ledger/{identifier}/performance",response_model=ApiEnvelope[dict[str,Any]])
def performance(identifier:str,service:Platform,runId:str | None=None):
    return respond(lambda:service.ledger_performance(identifier,runId))


@router.post("/portfolio/scenarios",response_model=ApiEnvelope[dict[str,Any]])
def scenario(body:ScenarioInput,service:Platform):
    return respond(lambda:service.discovery.scenario(body))


@router.get("/universes",response_model=ApiEnvelope[list[dict[str,Any]]])
def universes(service:Platform):
    return respond(service.discovery.universes)


@router.get("/universes/{universe}/members",response_model=ApiEnvelope[dict[str,Any]])
def members(universe:str,service:Platform,asOf:date):
    return respond(lambda:service.discovery.members(universe,asOf.isoformat()))


@router.get("/market/breadth",response_model=ApiEnvelope[dict[str,Any]])
def breadth(service:Platform,universe:str,asOf:date):
    return respond(lambda:service.discovery.breadth(universe,asOf.isoformat()))


@router.get("/market/factors",response_model=ApiEnvelope[dict[str,Any]])
def factors(service:Platform,symbol:Symbol,factors:str,start:date,end:date):
    return respond(lambda:service.discovery.factors(symbol,factors.split(","),start,end))


@router.post("/research/experiments",response_model=ApiEnvelope[RecordData])
def experiment(body:ExperimentInput,service:Platform,write:Write):
    return respond(lambda:service.discovery.experiment(body))


@router.post("/research/evaluations",response_model=ApiEnvelope[RecordData])
def evaluation(body:EvaluationInput,service:Platform,write:Write):
    return respond(lambda:service.discovery.evaluate(body))


@router.post("/securities/metadata",response_model=ApiEnvelope[RecordData])
def security_metadata(body:SecurityMetadataInput,service:Platform,write:Write):
    return respond(lambda:service.security_metadata(body))


@router.get("/market/bars",response_model=ApiEnvelope[dict[str,Any]])
def market_bars(service:Platform,symbols:str,start:date,end:date,
                fields:str="close,adjustedClose",limit:int=Query(1000,ge=1,le=10000),
                cursor:str | None=Query(None,max_length=1000),runId:str | None=None):
    return respond(lambda:service.bars(symbols.split(","),start,end,fields.split(","),limit,cursor,runId))


@router.post("/research/valuation",response_model=ApiEnvelope[dict[str,Any]])
def valuation(body:ValuationInput,service:Platform):
    return respond(lambda:service.valuation(body))


@router.post("/research/live/profile",response_model=ApiEnvelope[dict[str,Any]])
def live_profile(body:LiveProfileInput,service:Service,write:Write):
    return respond(lambda:service.research_service.profile_with_metadata(body.symbol))


@router.get("/research/evaluations",response_model=ApiEnvelope[dict[str,Any]])
def evaluations(service:Platform,runId:str):
    return respond(lambda:service.discovery.evaluations(runId))


@router.get("/research/experiments/{identifier}/replay",response_model=ApiEnvelope[dict[str,Any]])
def replay_experiment(identifier:str,service:Platform):
    return respond(lambda:service.discovery.replay_experiment(identifier))

# Current holdings have their own user-scoped service, separate from frozen research.
from quant.portfolio_contracts import PortfolioWrite, CurrentSnapshotInput
from quant.portfolio_service import PortfolioService


def current_portfolio(service: Service):
    return PortfolioService(service.repository.path, service.repository.user_id)


CurrentPortfolio = Annotated[PortfolioService, Depends(current_portfolio)]


@router.get('/portfolio/holdings', response_model=ApiEnvelope[dict[str, Any]])
def portfolio_holdings(service: CurrentPortfolio):
    return respond(service.show)


@router.get('/portfolio/schema', response_model=ApiEnvelope[dict[str, Any]])
def portfolio_schema():
    return respond(lambda: PortfolioWrite.model_json_schema())


@router.post('/portfolio/preview', response_model=ApiEnvelope[dict[str, Any]])
def portfolio_preview(body: PortfolioWrite, service: CurrentPortfolio):
    return respond(lambda: service.preview(body))


@router.post('/portfolio/apply', response_model=ApiEnvelope[dict[str, Any]])
def portfolio_apply(body: PortfolioWrite, service: CurrentPortfolio, write: Write):
    return respond(lambda: service.apply(body))


@router.get('/portfolio/history', response_model=ApiEnvelope[list[dict[str, Any]]])
def portfolio_history(service: CurrentPortfolio, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
    return respond(lambda: service.history(limit, offset))


@router.post('/portfolio/capture', response_model=ApiEnvelope[RecordData])
def portfolio_capture(body: CurrentSnapshotInput, service: CurrentPortfolio, write: Write):
    return respond(lambda: service.snapshot(body))

from quant.portfolio_contracts import (TransactionImport, NavInput, ReportedNavInput,
    HoldingsScenario, SaleSimulation, PortfolioAnalysisData, TransactionData)
from quant.ledger import AccountPerformanceService


def account_performance(service: Service):
    return AccountPerformanceService(service.repository.path, service.repository.user_id)


AccountPerformance = Annotated[AccountPerformanceService, Depends(account_performance)]


@router.get('/portfolio/analysis', response_model=ApiEnvelope[PortfolioAnalysisData])
def portfolio_analysis(service: CurrentPortfolio, account: str | None = None, date: date | None = None):
    return respond(lambda: service.analysis(account, date))


@router.get('/portfolio/transactions', response_model=ApiEnvelope[list[TransactionData]])
def portfolio_transactions(service: CurrentPortfolio, account: str | None = None, start: date | None = None,
                           end: date | None = None, limit: int = Query(100,ge=1,le=10000), offset: int = Query(0,ge=0)):
    return respond(lambda: service.transactions(account,start,end,limit,offset))


@router.post('/portfolio/transactions/preview', response_model=ApiEnvelope[dict[str,Any]])
def transactions_preview(body: TransactionImport, service: CurrentPortfolio):
    return respond(lambda: service.preview(body))


@router.post('/portfolio/transactions/import', response_model=ApiEnvelope[dict[str,Any]])
def transactions_import(body: TransactionImport, service: CurrentPortfolio, write: Write):
    return respond(lambda: service.apply(body))


@router.post('/portfolio/nav', response_model=ApiEnvelope[RecordData])
def nav_capture(body: NavInput, service: AccountPerformance, write: Write):
    return respond(lambda: service.capture_nav(body))


@router.get('/portfolio/nav', response_model=ApiEnvelope[list[dict[str,Any]]])
def nav_list(service: AccountPerformance, account: str | None = None, limit: int = Query(100,ge=1,le=1000), offset: int = Query(0,ge=0)):
    return respond(lambda: service.nav_records(account,limit,offset))


@router.get('/portfolio/nav/{identifier}', response_model=ApiEnvelope[dict[str,Any]])
def nav_get(identifier: str, service: AccountPerformance):
    return respond(lambda: service.get_nav(identifier))


@router.post('/portfolio/nav/observations', response_model=ApiEnvelope[RecordData])
def nav_observation(body: ReportedNavInput, service: AccountPerformance, write: Write):
    return respond(lambda: service.reported_nav(body))


@router.get('/portfolio/tax-lots', response_model=ApiEnvelope[dict[str,Any]])
def tax_lots(service: CurrentPortfolio, account: str | None = None, date: date | None = None):
    return respond(lambda: service.tax_lots(account,date))


@router.post('/portfolio/sale-simulation', response_model=ApiEnvelope[dict[str,Any]])
def sale_simulation(body: SaleSimulation, service: CurrentPortfolio):
    return respond(lambda: service.simulate_sales(body))


@router.post('/portfolio/holdings-scenario', response_model=ApiEnvelope[dict[str,Any]])
def holdings_scenario(body: HoldingsScenario, service: CurrentPortfolio):
    return respond(lambda: service.scenario(body))


from quant.portfolio_contracts import PerformanceData


@router.get('/portfolio/nav/{identifier}/performance', response_model=ApiEnvelope[PerformanceData])
def account_risk(identifier: str, service: AccountPerformance):
    return respond(lambda:service.performance(identifier))


@router.get('/portfolio/schemas', response_model=ApiEnvelope[dict[str,Any]])
def portfolio_schemas():
    return respond(lambda:{model.__name__:model.model_json_schema() for model in
        (PortfolioWrite,TransactionImport,NavInput,ReportedNavInput,HoldingsScenario,SaleSimulation,CurrentSnapshotInput)})
