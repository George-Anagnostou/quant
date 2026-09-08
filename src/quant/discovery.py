"""Stored market discovery and bounded research experiments."""
from datetime import date
from pathlib import Path

import polars as pl

from quant.analysis import summarize_risk_metrics
from quant.database import database_connection
from quant.market_store import MarketDataRepository
from quant.record_store import RecordRepository
from quant.workflows import ResearchWorkflow, records


class DiscoveryService:
    def __init__(self,path):
        self.path, self.store = path, RecordRepository(path)

    def universes(self):
        with database_connection(self.path,read_only=True) as db:
            return [dict(r) for r in db.execute("SELECT universe_id AS universe,observed_on AS date,COUNT(*) AS members FROM universe_memberships GROUP BY universe_id,observed_on ORDER BY universe_id,observed_on")]

    def members(self,universe,as_of):
        with database_connection(self.path,read_only=True) as db:
            observed = db.execute("SELECT MAX(observed_on) FROM universe_memberships WHERE universe_id=? AND observed_on<=?",(universe,as_of)).fetchone()[0]
            if not observed:
                raise ValueError("No observed universe membership at or before requested date")
            rows = db.execute("SELECT s.symbol,s.company FROM universe_memberships m JOIN securities s ON m.security_id=s.id WHERE m.universe_id=? AND m.observed_on=? ORDER BY m.sort_order",(universe,observed)).fetchall()
        return {"universe":universe,"observedOn":observed,"members":[dict(r) for r in rows],
                "warning":"Membership is observed, not reconstructed point-in-time history"}

    def breadth(self,universe,as_of):
        members = self.members(universe,as_of)
        symbols = [r["symbol"] for r in members["members"]]
        end = date.fromisoformat(as_of)
        from datetime import timedelta
        history = MarketDataRepository(self.path,read_only=True).load(symbols,start=end-timedelta(days=450),end=end).drop_nulls("Adjusted Close")
        rows = []
        for symbol in symbols:
            series = history.filter(pl.col("Symbol")==symbol).sort("Date")
            if series.height:
                prices = series["Adjusted Close"]
                from quant.calendars import sessions
                def covered(count):
                    return len(prices)>=count and series["Date"].tail(count).to_list()==sessions(series["Date"][-count],series["Date"][-1])
                rows.append({"symbol":symbol,"date":series["Date"][-1].isoformat(),
                    "above50":bool(prices[-1]>prices.tail(50).mean()) if covered(50) else None,
                    "above200":bool(prices[-1]>prices.tail(200).mean()) if covered(200) else None,
                    "return1m":prices[-1]/prices[-22]-1 if covered(22) else None})
        from quant.calendars import sessions
        session = sessions(end-timedelta(days=14),end)[-1].isoformat()
        eligible = [r for r in rows if r["date"]==session]
        ranking = sorted([r for r in eligible if r["return1m"] is not None],key=lambda r:(r["return1m"],r["symbol"]))
        for row in rows:
            row["relativeStrengthPercentile"] = None
        for index,row in enumerate(ranking):
            row["relativeStrengthPercentile"] = index/(len(ranking)-1) if len(ranking)>1 else None
        summary = {}
        for name in ("above50","above200"):
            available = [r[name] for r in eligible if r[name] is not None]
            summary[name] = sum(available)/len(available) if available else None
            summary[name+"Coverage"] = len(available)/len(symbols) if symbols else 0
        return {**members,"date":session,"breadth":summary,"rows":rows,"universeSize":len(symbols)}

    def scenario(self,body):
        snapshot = self.store.get("snapshot",body.snapshotId)["payload"]
        if snapshot["currency"] != "USD" or any(p["currency"]!="USD" for p in snapshot["positions"]):
            raise ValueError("Scenarios require USD positions")
        if any(p["marketValue"] is None for p in snapshot["positions"]):
            raise ValueError("Scenario requires observed position market values")
        known = {p["symbol"] for p in snapshot["positions"]}
        if set(body.shocks)-known or any(value < -1 for value in body.shocks.values()):
            raise ValueError("Shock symbols must be held and losses cannot exceed 100%")
        rows = [{"symbol":p["symbol"],"shock":body.shocks.get(p["symbol"],0),
                 "valueChange":p["marketValue"]*body.shocks.get(p["symbol"],0)} for p in snapshot["positions"]]
        return {"positions":rows,"valueChange":sum(r["valueChange"] for r in rows),
                "assumptions":"Unspecified positions and cash unchanged; deterministic hypothetical shock, not a forecast"}

    def factors(self,symbol,factors,start,end):
        if not 1 <= len(factors) <= 5 or len(set(factors)) != len(factors) or symbol in factors:
            raise ValueError("Specify 1-5 distinct factor proxy symbols excluding the asset")
        prices = MarketDataRepository(self.path,read_only=True).load([symbol,*factors],start=start,end=end).drop_nulls("Adjusted Close")
        # Compute returns only after aligning prices, then reject missing daily sessions.
        wide = prices.pivot(on="Symbol",index="Date",values="Adjusted Close").sort("Date")
        if any(s not in wide.columns for s in [symbol,*factors]):
            raise ValueError("Factor history unavailable")
        wide = wide.drop_nulls([symbol,*factors])
        from quant.calendars import sessions
        if wide.height < 61 or wide["Date"].to_list()!=sessions(wide["Date"][0],wide["Date"][-1]):
            raise ValueError("Factor regression needs at least 60 complete daily observations")
        returns = wide.select([(pl.col(s)/pl.col(s).shift(1)-1).alias(s) for s in [symbol,*factors]]).drop_nulls()
        x = [[1.0,*[r[s] for s in factors]] for r in returns.to_dicts()]
        y = returns[symbol].to_list()
        k = len(factors)+1
        system = [[sum(r[i]*r[j] for r in x) for j in range(k)]+[sum(r[i]*v for r,v in zip(x,y))] for i in range(k)]
        coefficients = solve(system)
        residual = sum((v-sum(a*b for a,b in zip(r,coefficients)))**2 for r,v in zip(x,y))
        total = sum((v-sum(y)/len(y))**2 for v in y)
        return {"symbol":symbol,"proxies":dict(zip(factors,coefficients[1:])),"annualizedIntercept":coefficients[0]*252,
                "rSquared":1-residual/total if total else None,"observations":len(y),"methodologyVersion":"ols-proxy-v1",
                "warnings":["ETF/security returns are factor proxies, not pure academic factors", "Coefficients are descriptive historical estimates"]}

    def experiment(self,body):
        run = self.store.get("run",body.runId)["payload"]
        path = ResearchWorkflow(self.path).dataset(run)
        prices = MarketDataRepository(path,read_only=True).load([body.symbol],end=date.fromisoformat(run["snapshot"]["date"])).drop_nulls("Adjusted Close")
        result = momentum_experiment(prices,body.trainingEnd,body.lookback,body.costBps)
        return self.store.put("experiment",body.importKey,{"request":body.model_dump(mode="json"),"result":result,
            "calculationSource":Path(__file__).read_text(),
            "datasetSha256":run["datasetSha256"],"warning":"Universe is the frozen dataset's selected securities; survivorship and selection bias are not corrected"})

    def replay_experiment(self,identifier):
        from pathlib import Path
        from quant.record_store import canonical
        payload=self.store.get("experiment",identifier)["payload"]
        if payload["calculationSource"]!=Path(__file__).read_text():
            raise ValueError("Experiment replay requires its preserved source revision")
        request=payload["request"]
        run=self.store.get("run",request["runId"])["payload"]
        path=ResearchWorkflow(self.path).dataset(run)
        prices=MarketDataRepository(path,read_only=True).load([request["symbol"]],end=date.fromisoformat(run["snapshot"]["date"])).drop_nulls("Adjusted Close")
        result=momentum_experiment(prices,date.fromisoformat(request["trainingEnd"]),request["lookback"],request["costBps"])
        return {"matches":canonical(result)==canonical(payload["result"]),"result":result}

    def evaluate(self,body):
        report = self.store.get("report",body.reportId)
        return self.store.put("evaluation",body.importKey,{**body.model_dump(mode="json"),
            "runId":report["payload"]["runId"],"model":report["payload"]["model"],
            "scoreSource":"Explicit evaluator rubric; Quant does not certify subjective scores"})

    def evaluations(self,run_id):
        with database_connection(self.path,read_only=True) as db:
            rows = db.execute("""SELECT payload FROM records WHERE user_id=? AND kind='evaluation'
                AND json_extract(payload,'$.runId')=? ORDER BY created_at,id LIMIT 1000""",
                (self.store.user_id,run_id)).fetchall()
        import json
        if not rows:
            return {"runId":run_id,"models":[]}
        frame=pl.DataFrame([json.loads(r[0]) for r in rows])
        metrics=["factualAccuracy","numericalConsistency","evidenceUse","missingDataHandling","costUsd"]
        summary=frame.group_by("model").agg(pl.len().alias("evaluations"),*[pl.col(m).mean() for m in metrics]).sort("model")
        return {"runId":run_id,"models":records(summary),"warning":"Averages of explicit reviewer scores on one frozen case; not measured investment performance"}


