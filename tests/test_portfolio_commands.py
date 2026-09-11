import copy
import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from quant.database import initialize_database, database_connection, _create_schema, _migrate_v2
from quant.portfolio_contracts import PortfolioWrite, CurrentSnapshotInput
from quant.portfolio_service import PortfolioService
from quant.user_data import UserDataRepository
from quant.cli import main
from tests.test_research_engine import request


def command(actions, revision=0, key='opening', day='2026-09-01'):
    return PortfolioWrite(importKey=key, expectedRevision=revision, effectiveDate=day,
                          source='test-fixture', reason='Explicit portfolio instruction', actions=actions)


def lot(**kwargs):
    return dict(symbol='AAPL', account='Brokerage', quantity='10', totalCost='100.02', acquired='2026-08-01', **kwargs)


def opening():
    return command([{'action':'account','account':{'name':'Brokerage'}},
                    {'action':'cash','account':'Brokerage','amount':'1000'},
                    {'action':'add','lot':lot()}])


class PortfolioCommandsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'quant.db'
        self.repo = UserDataRepository(self.path)

    def seed(self):
        return self.repo.apply_portfolio(opening())['after']['lots'][0]['id']

    def test_preview_does_not_create_database_and_matches_apply(self):
        preview = self.repo.preview_portfolio(opening())
        self.assertFalse(self.path.exists())
        result = self.repo.apply_portfolio(opening())
        self.assertEqual(preview, result)
        self.assertEqual(result['after']['accounts'][0]['cash'], '1000')
        self.assertEqual(len(self.repo.list_positions()), 1)

    def test_retries_are_idempotent_even_after_later_change(self):
        self.seed()
        body = command([{'action':'deposit','account':'Brokerage','amount':'1'}],1,'deposit')
        first = self.repo.apply_portfolio(body)
        self.assertEqual(self.repo.apply_portfolio(body), first)
        self.assertEqual(self.repo.preview_portfolio(opening())['alreadyApplied'], True)
        self.assertEqual(self.repo.portfolio_state()['revision'], 2)
        changed = body.model_copy(update={'reason':'different'})
        with self.assertRaisesRegex(ValueError, 'different content'):
            self.repo.apply_portfolio(changed)

    def test_partial_then_full_sale_keeps_exact_basis_and_cash(self):
        identifier = self.seed()
        partial = command([{'action':'sell','lotId':identifier,'quantity':'3','unitPrice':'20','fees':'1'}],1,'sale')
        result = self.repo.apply_portfolio(partial)
        remaining = result['after']['lots'][0]
        self.assertEqual(Decimal(remaining['quantity']), Decimal('7'))
        self.assertEqual(Decimal(remaining['totalCost']), Decimal('70.014'))
        self.assertEqual(Decimal(result['changes'][0]['realizedGain']), Decimal('28.994'))
        self.assertEqual(Decimal(result['after']['accounts'][0]['cash']), Decimal('1059'))
        result = self.repo.apply_portfolio(command([{'action':'sell','lotId':identifier,'quantity':'7','unitPrice':'20'}],2,'close'))
        self.assertEqual(result['after']['lots'], [])
        self.assertEqual(Decimal(result['after']['accounts'][0]['cash']), Decimal('1199'))
        self.assertEqual(len(self.repo.portfolio_history()),3)

    def test_purchase_includes_fees_once(self):
        self.seed()
        new_lot = {**lot(), 'acquired':'2026-09-02','totalCost':'102'}
        result = self.repo.apply_portfolio(command([{'action':'buy','lot':new_lot,'fees':'2'}],1,'buy','2026-09-02'))
        self.assertEqual(Decimal(result['after']['accounts'][0]['cash']), Decimal('898'))
        self.assertEqual(Decimal(result['after']['lots'][-1]['totalCost']), Decimal('102'))

    def test_edits_and_removal_are_corrections_without_cash_changes(self):
        identifier = self.seed()
        edited = {**lot(), 'quantity':'11','totalCost':'123.4567'}
        result = self.repo.apply_portfolio(command([{'action':'edit','lotId':identifier,'lot':edited}],1,'edit'))
        self.assertEqual(result['after']['lots'][0]['id'],identifier)
        self.assertEqual(result['changes'][0]['before']['quantity'],'10')
        self.repo.apply_portfolio(command([{'action':'remove','lotId':identifier}],2,'remove'))
        self.assertEqual(self.repo.portfolio_state()['accounts'][0]['cash'],'1000')

    def test_batch_failure_rolls_back_all_changes_and_audit(self):
        identifier = self.seed()
        body=command([{'action':'deposit','account':'Brokerage','amount':'2'},
                      {'action':'sell','lotId':identifier,'quantity':'11','unitPrice':'20'}],1,'fail')
        with self.assertRaisesRegex(ValueError,'exceeds'):
            self.repo.apply_portfolio(body)
        self.assertEqual(self.repo.portfolio_state()['accounts'][0]['cash'],'1000')
        self.assertEqual(len(self.repo.portfolio_history()),1)
        with database_connection(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM records WHERE kind='portfolio_write'").fetchone()[0],1)

    def test_concurrent_writers_require_fresh_revision(self):
        self.seed()
        def write(index):
            try:
                UserDataRepository(self.path).apply_portfolio(command([{'action':'deposit','account':'Brokerage','amount':'1'}],1,f'key-{index}'))
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(write,range(2))),1)
        self.assertEqual(self.repo.portfolio_state()['revision'],2)

    def test_user_isolation_for_reads_writes_and_idempotency(self):
        identifier = self.seed()
        with database_connection(self.path) as db:
            db.execute("INSERT INTO users(id) VALUES ('other')")
        other = UserDataRepository(self.path,'other')
        with self.assertRaisesRegex(ValueError,'Unknown lot'):
            other.apply_portfolio(command([{'action':'remove','lotId':identifier}]))
        other.apply_portfolio(opening())
        self.assertNotEqual(other.portfolio_state()['lots'][0]['id'],identifier)
        self.assertEqual(self.repo.portfolio_state()['revision'],1)

    def test_unknown_cash_and_negative_cash_are_not_assumed(self):
        self.repo.apply_portfolio(command([{'action':'account','account':{'name':'Brokerage'}}]))
        with self.assertRaisesRegex(ValueError,'unknown'):
            self.repo.apply_portfolio(command([{'action':'withdrawal','account':'Brokerage','amount':'1'}],1,'withdraw'))
        self.repo.apply_portfolio(command([{'action':'cash','account':'Brokerage','amount':'1'}],1,'cash'))
        with self.assertRaisesRegex(ValueError,'negative'):
            self.repo.apply_portfolio(command([{'action':'withdrawal','account':'Brokerage','amount':'2'}],2,'withdraw'))

    def test_backdates_conflicts_and_cross_account_edits_rejected(self):
        identifier = self.seed()
        cases = [command([{'action':'remove','lotId':identifier}],0,'stale'),
                 command([{'action':'remove','lotId':identifier}],1,'backdate','2026-08-31')]
        for body in cases:
            with self.assertRaises(ValueError): self.repo.apply_portfolio(body)

    def test_duplicate_source_lot_identity_rejected(self):
        self.seed()
        actions=[{'action':'add','lot':lot(sourceLotId='external-1')}, {'action':'add','lot':lot(sourceLotId='external-1')}]
        with self.assertRaisesRegex(ValueError,'Duplicate sourceLotId'):
            self.repo.apply_portfolio(command(actions,1,'duplicate'))

    def test_foreign_currency_is_stored_but_not_mispriced_as_usd(self):
        foreign={**lot(),'account':'Euro','currency':'EUR'}
        self.repo.apply_portfolio(command([{'action':'account','account':{'name':'Euro','currency':'EUR'}}, {'action':'add','lot':foreign}]))
        self.assertEqual(PortfolioService(self.path,'local-admin').show()['positions'][0]['currency'],'EUR')
        with self.assertRaisesRegex(ValueError,'USD'):
            self.repo.positions_frame()

    def test_aggregation_and_current_snapshot_preserve_account_and_lots(self):
        self.seed()
        self.repo.apply_portfolio(command([{'action':'add','lot':lot()}],1,'another'))
        service=PortfolioService(self.path,'local-admin')
        position=service.show()['positions'][0]
        self.assertEqual(position['lotCount'],2)
        self.assertEqual(Decimal(position['quantity']),20)
        self.assertEqual(Decimal(position['totalCost']),Decimal('200.04'))
        snapshot=service.snapshot(CurrentSnapshotInput(importKey='capture',expectedRevision=2,account='Brokerage',date='2026-09-01'))
        self.assertEqual(len(snapshot['payload']['positions']),1)
        self.assertEqual(snapshot['payload']['cash'],1000)
        self.assertEqual(len(self.repo.list_positions()),2)

    def test_migration_preserves_v2_positions_and_backup(self):
        with closing(sqlite3.connect(self.path)) as db:
            _create_schema(db); _migrate_v2(db)
            db.execute("INSERT INTO positions VALUES ('old','local-admin','AAPL',2,12.34,'IRA','Equity',NULL,'2025-01-01')")
            db.execute('PRAGMA user_version=2')
            db.commit()
        initialize_database(self.path)
        state=self.repo.portfolio_state()
        self.assertEqual(state['lots'][0]['id'],'old')
        self.assertEqual(Decimal(state['lots'][0]['totalCost']),Decimal('24.68'))
        self.assertEqual(state['accounts'][0]['cash'],None)
        self.assertEqual(len(list(self.path.parent.glob('*.v2-*.backup'))),1)

    def test_api_contract_preview_writes_and_retired_watchlist(self):
        from quant.dashboard import server, api_alpha
        from quant.dashboard.services import DashboardService
        service=DashboardService(UserDataRepository(self.path,read_only=True))
        with patch.object(api_alpha,'_service',service):
            status,body=request(server.app,'/api/alpha/portfolio/preview','POST',opening().model_dump(mode='json'))
            self.assertEqual(status,200,body)
            self.assertFalse(self.path.exists())
            status,body=request(server.app,'/api/alpha/portfolio/apply','POST',opening().model_dump(mode='json'),client='203.0.113.1')
            self.assertEqual(status,403)
            status,body=request(server.app,'/api/alpha/portfolio/apply','POST',opening().model_dump(mode='json'))
            self.assertEqual(status,200,body)
            status,body=request(server.app,'/api/alpha/portfolio/holdings')
            self.assertEqual(status,200,body)
            self.assertEqual(body['data']['revision'],1)
        for path in ['/api/watchlist','/api/alpha/watchlist']:
            self.assertEqual(request(server.app,path)[0],404)

    def test_agent_cli_only_forwards_json_to_api(self):
        file=self.path.parent/'request.json'; file.write_text(json.dumps(opening().model_dump(mode='json')))
        with patch('quant.query_cli.ApiClient') as client, redirect_stdout(StringIO()):
            client.return_value.request.return_value={'data':{},'meta':{},'warnings':[]}
            main(['portfolio','apply','--json-file',str(file)])
            args=client.return_value.request.call_args
            self.assertEqual(args.args,('POST','portfolio/apply'))
            self.assertEqual(args.kwargs['body']['actions'][2]['lot']['totalCost'],'100.02')
        self.assertFalse(self.path.exists())
