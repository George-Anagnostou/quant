"""Application boundary for discovery, durable jobs and research workflows."""
from datetime import date
from pathlib import Path

from quant.calendars import CALENDAR_VERSION, freshness, sessions
from quant.database import database_connection
from quant.discovery import DiscoveryService
from quant.fundamentals import FundamentalService
from quant.ingestion import IngestionService
from quant.ledger import LedgerService
from quant.record_store import RecordRepository
from quant.workflows import METHOD_VERSION, ResearchWorkflow


class PlatformService:
    def __init__(self,path):
        self.path=Path(path)
        self.records=RecordRepository(self.path)
        self.research=ResearchWorkflow(self.path)
        self.fundamentals=FundamentalService(self.path)
        self.ledger=LedgerService(self.path)
        self.discovery=DiscoveryService(self.path)

    def capabilities(self):
        return {"methodologyVersion":METHOD_VERSION,"calendarVersion":CALENDAR_VERSION,
            "providers":["yahoo","sec"],"baseCurrency":"USD","calendars":["XNYS"],
            "limits":{"portfolioPositions":200,"jointRiskPositions":50,"factorProxies":5,"recordBytes":8388608},
            "units":{"returns":"fraction","weights":"fraction","legacyCorePercentColumns":"explicit % suffix","costs":"basis points"},
            "features":["snapshots","portfolio_readiness","frozen_research_runs","evidence","ledger_reconciliation","market_breadth","factor_proxies","hypothesis_tests","agent_evaluations"],
            "conventions":{"riskFreeRate":0,"annualization":252,"ledgerEvents":"end_of_day","backcastsAreActualPerformance":False},
            "workflow":["Inspect data quality","Import a dated snapshot","Check snapshot readiness","Create a frozen run","Inspect its warnings and evidence","Save a report acknowledging every warning","Replay the run"]}

    def readiness(self, snapshot_id, period="1y", benchmark="SPY"):
        from quant.readiness import portfolio_readiness
        snapshot = self.records.get("snapshot", snapshot_id)["payload"]
        return portfolio_readiness(self.path, snapshot, snapshot_id, period, benchmark)

    def run_readiness(self, identifier):
        from datetime import datetime
        from quant.readiness import portfolio_readiness
        record = self.records.get("run", identifier)
        run = record["payload"]
        request = run["request"]
        return portfolio_readiness(self.research.dataset(run), run["snapshot"], request["snapshotId"],
                                   request["period"], request["benchmark"],
                                   now=datetime.fromisoformat(record["createdAt"]), run_id=identifier,
                                   dataset_sha256=run["datasetSha256"])

    def valuation(self,body):
        from quant.fundamentals import valuation_sensitivity
        return valuation_sensitivity(body)

    def ledger_performance(self,identifier,run_id=None):
        if run_id is None:
            return self.ledger.performance(identifier)
        run=self.records.get("run",run_id)["payload"]
        result=LedgerService(self.research.dataset(run)).performance(identifier)
        return {**result,"datasetSha256":run["datasetSha256"]}

    def start_run(self,body):
        return self._run_summary(self.research.create_run(body))

    def run_summary(self,identifier):
        return self._run_summary(self.records.get("run",identifier))

    @staticmethod
    def _run_summary(record):
        payload=record["payload"]
        result={**payload["result"]}
        if result["risk"] is not None:
            result["risk"]={key:value for key,value in result["risk"].items() if key not in {"history","correlations"}}
        return {"id":record["id"],"request":payload["request"],"datasetSha256":payload["datasetSha256"],
                "calculationDigest":payload["calculationDigest"],"result":result,
                "fullRecordPath":f"research/records/run/{record['id']}",
                "omittedFromSummary":["calculationSources","risk.history","risk.correlations"]}

    def calendar(self,calendar,start,end):
        return {"calendar":calendar,"version":CALENDAR_VERSION,"sessions":[d.isoformat() for d in sessions(start,end,calendar)]}

    def gaps(self,limit=100,offset=0):
        with database_connection(self.path,read_only=True) as db:
            rows=db.execute("SELECT * FROM data_issues WHERE resolved_at IS NULL ORDER BY symbol,session_date,code LIMIT ? OFFSET ?",(limit,offset)).fetchall()
        return {"issues":[dict(r) for r in rows],"limit":limit,"offset":offset}

    def quality(self):
        from quant.market_store import MarketDataRepository
        coverage=MarketDataRepository(self.path,read_only=True).coverage()
        with database_connection(self.path,read_only=True) as db:
            metadata={r["symbol"]:dict(r) for r in db.execute("SELECT symbol,currency,instrument_type,calendar FROM securities")}
            latest=db.execute("SELECT * FROM ingestion_runs ORDER BY started_at DESC LIMIT 1").fetchone()
        return {"securities":[{"symbol":r["Symbol"],"firstSession":r["First Session"].isoformat(),
            "lastSession":r["Last Session"].isoformat(),"sessionCount":r["Session Count"],
            "metadata":metadata[r["Symbol"]],"freshness":freshness(r["Last Session"].isoformat(),metadata[r["Symbol"]]["calendar"])} for r in coverage.to_dicts()],
            "lastIngestion":dict(latest) if latest else None}

    def request_sync(self,key,symbols,horizon):
        if horizon is not None:
            from quant.calendars import latest_completed_session
            if horizon > latest_completed_session():
                raise ValueError("Horizon must not exceed latest completed session")
            sessions(horizon,date.today())
        return self.records.put("sync_request",key,{"symbols":symbols,"horizon":horizon.isoformat() if horizon else None})

    def run(self,identifier):
        try:
            return IngestionService(self.path).run(identifier)
        except ValueError:
            queued=self.records.get("sync_request",identifier)
            return {"id":identifier,"status":"queued","jobs":[],"request":queued["payload"]}

    def security_metadata(self, body):
        from quant.market_store import MarketDataRepository
        import polars as pl
        def apply(db):
            MarketDataRepository._upsert_securities(db,pl.DataFrame({"Symbol":[body.symbol],"Company":[None]},schema={"Symbol":pl.String,"Company":pl.String}))
            db.execute("UPDATE securities SET currency=?,calendar=?,instrument_type=? WHERE symbol=?",
                       (body.currency,body.calendar,body.instrumentType,body.symbol))
        return self.records.put("security_metadata",body.importKey,body.model_dump(mode="json"),after_insert=apply)

    def bars(self,symbols,start,end,fields,limit,cursor,run_id):
        import base64
        import hashlib
        import json
        from quant.market_store import MarketDataRepository
        from quant.record_store import canonical
        from quant.workflows import records
        if len(symbols)>100 or not symbols:
            raise ValueError("Bars accepts 1-100 symbols")
        mapping={"date":"Date","symbol":"Symbol","open":"Open","high":"High","low":"Low","close":"Close","adjustedClose":"Adjusted Close","volume":"Volume"}
        if set(fields)-set(mapping) or not fields:
            raise ValueError("Unsupported bar projection")
        path=self.path
        if run_id:
            run=self.records.get("run",run_id)["payload"]
            path=self.research.dataset(run)
            revision=run["datasetSha256"]
        else:
            with database_connection(path,read_only=True) as db:
                revision=tuple(db.execute("SELECT COUNT(*),MAX(retrieved_at) FROM daily_bars").fetchone())
        signature=hashlib.sha256(canonical([symbols,start.isoformat(),end.isoformat(),fields,revision]).encode()).hexdigest()
        offset=0
        if cursor:
            try:
                page=json.loads(base64.urlsafe_b64decode(cursor))
                if page["signature"]!=signature or not isinstance(page["offset"],int) or not 0<=page["offset"]<=10_000_000:
                    raise ValueError()
                offset=page["offset"]
            except (ValueError,KeyError,TypeError) as error:
                raise ValueError("Invalid cursor, changed query or changed dataset; restart pagination") from error
        if start>end or (end-start).days>20*366:
            raise ValueError("Invalid bar range (maximum 20 years)")
        frame=MarketDataRepository(path,read_only=True).load(symbols,start=start,end=end,
            columns=[mapping[f] for f in dict.fromkeys(["date","symbol",*fields])],limit=limit+1,offset=offset)
        next_cursor=base64.urlsafe_b64encode(canonical({"offset":offset+limit,"signature":signature}).encode()).decode() if frame.height>limit else None
        inverse={v:k for k,v in mapping.items()}
        return {"bars":records(frame.head(limit).rename(inverse,strict=False)),"nextCursor":next_cursor,
                "datasetRevision":signature,"runId":run_id,"priceBasis":"provider_close_and_adjusted_close"}
