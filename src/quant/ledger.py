"""Reconciled USD ledger performance with explicit end-of-day event timing."""
from datetime import date
import math

from quant.calendars import sessions
from quant.market_store import MarketDataRepository
from quant.record_store import RecordRepository


def money_weighted_return(cashflows):
    """Conventional dated cash flows only; do not arbitrarily select an IRR root."""
    amounts = [amount for _,amount in cashflows if amount]
    changes = sum(a*b < 0 for a,b in zip(amounts,amounts[1:]))
    if changes != 1 or not amounts or amounts[0] >= 0:
        return None
    first = cashflows[0][0]
    def npv(log_rate):
        return sum(amount*math.exp(-log_rate*((day-first).days/365)) for day,amount in cashflows)
    lo, hi = -10.0, 10.0
    if npv(lo)*npv(hi) > 0:
        return None
    for _ in range(100):
        middle = (lo+hi)/2
        if npv(middle) > 0:
            lo = middle
        else:
            hi = middle
    return math.expm1((lo+hi)/2)


def ledger_performance(opening, closing, events, prices, complete):
    """Replay quantities and cash, then gate performance on exact reconciliation.

    All cash flows are assumed to occur at day end. Actual execution timestamps
    are unavailable; this convention is exposed with every performance result.
    Prices must be contemporaneous unadjusted closes in the ledger share basis.
    """
    start, end = date.fromisoformat(opening["date"]), date.fromisoformat(closing["date"])
    if end <= start or opening["account"] != closing["account"]:
        raise ValueError("Ledger checkpoints must be chronological and in one account")
    if opening["currency"] != "USD" or closing["currency"] != "USD":
        raise ValueError("Ledger currently supports USD only")
    if opening["cash"] is None or closing["cash"] is None:
        raise ValueError("Ledger checkpoints require known cash")
    if any(p["marketValue"] is None for p in opening["positions"]+closing["positions"]):
        raise ValueError("Ledger checkpoints require observed position market values")
    shares = {p["symbol"]:p["quantity"] for p in opening["positions"]}
    cash = opening["cash"]
    initial = sum(p["marketValue"] for p in opening["positions"])+cash
    if initial <= 0:
        raise ValueError("Opening account value must be positive")
    expected = {p["symbol"]:p["quantity"] for p in closing["positions"]}
    by_day, seen = {}, set()
    for event in events:
        day = date.fromisoformat(event["date"])
        if event["sourceId"] in seen or not start < day <= end:
            raise ValueError("Duplicate transaction identity or event outside checkpoints")
        seen.add(event["sourceId"])
        by_day.setdefault(day,[]).append(event)
    lookup = {(r["Date"],r["Symbol"]):r["Close"] for r in prices.to_dicts()}
    days = sorted(set(sessions(start,end)) | set(by_day) | {end})
    series, missing, flows = [], [], [(start,-initial)]
    previous, growth = initial, 1.0
    for day in days:
        if day <= start:
            continue
        external = 0
        for event in by_day.get(day,[]):
            kind, symbol, amount = event["kind"], event["symbol"], event["amount"]
            if kind in {"buy","sell"}:
                sign = 1 if kind == "buy" else -1
                shares[symbol] = shares.get(symbol,0)+sign*event["quantity"]
                cash -= sign*amount
                if shares[symbol] < -1e-8:
                    raise ValueError("Transactions imply an unsupported short position")
            elif kind == "split":
                shares[symbol] = shares.get(symbol,0)*event["ratio"]
            elif kind in {"deposit","withdrawal","transfer_in","transfer_out"}:
                flow = amount*(1 if kind in {"deposit","transfer_in"} else -1)
                external += flow
                cash += flow
            else:
                cash += amount*(1 if kind == "dividend" else -1)
        if external:
            flows.append((day,-external))
        value = cash
        for symbol, quantity in shares.items():
            if abs(quantity) < 1e-8:
                continue
            price = lookup.get((day,symbol))
            if price is None:
                missing.append({"date":day.isoformat(),"symbol":symbol})
            else:
                value += price*quantity
        daily = (value-external)/previous-1 if previous > 0 and not missing else None
        if daily is not None:
            growth *= 1+daily
        series.append({"date":day.isoformat(),"cash":cash,"value":value if not missing else None,"externalFlow":external,"return":daily})
        previous = value
    differences = [{"symbol":s,"derived":shares.get(s,0),"observed":expected.get(s,0)} for s in sorted(set(shares)|set(expected))
                   if abs(shares.get(s,0)-expected.get(s,0)) > 1e-7]
    cash_difference = cash-closing["cash"]
    final = sum(p["marketValue"] for p in closing["positions"])+closing["cash"]
    value_difference = None if missing else previous-final
    reconciled = not differences and abs(cash_difference) < 0.01 and value_difference is not None and abs(value_difference) < 0.02
    eligible = bool(complete and reconciled and not missing and all(p["return"] is not None for p in series))
    flows.append((end,final))
    return {"methodologyVersion":"ledger-eod-v1", "date":end.isoformat(), "startDate":start.isoformat(),
            "reconciled":reconciled, "performanceAvailable":eligible,
            "endingPositions":{symbol:quantity for symbol,quantity in shares.items() if abs(quantity)>1e-8},
            "quantityDifferences":differences,"cashDifference":cash_difference,"valueDifference":value_difference,
            "missingPrices":missing,"history":series,"timeWeightedReturn":growth-1 if eligible else None,
            "moneyWeightedReturnAnnualized":money_weighted_return(flows) if eligible else None,
            "warnings":["Events and external flows are treated as end-of-day; execution timing is not modeled",
                        "Money-weighted return is withheld for nonconventional cash flows or no unique bracketed root"]
                       + ([] if complete else ["Transaction import not attested complete"])}


