"""Application adapter for agent-managed current holdings and research snapshots."""
from datetime import date

from quant.analysis import aggregate_lots
from quant.contracts import SnapshotInput
from quant.user_data import UserDataRepository
from quant.workflows import ResearchWorkflow


class PortfolioService:
    def __init__(self, path, user_id):
        self.repository = UserDataRepository(path, user_id, read_only=True)

    def show(self):
        state = self.repository.portfolio_state()
        return {**state, 'positions': aggregate_lots(state['lots']),
                'conventions': {'money': 'decimal strings', 'totalCost': 'total remaining lot basis including fees',
                                'cash': 'null means unknown', 'analysisCurrency': 'USD',
                                'history': 'internal events only; opening lots are not inferred purchases'}}

    def preview(self, body):
        return self.repository.preview_portfolio(body)

    def apply(self, body):
        return UserDataRepository(self.repository.path, self.repository.user_id).apply_portfolio(body)

    def history(self, limit=100, offset=0):
        return self.repository.portfolio_history(limit, offset)

    def snapshot(self, body):
        state = self.show()
        if body.expectedRevision != state['revision']:
            raise ValueError('Portfolio revision conflict; reread current holdings')
        account = next((a for a in state['accounts'] if a['name'] == body.account), None)
        if account is None:
            raise ValueError('Unknown account')
        if account.get('asOf') != body.date.isoformat():
            raise ValueError('Snapshot date must match account state date; current holdings cannot reconstruct past holdings')
        positions = [p for p in state['positions'] if p['account'] == body.account]
        snapshot = SnapshotInput(importKey=body.importKey, date=body.date, account=body.account,
            source=f"portfolio revision {state['revision']}", currency=account['currency'], cash=account.get('cash'),
            positions=[{'symbol': p['symbol'], 'quantity': p['quantity'], 'averageCost': p['averageCost'],
                        'currency': p['currency'], 'assetClass': p.get('assetClass'), 'sector': p.get('sector'), 'strategy': p.get('strategy')}
                       for p in positions])
        # This shares the existing immutable research contract; it never applies holdings.
        workflow = ResearchWorkflow(self.repository.path, self.repository.user_id)
        return workflow.import_snapshot(snapshot)

    def transactions(self, account=None, start=None, end=None, limit=100, offset=0):
        if start and end and start > end:
            raise ValueError('Start date must not follow end date')
        return self.repository.transactions(account, start, end, limit, offset)

    def analysis(self, account=None, as_of=None):
        from quant.calendars import latest_completed_session
        from quant.analysis import aggregate_portfolio_values, portfolio_value_summary, value_portfolio_lots
        day = as_of or latest_completed_session()
        state, frame, quotes = self._analysis_inputs(account, day)
        valued = value_portfolio_lots(frame, quotes, day)
        summary = portfolio_value_summary(valued, state['accounts'])
        transactions = self.transactions(account, end=day, limit=10000)
        if len(transactions) == 10000:
            raise ValueError('Analysis transaction limit reached; select a smaller account scope')
        realized = {}
        for transaction in transactions:
            if transaction['realizedGain'] is not None:
                realized[transaction['symbol']] = realized.get(transaction['symbol'],0) + float(transaction['realizedGain'])
        positions = aggregate_portfolio_values(valued,'symbol',summary['accountValue']).to_dicts()
        quote_fields = {row['symbol']: row for row in valued.select('symbol','lastPrice','priceDate','dayReturn').unique().to_dicts()}
        for position in positions:
            position.update(quote_fields[position['symbol']])
            position['recordedRealizedGain'] = realized.get(position['symbol'],0)
        summary['recordedRealizedGain'] = sum(realized.values())
        allocations = {dimension: [{'name':row[dimension], **{key:value for key,value in row.items() if key not in {dimension,'quantity','averageCost'}}}
                                  for row in aggregate_portfolio_values(valued,dimension,summary['accountValue']).to_dicts()]
                       for dimension in ('account','assetClass','strategy','sector')}
        warnings = []
        if summary['unpricedLotCount']:
            warnings.append('Some lots lack a positive close on the requested date or verified USD currency. Full valuation and weights are withheld.')
        if summary['cash'] is None:
            warnings.append('Cash is unknown in at least one account; account value and weights are withheld.')
        return {'date':day.isoformat(),'revision':state['revision'],'account':account,'currency':'USD',
                'positions':positions,'lots':valued.to_dicts(),'summary':summary,'allocations':allocations,
                'accounts':state['accounts'],'recordedRealizedGainsBySymbol':realized,
                'methodologyVersion':'portfolio-analysis-v1','provider':'yahoo','priceBasis':'close',
                'warnings':warnings,'conventions':{'returns':'fractions','weights':'fraction of securities plus cash',
                    'unrealizedReturn':'gain on remaining cost basis, not account performance',
                    'dayGain':'current quantity times session price change; not adjusted for intraday trades',
                    'recordedRealizedGain':'all recorded sales through date; not asserted complete'}}

    def _analysis_inputs(self, account, day):
        import polars as pl
        from datetime import timedelta
        from quant.calendars import sessions
        from quant.market_store import MarketDataRepository
        state = self.repository.portfolio_state()
        if day > date.today():
            raise ValueError('Analysis date cannot be in the future')
        if account:
            state = {**state, 'accounts':[a for a in state['accounts'] if a['name']==account],
                     'lots':[lot for lot in state['lots'] if lot['account']==account]}
            if not state['accounts']:
                raise ValueError('Unknown account')
        if any(a['currency']!='USD' for a in state['accounts']):
            raise ValueError('Portfolio analysis supports USD accounts only')
        if any(a.get('asOf') and a['asOf']>day.isoformat() for a in state['accounts']):
            raise ValueError('Current holdings cannot be valued before their state date; use retained NAV for history')
        frame = portfolio_lots_frame(state['lots'])
        symbols = frame['symbol'].unique().to_list()
        days = sessions(day-timedelta(days=14),day)
        previous = max((d for d in days if d<day), default=None)
        start = previous or day
        quote_rows=[]
        if symbols:
            market = MarketDataRepository(self.repository.path,read_only=True)
            metadata,_ = market.readiness_inputs(symbols,start,day)
            history = market.load(symbols,start=start,end=day,columns=['Symbol','Date','Close'])
            lookup={(row['Symbol'],row['Date']):row['Close'] for row in history.to_dicts()}
            for symbol in symbols:
                verified = metadata.get(symbol,{}).get('currency')=='USD'
                close=lookup.get((symbol,day)) if verified else None
                prior=lookup.get((symbol,previous)) if verified else None
                quote_rows.append({'symbol':symbol,'lastPrice':close if close and close>0 else None,
                                   'previousClose':prior if prior and prior>0 else None,
                                   'priceDate':day.isoformat() if close and close>0 else None})
        quotes = pl.DataFrame(quote_rows,schema={'symbol':pl.String,'lastPrice':pl.Float64,'previousClose':pl.Float64,'priceDate':pl.String})
        return state, frame, quotes

    def scenario(self, body):
        import polars as pl
        from quant.analysis import portfolio_shock, value_portfolio_lots, portfolio_value_summary
        state,frame,quotes=self._analysis_inputs(body.account,body.date)
        valued=value_portfolio_lots(frame,quotes,body.date)
        summary=portfolio_value_summary(valued,state['accounts'])
        if summary['accountValue'] is None:
            raise ValueError('Scenario requires known cash and complete valuations')
        scenario=portfolio_shock(valued,body.dimension,body.shocks)
        rows=scenario.group_by('symbol').agg(pl.col('marketValue').sum(),pl.col('valueChange').sum(),pl.col('scenarioValue').sum()).to_dicts()
        change=scenario['valueChange'].sum() or 0.0
        return {'date':body.date.isoformat(),'revision':state['revision'],'currency':'USD','positions':rows,
                'startingValue':summary['accountValue'],'valueChange':change,'scenarioValue':summary['accountValue']+change,
                'return':change/summary['accountValue'] if summary['accountValue']>0 else None,
                'warnings':['Hypothetical one-step price shocks; cash unchanged; no forecast probabilities or tax costs.']}

    def tax_lots(self, account=None, as_of=None):
        data=self.analysis(account,as_of)
        return {'date':data['date'],'revision':data['revision'],'lots':data['lots'],
                'realizedSales':[t for t in self.transactions(account,end=date.fromisoformat(data['date']),limit=10000) if t['kind']=='sell'],
                'warnings':data['warnings']+['Holding days and gains are descriptive. Tax rates, wash-sale treatment, and tax liability are not calculated.']}

    def simulate_sales(self, body):
        import polars as pl
        from quant.analysis import simulate_lot_sales
        state=self.repository.portfolio_state()
        lots=[lot for lot in state['lots'] if lot['account']==body.account]
        account=next((a for a in state['accounts'] if a['name']==body.account),None)
        if not account or account['currency']!='USD':
            raise ValueError('Select a known USD account')
        if body.date > date.today() or account.get('asOf') and body.date.isoformat()<account['asOf']:
            raise ValueError('Sale simulation date must be between account state date and today')
        rows=[]
        for sale in body.sales:
            identifier=sale.lotId
            if identifier is None:
                matches=[lot['id'] for lot in lots if lot.get('sourceLotId')==sale.sourceLotId and lot['account']==sale.account]
                if len(matches)!=1:
                    raise ValueError('Source lot selector must match one active lot in the selected account')
                identifier=matches[0]
            rows.append({'lotId':identifier,'sellQuantity':float(sale.quantity),'unitPrice':float(sale.unitPrice),'fees':float(sale.fees)})
        sales=pl.DataFrame(rows)
        result=simulate_lot_sales(portfolio_lots_frame(lots),sales,body.date)
        return {'date':body.date.isoformat(),'revision':state['revision'],'currency':'USD','sales':result.to_dicts(),
                'proceeds':result['proceeds'].sum(),'realizedGain':result['realizedGain'].sum(),
                'warnings':['Simulation only; holdings and cash are unchanged. No tax rates or wash-sale treatment assumed.']}


def portfolio_lots_frame(lots):
    import polars as pl
    schema={'id':pl.String,'symbol':pl.String,'account':pl.String,'quantity':pl.Float64,'totalCost':pl.Float64,
            'acquired':pl.String,'assetClass':pl.String,'strategy':pl.String,'sector':pl.String}
    return pl.DataFrame([{key:float(lot[key]) if key in {'quantity','totalCost'} else lot.get(key) for key in schema} for lot in lots],schema=schema).with_columns(
        pl.col('assetClass','strategy','sector').fill_null('Unclassified'))