def solve(matrix):
    n=len(matrix)
    for i in range(n):
        pivot=max(range(i,n),key=lambda j:abs(matrix[j][i]))
        matrix[i],matrix[pivot]=matrix[pivot],matrix[i]
        if abs(matrix[i][i]) < 1e-12:
            raise ValueError("Factor regression is singular or ill-conditioned")
        scale=matrix[i][i]
        matrix[i]=[v/scale for v in matrix[i]]
        for j in range(n):
            if i != j:
                scale=matrix[j][i]
                matrix[j]=[a-scale*b for a,b in zip(matrix[j],matrix[i])]
    return [r[-1] for r in matrix]


def momentum_experiment(prices,training_end,lookback,cost_bps):
    from quant.calendars import sessions
    prices=prices.sort("Date")
    if prices.height<lookback+3 or prices["Date"].to_list()!=sessions(prices["Date"][0],prices["Date"][-1]):
        raise ValueError("Experiment requires contiguous session history and sufficient warm-up")
    dates,values=prices["Date"].to_list(),prices["Adjusted Close"].to_list()
    if sum(d<=training_end for d in dates)<lookback+2 or sum(d>training_end for d in dates)<20:
        raise ValueError("Require warm-up before trainingEnd and at least 20 out-of-sample sessions")
    signals=[int(values[i]>values[i-lookback]) if i>=lookback else 0 for i in range(len(values))]
    rows=[]
    for i in range(1,len(values)):
        held=signals[i-2] if i>=2 else 0
        next_position=signals[i-1]
        gross=held*(values[i]/values[i-1]-1)
        net=(1+gross)*(1-abs(next_position-held)*cost_bps/10000)-1
        rows.append({"Date":dates[i],"Symbol":"Strategy","Return":net,"Held":held})
    frame=pl.DataFrame(rows)
    return {"methodologyVersion":"momentum-next-close-v1","execution":"Signal at close t; execute close t+1; accrue position returns from t+1 to t+2",
            "training":records(summarize_risk_metrics(frame.filter(pl.col("Date")<=training_end))),
            "outOfSample":records(summarize_risk_metrics(frame.filter(pl.col("Date")>training_end))),
            "history":records(frame),"costBps":cost_bps,"parametersSelectedAutomatically":False,
            "warnings":["Fixed hypothesis test, not evidence of predictive alpha", "Adjusted-close execution proxy; liquidity, taxes and slippage beyond stated costs are not modeled"]}
