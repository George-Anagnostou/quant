import json
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import polars as pl

from quant.database import database_connection
from quant.market_store import MarketDataRepository
from quant.user_data import UserDataRepository
from quant.portfolio_service import PortfolioService
from quant.portfolio_contracts import (PortfolioWrite, TransactionImport, HoldingsScenario, SaleSimulation,
    NavInput, ReportedNavInput, CurrentSnapshotInput, PortfolioAnalysisData)
from quant.contracts import SnapshotInput
from quant.workflows import ResearchWorkflow
from quant.ledger import AccountPerformanceService
from tests.test_research_engine import request


class PortfolioAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'quant.db'
        self.repo=UserDataRepository(self.path)
        self.service=PortfolioService(self.path,'local-admin')
        self.performance=AccountPerformanceService(self.path,'local-admin')
        self.base={'importKey':'opening','expectedRevision':0,'effectiveDate':'2026-08-24',
                   'source':'fixture','reason':'Opening observed portfolio','actions':[
                       {'action':'account','account':{'name':'Main'}},
                       {'action':'cash','account':'Main','amount':'100'},
                       {'action':'add','lot':{'symbol':'AAPL','account':'Main','quantity':'10','totalCost':'80',
                        'acquired':'2025-01-10','sector':'Technology','strategy':'Growth','assetClass':'Equity'}}]}
        result=self.repo.apply_portfolio(PortfolioWrite(**self.base))
        self.lot_id=result['after']['lots'][0]['id']
        self.days=[date(2026,8,24)+timedelta(days=i) for i in range(5)]
        rows=[{'Date':day,'Symbol':symbol,'Close':value,'Adjusted Close':value} for symbol,values in
              [('AAPL',[10.,12.,11.,14.,15.]),('SPY',[100.,101.,100.,102.,103.])]
              for day,value in zip(self.days,values)]
        MarketDataRepository(self.path).save(pl.DataFrame(rows))
        with database_connection(self.path) as db:
            db.execute("UPDATE securities SET currency='USD',calendar='XNYS'")

    def transaction_import(self):
        return TransactionImport(importKey='import-1',expectedRevision=1,source='Broker transactions',reason='Import ordered activity',transactions=[
            {'sourceId':'b1','date':'2026-08-25','entry':{'action':'buy','lot':{'symbol':'AAPL','account':'Main','quantity':'2','totalCost':'24',
                'acquired':'2026-08-25','sector':'Technology','strategy':'Growth','assetClass':'Equity'}}},
            {'sourceId':'d1','date':'2026-08-26','entry':{'action':'deposit','account':'Main','amount':'50'}},
            {'sourceId':'s1','date':'2026-08-27','entry':{'action':'sell','lotId':self.lot_id,'quantity':'3','unitPrice':'14','fees':'1'}}])

    def capture(self, complete=True):
        opening=self.service.snapshot(CurrentSnapshotInput(importKey='open-snap',expectedRevision=1,account='Main',date='2026-08-24'))
        self.repo.apply_portfolio(self.transaction_import())
        closing=ResearchWorkflow(self.path).import_snapshot(SnapshotInput(importKey='close-snap',date='2026-08-28',account='Main',source='broker closing fixture',cash=167,
            positions=[{'symbol':'AAPL','quantity':9,'averageCost':80/9}]))
        body=NavInput(importKey='nav-complete' if complete else 'nav-incomplete',account='Main',openingSnapshotId=opening['id'],closingSnapshotId=closing['id'],complete=complete)
        return self.performance.capture_nav(body),body

    def test_ticker_values_cash_strategy_sector_and_units(self):
        data=self.service.analysis('Main',date(2026,8,24))
        PortfolioAnalysisData.model_validate(data)
        self.assertEqual(data['summary']['accountValue'],200)
        self.assertEqual(data['positions'][0]['unrealizedReturn'],0.25)
        self.assertEqual(data['positions'][0]['weight'],0.5)
        self.assertEqual(data['allocations']['sector'][0]['name'],'Technology')
        self.assertEqual(data['allocations']['strategy'][0]['name'],'Growth')
        self.assertNotIn('averageCost',data['allocations']['sector'][0])

    def test_transaction_import_preview_atomicity_idempotency_and_realized_gain(self):
        body=self.transaction_import()
        preview=self.repo.preview_portfolio(body)
        self.assertEqual(self.repo.portfolio_state()['revision'],1)
        result=self.repo.apply_portfolio(body)
        self.assertEqual(result,preview)
        self.assertEqual(result['after']['revision'],2)
        self.assertEqual(Decimal(result['after']['accounts'][0]['cash']),167)
        events=self.repo.transactions('Main')
        self.assertEqual([t['kind'] for t in events],['buy','deposit','sell'])
        self.assertEqual(Decimal(events[-1]['realizedGain']),17)
        self.assertEqual(events[-1]['sourceId'],'s1')
        self.assertEqual(self.repo.apply_portfolio(body),result)
        self.assertEqual(len(self.repo.transactions()),3)
        repeated=body.model_copy(update={'importKey':'overlapping','expectedRevision':2})
        with self.assertRaisesRegex(ValueError,'already imported'):
            self.repo.apply_portfolio(repeated)

    def test_import_conflict_failure_and_unsorted_events(self):
        body=self.transaction_import()
        bad=body.model_dump(mode='json');bad['transactions'][-1]['entry']['quantity']='999'
        with self.assertRaises(ValueError): self.repo.apply_portfolio(TransactionImport(**bad))
        self.assertEqual(self.repo.portfolio_state()['revision'],1)
        self.assertEqual(self.repo.transactions(),[])
        with self.assertRaisesRegex(ValueError,'revision conflict'):
            self.repo.apply_portfolio(body.model_copy(update={'expectedRevision':0}))
        bad=body.model_dump(mode='json');bad['transactions'].reverse()
        with self.assertRaisesRegex(ValueError,'ordered'):
            TransactionImport(**bad)

    def test_missing_prices_or_currency_never_create_complete_totals(self):
        data=self.service.analysis('Main',date(2026,8,31))
        self.assertIsNone(data['summary']['accountValue'])
        self.assertIsNone(data['positions'][0]['weight'])
        self.assertEqual(data['summary']['unpricedLotCount'],1)
        with database_connection(self.path) as db: db.execute("UPDATE securities SET currency=NULL")
        self.assertIsNone(self.service.analysis('Main',date(2026,8,24))['summary']['securitiesValue'])

    def test_actual_nav_risk_and_dollar_contribution_reconcile(self):
        record,body=self.capture()
        result=record['payload']['result']
        self.assertTrue(result['performanceAvailable'],result)
        self.assertEqual([row['value'] for row in result['history']],[200,220,258,293,302])
        self.assertEqual(result['history'][2]['externalFlow'],50)
        self.assertAlmostEqual(result['history'][2]['return'],208/220-1)
        self.assertEqual(result['risk']['observations'],4)
        self.assertIsNotNone(result['risk']['annualizedVolatility'])
        self.assertIsNotNone(result['risk']['beta'])
        self.assertAlmostEqual(sum(row['profitContribution'] for row in result['profitContributions']),52)
        self.assertEqual(self.performance.capture_nav(body),record)
        # Retained NAV remains unchanged after a provider correction.
        MarketDataRepository(self.path).save(pl.DataFrame({'Date':[self.days[-1]],'Symbol':['AAPL'],'Close':[100.],'Adjusted Close':[100.]}))
        self.assertEqual(self.performance.get_nav(record['id'])['payload']['result']['history'][-1]['value'],302)

    def test_actual_metrics_withheld_for_incomplete_history(self):
        record,_=self.capture(False)
        result=record['payload']['result']
        self.assertFalse(result['performanceAvailable'])
        self.assertIsNone(result['risk'])
        self.assertIsNone(result['timeWeightedReturn'])
        self.assertEqual(result['profitContributions'],[])

    def test_missing_daily_prices_withhold_actual_performance(self):
        with database_connection(self.path) as db:
            db.execute("DELETE FROM daily_bars WHERE session_date='2026-08-26' AND security_id=(SELECT id FROM securities WHERE symbol='AAPL')")
        record,_=self.capture()
        result=record['payload']['result']
        self.assertFalse(result['performanceAvailable'])
        self.assertIsNone(result['risk'])
        self.assertTrue(result['missingPrices'])

    def test_cash_correction_inside_period_is_not_a_return(self):
        record,body=self.capture()
        self.repo.apply_portfolio(PortfolioWrite(importKey='correct',expectedRevision=2,effectiveDate='2026-08-28',source='test',reason='balance correction',
                                               actions=[{'action':'cash','account':'Main','amount':'167'}]))
        result=self.performance.capture_nav(body.model_copy(update={'importKey':'corrected-period'}))['payload']['result']
        self.assertFalse(result['performanceAvailable'])
        self.assertTrue(result['corrections'])

    def test_reported_nav_is_separate_and_comparable(self):
        record,_=self.capture()
        observed=self.performance.reported_nav(ReportedNavInput(importKey='broker-nav',account='Main',date='2026-08-28',value='301',source='Broker statement'))
        result=self.performance.get_nav(record['id'])
        self.assertEqual(result['reportedComparisons'][0]['difference'],1)
        self.assertEqual(result['reportedComparisons'][0]['id'],observed['id'])
        self.assertEqual(result['payload']['result']['history'][-1]['value'],302)

    def test_scenario_and_sale_simulation_are_read_only(self):
        before=self.repo.portfolio_state()
        result=self.service.scenario(HoldingsScenario(account='Main',date='2026-08-24',dimension='sector',shocks={'Technology':-0.2}))
        self.assertEqual(result['valueChange'],-20)
        self.assertEqual(result['scenarioValue'],180)
        sale=self.service.simulate_sales(SaleSimulation(account='Main',date='2026-08-24',sales=[{'action':'sell','lotId':self.lot_id,'quantity':'3','unitPrice':'14','fees':'1'}]))
        self.assertEqual(sale['realizedGain'],17)
        self.assertGreater(sale['sales'][0]['holdingDays'],365)
        self.assertEqual(self.repo.portfolio_state(),before)
        with self.assertRaisesRegex(ValueError,'not present'):
            self.service.scenario(HoldingsScenario(account='Main',date='2026-08-24',shocks={'MSFT':0.1}))

    def test_api_contract_and_read_only_analysis(self):
        from quant.dashboard import server,api_alpha
        from quant.dashboard.services import DashboardService
        service=DashboardService(UserDataRepository(self.path,read_only=True))
        before=self.repo.portfolio_state()
        with patch.object(api_alpha,'_service',service),patch('quant.quotes.get_market_history',side_effect=AssertionError('provider')):
            status,response=request(server.app,'/api/alpha/portfolio/analysis',query='account=Main&date=2026-08-24')
            self.assertEqual(status,200,response)
            self.assertEqual(response['data']['summary']['accountValue'],200)
            status,response=request(server.app,'/api/alpha/portfolio/transactions/import','POST',self.transaction_import().model_dump(mode='json'),client='203.0.113.5')
            self.assertEqual(status,403,response)
        self.assertEqual(self.repo.portfolio_state(),before)

    def test_split_updates_lots_preserves_basis_and_reconstructs_nav_prices(self):
        opening=self.service.snapshot(CurrentSnapshotInput(importKey='split-open',expectedRevision=1,account='Main',date='2026-08-24'))
        self.repo.apply_portfolio(PortfolioWrite(importKey='split',expectedRevision=1,effectiveDate='2026-08-26',source='issuer',reason='Two for one split',
            actions=[{'action':'split','account':'Main','symbol':'AAPL','ratio':'2'}]))
        lots=self.repo.portfolio_state()['lots']
        self.assertEqual(Decimal(lots[0]['quantity']),20)
        self.assertEqual(Decimal(lots[0]['totalCost']),80)
        rows=[{'Date':d,'Symbol':'AAPL','Close':p,'Adjusted Close':p} for d,p in zip(self.days,[5.,6.,5.5,7.,7.5])]
        MarketDataRepository(self.path).save(pl.DataFrame(rows))
        closing=ResearchWorkflow(self.path).import_snapshot(SnapshotInput(importKey='split-close',account='Main',date='2026-08-28',source='Broker',cash=100,
            positions=[{'symbol':'AAPL','quantity':20,'marketValue':150}]))
        record=self.performance.capture_nav(NavInput(importKey='split-nav',account='Main',openingSnapshotId=opening['id'],closingSnapshotId=closing['id'],complete=True))
        result=record['payload']['result']
        self.assertTrue(result['performanceAvailable'],result)
        self.assertEqual([r['value'] for r in result['history']],[200,220,210,240,250])
        self.assertEqual(self.repo.transactions()[0]['ratio'],'2')

    def test_buy_and_sell_import_can_select_new_lot_by_source_identity(self):
        body=TransactionImport(importKey='round-trip',expectedRevision=1,source='broker',reason='Completed round trip',transactions=[
            {'sourceId':'new-buy','date':'2026-08-25','entry':{'action':'buy','lot':{'symbol':'MSFT','account':'Main','quantity':'2','totalCost':'20','acquired':'2026-08-25','sourceLotId':'broker-lot'}}},
            {'sourceId':'new-sale','date':'2026-08-26','entry':{'action':'sell','account':'Main','sourceLotId':'broker-lot','quantity':'2','unitPrice':'12'}}])
        result=self.repo.apply_portfolio(body)
        self.assertEqual(len(result['after']['lots']),1)
        self.assertEqual(Decimal(self.repo.transactions()[-1]['realizedGain']),4)
        self.assertEqual(Decimal(result['after']['accounts'][0]['cash']),104)

    def test_latest_reported_nav_mismatch_gates_performance_endpoint(self):
        from quant.portfolio_contracts import PerformanceData
        record,_=self.capture()
        self.performance.reported_nav(ReportedNavInput(importKey='bad-nav',account='Main',date='2026-08-28',value='300',source='Broker'))
        data=self.performance.performance(record['id'])
        PerformanceData.model_validate(data)
        self.assertFalse(data['performanceAvailable'])
        self.assertIsNone(data['risk'])
        self.assertIsNone(data['timeWeightedReturn'])
        self.performance.reported_nav(ReportedNavInput(importKey='reconciled-nav',account='Main',date='2026-08-28',value='302',source='Corrected broker statement'))
        self.assertTrue(self.performance.performance(record['id'])['performanceAvailable'])

    def test_new_client_commands_and_openapi_shapes(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from quant.cli import main
        from quant.dashboard import api_alpha
        commands=[(['portfolio','analysis','--account','Main','--date','2026-08-28'],'portfolio/analysis'),
                  (['query','portfolio-analysis','--account','Main'],'portfolio/analysis'),
                  (['portfolio','transactions','--account','Main'],'portfolio/transactions'),
                  (['portfolio','performance','nav-id'],'portfolio/nav/nav-id/performance')]
        with patch('quant.query_cli.ApiClient') as client,redirect_stdout(StringIO()):
            client.return_value.request.return_value={'data':{},'meta':{},'warnings':[]}
            for args,endpoint in commands:
                main(args)
                self.assertEqual(client.return_value.request.call_args.args,('GET',endpoint))
        schema=api_alpha.app.openapi()
        ref=schema['paths']['/portfolio/analysis']['get']['responses']['200']['content']['application/json']['schema']['$ref']
        self.assertIn('PortfolioAnalysisData',ref)
        self.assertIn('PerformanceData',schema['paths']['/portfolio/nav/{identifier}/performance']['get']['responses']['200']['content']['application/json']['schema']['$ref'])

    def test_ticker_rollup_preserves_mixed_strategy_allocation(self):
        self.repo.apply_portfolio(PortfolioWrite(importKey='second-lot',expectedRevision=1,effectiveDate='2026-08-24',source='test',reason='Separate allocation',
            actions=[{'action':'add','lot':{'symbol':'AAPL','account':'Main','quantity':'5','totalCost':'40','strategy':'Value','sector':'Technology'}}]))
        data=self.service.analysis('Main',date(2026,8,24))
        self.assertEqual(len(data['positions']),1)
        self.assertEqual(data['positions'][0]['quantity'],15)
        self.assertEqual({row['name'] for row in data['allocations']['strategy']},{'Growth','Value'})
        self.assertEqual(self.service.show()['positions'][0]['strategy'],'Mixed')

    def test_empty_cash_only_account_values_without_price_lookup(self):
        self.repo.apply_portfolio(PortfolioWrite(importKey='cash-account',expectedRevision=1,effectiveDate='2026-08-24',source='test',reason='Cash only',
            actions=[{'action':'account','account':{'name':'Cash'}},{'action':'cash','account':'Cash','amount':'123.45'}]))
        data=self.service.analysis('Cash',date(2026,8,24))
        self.assertEqual(data['summary']['accountValue'],123.45)
        self.assertEqual(data['positions'],[])
        PortfolioAnalysisData.model_validate(data)

    def test_incomplete_benchmark_withholds_beta_but_keeps_account_risk(self):
        with database_connection(self.path) as db:
            db.execute("DELETE FROM daily_bars WHERE session_date='2026-08-26' AND security_id=(SELECT id FROM securities WHERE symbol='SPY')")
        record,_=self.capture()
        result=record['payload']['result']
        self.assertTrue(result['performanceAvailable'])
        self.assertIsNone(result['risk']['beta'])
        self.assertIsNotNone(result['risk']['annualizedVolatility'])

    def test_actual_performance_requires_cash_and_quantity_reconciliation(self):
        record,body=self.capture()
        wrong=ResearchWorkflow(self.path).import_snapshot(SnapshotInput(importKey='wrong-close',date='2026-08-28',account='Main',source='Unreconciled observation',cash=168,
            positions=[{'symbol':'AAPL','quantity':10}]))
        result=self.performance.capture_nav(body.model_copy(update={'importKey':'unreconciled','closingSnapshotId':wrong['id']}))['payload']['result']
        self.assertFalse(result['performanceAvailable'])
        self.assertIsNone(result['risk'])
        self.assertTrue(result['quantityDifferences'])
        self.assertNotEqual(result['cashDifference'],0)

    def test_full_api_import_nav_and_risk_workflow(self):
        from quant.dashboard import server,api_alpha
        from quant.dashboard.services import DashboardService
        record,body=self.capture()
        service=DashboardService(UserDataRepository(self.path,read_only=True))
        with patch.object(api_alpha,'_service',service):
            status,response=request(server.app,f"/api/alpha/portfolio/nav/{record['id']}/performance")
            self.assertEqual(status,200,response)
            self.assertTrue(response['data']['performanceAvailable'])
            status,response=request(server.app,'/api/alpha/portfolio/transactions',query='account=Main')
            self.assertEqual(status,200,response)
            self.assertEqual(len(response['data']),3)
            self.assertEqual(response['data'][-1]['realizedGain'],'17.000000000000')
            status,response=request(server.app,'/api/alpha/portfolio/nav','POST',body.model_dump(mode='json'))
            self.assertEqual(status,200,response)
            self.assertEqual(response['data']['id'],record['id'])

    def test_transaction_import_and_nav_reads_are_user_scoped(self):
        record,_=self.capture()
        with database_connection(self.path) as db: db.execute("INSERT INTO users(id) VALUES ('other-user')")
        other=UserDataRepository(self.path,'other-user')
        self.assertEqual(other.transactions(),[])
        performance=AccountPerformanceService(self.path,'other-user')
        self.assertEqual(performance.nav_records(),[])
        with self.assertRaisesRegex(ValueError,'Unknown nav'):
            performance.get_nav(record['id'])

    def test_quotes_retrieved_before_a_declared_split_block_actual_performance(self):
        opening=self.service.snapshot(CurrentSnapshotInput(importKey='old-quote-open',expectedRevision=1,account='Main',date='2026-08-24'))
        self.repo.apply_portfolio(PortfolioWrite(importKey='recorded-split',expectedRevision=1,effectiveDate='2026-08-26',source='issuer',reason='Two for one',
            actions=[{'action':'split','account':'Main','symbol':'AAPL','ratio':'2'}]))
        with database_connection(self.path) as db:
            db.execute("UPDATE daily_bars SET retrieved_at='2026-08-25T00:00:00+00:00' WHERE security_id=(SELECT id FROM securities WHERE symbol='AAPL')")
        closing=ResearchWorkflow(self.path).import_snapshot(SnapshotInput(importKey='old-quote-close',account='Main',date='2026-08-28',source='broker',cash=100,positions=[{'symbol':'AAPL','quantity':20}]))
        record=self.performance.capture_nav(NavInput(importKey='old-quotes-nav',account='Main',openingSnapshotId=opening['id'],closingSnapshotId=closing['id'],complete=True))
        self.assertFalse(record['payload']['result']['performanceAvailable'])
        self.assertIsNone(record['payload']['result']['risk'])
        self.assertTrue(any('retrieved before' in warning for warning in record['payload']['result']['warnings']))