class LedgerService:
    def __init__(self,path):
        self.path, self.store = path, RecordRepository(path)

    def import_ledger(self, body):
        opening = self.store.get("snapshot",body.openingSnapshotId)["payload"]
        closing = self.store.get("snapshot",body.closingSnapshotId)["payload"]
        if opening["account"] != body.account or closing["account"] != body.account:
            raise ValueError("Ledger account must match both checkpoints")
        if closing["date"] <= opening["date"] or any(not opening["date"] < event.date.isoformat() <= closing["date"] for event in body.events):
            raise ValueError("Ledger events must fall strictly after opening and no later than closing checkpoint")
        if body.supersedes:
            prior = self.store.get("ledger",body.supersedes)["payload"]
            if prior["account"] != body.account:
                raise ValueError("Ledger correction must retain its account")
        return self.store.put("ledger",body.importKey,body.model_dump(mode="json"))

    def performance(self, identifier):
        ledger = self.store.get("ledger",identifier)["payload"]
        opening = self.store.get("snapshot",ledger["openingSnapshotId"])["payload"]
        closing = self.store.get("snapshot",ledger["closingSnapshotId"])["payload"]
        symbols = sorted({p["symbol"] for p in opening["positions"]+closing["positions"]} | {e["symbol"] for e in ledger["events"] if e["symbol"]})
        history = MarketDataRepository(self.path,read_only=True).load(symbols,start=date.fromisoformat(opening["date"]),end=date.fromisoformat(closing["date"]))
        # Yahoo Close is split-adjusted. Reconstruct the opening-share basis using
        # declared splits within this period; without a complete action ledger no
        # actual performance is claimed. Terminal values must still reconcile.
        import polars as pl
        for event in ledger["events"]:
            if event["kind"] == "split":
                history = history.with_columns(pl.when((pl.col("Symbol")==event["symbol"]) & (pl.col("Date")<date.fromisoformat(event["date"])))
                    .then(pl.col("Close")*event["ratio"]).otherwise(pl.col("Close")).alias("Close"))
        return {**ledger_performance(opening,closing,ledger["events"],history,ledger["complete"]),
                "provider":"yahoo","currency":"USD","priceBasis":"close_reconstructed_with_declared_splits"}


class AccountPerformanceService:
    """Persist daily NAV and risk from checkpoints and the internal transaction book."""
    def __init__(self, path, user_id):
        from quant.user_data import UserDataRepository
        self.path, self.user_id = path, user_id
        self.store = RecordRepository(path, user_id)
        self.users = UserDataRepository(path, user_id, read_only=True)

    def reported_nav(self, body):
        accounts=self.users.portfolio_state()['accounts']
        if not any(a['name']==body.account and a['currency']==body.currency for a in accounts):
            raise ValueError('Reported NAV requires a matching account and currency')
        return self.store.put('reported_nav',body.importKey,body.model_dump(mode='json'))

    def nav_records(self, account=None, limit=100, offset=0):
        from quant.database import is_database_initialized, database_connection
        if not 1<=limit<=1000 or offset<0:
            raise ValueError('Invalid NAV page')
        if not is_database_initialized(self.path):
            return []
        with database_connection(self.path,read_only=True) as db:
            rows=db.execute("SELECT * FROM records WHERE user_id=? AND kind IN ('nav','reported_nav') AND (? IS NULL OR COALESCE(json_extract(payload,'$.request.account'),json_extract(payload,'$.account'))=?) ORDER BY created_at,id LIMIT ? OFFSET ?",
                            (self.user_id,account,account,limit,offset)).fetchall()
        records=[]
        for row in rows:
            record=self.store._decode(row)
            payload=record['payload']
            records.append({k:record[k] for k in ('id','kind','createdAt')} | {'account':payload.get('request',payload)['account'],
                'date':payload.get('result',payload).get('date'), 'performanceAvailable':payload.get('result',{}).get('performanceAvailable')})
        return records

    def capture_nav(self, body):
        from quant.database import is_database_initialized
        request=body.model_dump(mode='json')
        if is_database_initialized(self.path):
            prior=self.store.by_key('nav',body.importKey)
            if prior:
                if prior['payload']['request']!=request:
                    raise ValueError('NAV identity already exists with different input')
                return prior
        result=self.calculate(body)
        return self.store.put('nav',body.importKey,{'request':request,'result':result})

    def calculate(self, body):
        import copy
        import hashlib
        import polars as pl
        from quant.analysis import actual_performance_metrics, calculate_daily_returns, account_profit_contributions
        from quant.record_store import canonical
        opening=copy.deepcopy(self.store.get('snapshot',body.openingSnapshotId)['payload'])
        closing=copy.deepcopy(self.store.get('snapshot',body.closingSnapshotId)['payload'])
        if opening['account']!=body.account or closing['account']!=body.account:
            raise ValueError('Both checkpoints must belong to the selected account')
        start,end=date.fromisoformat(opening['date']),date.fromisoformat(closing['date'])
        if end<=start or (end-start).days>3660:
            raise ValueError('NAV requires chronological checkpoints at most ten years apart')
        calendar_days=sessions(start,end)
        symbols=sorted({p['symbol'] for p in opening['positions']+closing['positions']})
        revision=self.users.portfolio_state()['revision']
        transactions=self.users.transactions(body.account,start,end,limit=10000)
        transactions=[t for t in transactions if t['date']>start.isoformat()]
        if len(transactions)>=9999:
            raise ValueError('NAV transaction limit reached')
        symbols=sorted(set(symbols)|{t['symbol'] for t in transactions if t['symbol']})
        market=MarketDataRepository(self.path,read_only=True)
        history=market.load([*symbols,body.benchmark],start=start,end=end,
                            columns=['Symbol','Date','Close','Adjusted Close','Retrieved At'],limit=100000)
        if history.height==100000:
            raise ValueError('NAV market-data limit reached; shorten the period')
        metadata,_=market.readiness_inputs([*symbols,body.benchmark],start,end)
        # Yahoo Close uses the latest share basis. Undo known splits, including
        # splits after these checkpoints, to recover each date's quantity basis.
        split_history=self.users.transactions(body.account,start,limit=10000)
        if len(split_history)==10000:
            raise ValueError('Split-history limit reached')
        stale_split_basis=[]
        for transaction in split_history:
            if transaction['kind']=='split':
                split_rows=history.filter(pl.col('Symbol')==transaction['symbol'])
                if any(str(value)[:10]<transaction['date'] for value in split_rows['Retrieved At'].to_list()):
                    stale_split_basis.append(transaction['symbol'])
                history=history.with_columns(pl.when((pl.col('Symbol')==transaction['symbol']) &
                    (pl.col('Date')<date.fromisoformat(transaction['date'])))
                    .then(pl.col('Close')*float(transaction['ratio'])).otherwise(pl.col('Close')).alias('Close'))
        lookup={(r['Symbol'],r['Date']):r['Close'] for r in history.to_dicts()}
        reasons=[]
        if stale_split_basis:
            reasons.append('Historical quotes were retrieved before a recorded split; synchronize their share basis before claiming actual performance')
        if any(metadata.get(s,{}).get('currency')!='USD' or metadata.get(s,{}).get('calendar')!='XNYS' for s in symbols):
            reasons.append('USD currency and XNYS calendar must be verified for every security')
        missing=[]
        discrepancies=[]
        for snapshot,day in ((opening,start),(closing,end)):
            for position in snapshot['positions']:
                close=lookup.get((position['symbol'],day))
                if close is None or close<=0:
                    missing.append({'date':day.isoformat(),'symbol':position['symbol']})
                else:
                    computed=position['quantity']*close
                    if position.get('marketValue') is not None and abs(computed-position['marketValue'])>0.02:
                        discrepancies.append({'date':day.isoformat(),'symbol':position['symbol'],
                                              'observed':position['marketValue'],'computed':computed})
                    if position.get('marketValue') is None:
                        position['marketValue']=computed
        corrections=self.users.corrections_between(body.account,start,end)
        if corrections:
            reasons.append('Opening-balance changes or corrections occur inside the period; reconcile them before claiming actual performance')
        if discrepancies:
            reasons.append('Checkpoint market values do not reconcile to stored prices')
        if missing:
            return {'date':end.isoformat(),'account':body.account,'currency':'USD','history':[],
                    'performanceAvailable':False,'risk':None,'missingPrices':missing,
                    'warnings':reasons+['Opening and closing NAV require stored checkpoint prices'],
                    'methodologyVersion':'account-nav-v1'}
        events=[{'sourceId':t['id'],'date':t['date'],'kind':t['kind'],'symbol':t['symbol'],
                 'quantity':float(t['quantity']) if t['quantity'] is not None else None,
                 'amount':float(t['amount']),'ratio':float(t['ratio']) if t['ratio'] is not None else None} for t in transactions]
        # Weekend cash events belong to the following session under the EOD convention.
        # Preserve original dates separately for audit; reject dates after the final session.
        for event in events:
            event_date=date.fromisoformat(event['date'])
            if event_date not in calendar_days:
                if event['kind'] in {'buy','sell','split'}:
                    raise ValueError('Exchange-traded transactions require a supported trading session')
                following=next((d for d in calendar_days if d>=event_date),None)
                if following is None:
                    raise ValueError('NAV end checkpoint must include the session following a non-session cash event')
                event['date']=following.isoformat()
        result=ledger_performance(opening,closing,events,history,body.complete and not reasons)
        if start not in calendar_days or end not in calendar_days:
            reasons.append('NAV checkpoints must be supported trading sessions')
            result['performanceAvailable']=False
        eligible=result['performanceAvailable']
        benchmark=history.filter(pl.col('Symbol')==body.benchmark)
        benchmark_returns=calculate_daily_returns(benchmark).select('Date','Return') if benchmark.height else pl.DataFrame(schema={'Date':pl.Date,'Return':pl.Float64})
        expected_dates={date.fromisoformat(row['date']) for row in result['history']}
        benchmark_complete=(expected_dates.issubset(set(benchmark_returns.drop_nulls('Return')['Date'].to_list()))
            and metadata.get(body.benchmark,{}).get('currency')=='USD' and metadata.get(body.benchmark,{}).get('calendar')=='XNYS')
        if not benchmark_complete:
            reasons.append('Benchmark coverage is incomplete; benchmark risk metrics are withheld')
        if eligible:
            risk=actual_performance_metrics(pl.DataFrame(result['history']).with_columns(pl.col('date').str.to_date()),benchmark_returns)
            if not benchmark_complete:
                risk.update(beta=None,annualizedAlpha=None)
        else:
            risk=None
        initial=sum(p['marketValue'] for p in opening['positions'])+opening['cash']
        rows=[{'date':start.isoformat(),'cash':opening['cash'],'value':initial,'externalFlow':0,'return':None},*result['history']]
        contribution=[]
        if eligible:
            checkpoints=lambda snapshot:pl.DataFrame([{'symbol':p['symbol'],'marketValue':p['marketValue']} for p in snapshot['positions']],schema={'symbol':pl.String,'marketValue':pl.Float64})
            pnl=[]
            for event in events:
                if event['kind'] in {'deposit','withdrawal','transfer_in','transfer_out','split'}:
                    continue
                sign=-1 if event['kind'] in {'buy','fee'} else 1
                pnl.append({'symbol':event['symbol'] or 'Unattributed cash income/fees','pnlCash':sign*event['amount']})
            contribution=account_profit_contributions(checkpoints(opening),checkpoints(closing),pl.DataFrame(pnl,schema={'symbol':pl.String,'pnlCash':pl.Float64})).to_dicts()
        used_prices=[{**r,'Date':r['Date'].isoformat()} for r in history.to_dicts()]
        if self.users.portfolio_state()['revision']!=revision:
            raise ValueError('Portfolio changed during NAV calculation; retry with a new read')
        if not eligible:
            result.update(timeWeightedReturn=None,moneyWeightedReturnAnnualized=None)
        return {**result,'history':rows,'risk':risk,'profitContributions':contribution,
                'warnings':result['warnings']+reasons,'checkpointDifferences':discrepancies,'corrections':corrections,
                'account':body.account,'currency':'USD','provider':'yahoo','priceBasis':'close_reconstructed_with_declared_splits',
                'sourceRevision':revision,'methodologyVersion':'account-nav-v1','transactionIds':[t['id'] for t in transactions],
                'priceDigest':hashlib.sha256(canonical(used_prices).encode()).hexdigest(),'prices':used_prices,
                'conventions':{'annualization':252,'riskFreeRate':0,'events':'end_of_day',
                    'nonSessionCashFlows':'next trading session','returns':'fractions',
                    'profitContributions':'dollar P&L, not additive multi-period return contributions'}}

    def get_nav(self, identifier):
        record=self.store.get('nav',identifier)
        result=record['payload']['result']
        reported=[]
        from quant.database import database_connection
        values={row['date']:row['value'] for row in result['history']}
        first=min(values) if values else result['date']
        with database_connection(self.path,read_only=True) as db:
            rows=db.execute("SELECT * FROM records WHERE user_id=? AND kind='reported_nav' AND json_extract(payload,'$.account')=? AND json_extract(payload,'$.date') BETWEEN ? AND ? ORDER BY created_at,id",
                            (self.user_id,result['account'],first,result['date'])).fetchall()
        for row in rows:
            observation=self.store._decode(row)
            value=float(observation['payload']['value'])
            day=observation['payload']['date']
            computed=values.get(day)
            reported.append({'id':observation['id'],'date':day,'value':value,'source':observation['payload']['source'],
                             'difference':computed-value if computed is not None else None})
        return {**record,'reportedComparisons':reported}

    def performance(self, identifier):
        record=self.get_nav(identifier)
        result=record['payload']['result']
        comparisons=record['reportedComparisons']
        latest={comparison['date']:comparison for comparison in comparisons}
        mismatch=any(comparison['difference'] is not None and abs(comparison['difference'])>0.02 for comparison in latest.values())
        available=result['performanceAvailable'] and not mismatch
        return {'id':identifier,'date':result['date'],'account':result['account'],'currency':'USD',
                'performanceAvailable':available,'reconciled':result.get('reconciled',False) and not mismatch,
                'risk':result.get('risk') if available else None,
                'timeWeightedReturn':result.get('timeWeightedReturn') if available else None,
                'moneyWeightedReturnAnnualized':result.get('moneyWeightedReturnAnnualized') if available else None,
                'profitContributions':result.get('profitContributions',[]) if available else [],
                'reportedComparisons':comparisons,'warnings':result['warnings']+(['Latest broker NAV does not reconcile; actual-performance metrics are withheld.'] if mismatch else []),
                'methodologyVersion':result['methodologyVersion'],'priceBasis':result.get('priceBasis'),
                'priceDigest':result.get('priceDigest'),'sourceRevision':result.get('sourceRevision')}
