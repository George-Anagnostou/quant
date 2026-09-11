from __future__ import annotations

import copy
import json
import sqlite3
from decimal import Decimal, localcontext
import uuid
from datetime import date
from pathlib import Path

import polars as pl

from quant.database import (
    DEFAULT_DATABASE_PATH,
    LOCAL_ADMIN_USER_ID,
    database_connection,
    initialize_database,
    is_database_initialized,
)

DEFAULT_USER_DATA_PATH = DEFAULT_DATABASE_PATH
MAX_SYMBOL_LENGTH = 32


class UserDataRepository:
    def __init__(
        self,
        path: Path = DEFAULT_USER_DATA_PATH,
        user_id: str = LOCAL_ADMIN_USER_ID,
        *,
        read_only: bool = False,
    ) -> None:
        self.path = path
        self.user_id = user_id
        self.read_only = read_only

    def initialize(self) -> None:
        if self.read_only:
            if not is_database_initialized(self.path):
                raise RuntimeError("User database is not initialized")
        else:
            initialize_database(self.path)
        with self._connect() as connection:
            user = connection.execute(
                "SELECT 1 FROM users WHERE id = ?", (self.user_id,)
            ).fetchone()
            if user is None:
                raise ValueError(f"Unknown user: {self.user_id}")

    def list_positions(self) -> list[dict]:
        self.initialize()
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT id, symbol, quantity, average_cost, account, asset_class, sector, acquired FROM positions WHERE user_id=? ORDER BY rowid",
                (self.user_id,))]

    def positions_frame(self) -> pl.DataFrame:
        self.initialize()
        lots = {lot['id']: lot for lot in self.portfolio_state()['lots']}
        positions = [{'id': lot['id'], 'symbol': lot['symbol'], 'quantity': float(lot['quantity']),
                      'average_cost': float(Decimal(lot['totalCost']) / Decimal(lot['quantity'])),
                      'account': lot['account'], 'asset_class': lot.get('assetClass'),
                      'sector': lot.get('sector'), 'acquired': lot.get('acquired')} for lot in lots.values()]
        if any(lot['currency'] != 'USD' for lot in lots.values()):
            raise ValueError('Portfolio analysis supports USD lots only; foreign currency lots remain available through portfolio show')
        return pl.DataFrame(
            {
                "ID": [position["id"] for position in positions],
                "Symbol": [position["symbol"] for position in positions],
                "Quantity": [position["quantity"] for position in positions],
                "Average Cost": [position["average_cost"] for position in positions],
                "Account": [position["account"] for position in positions],
                "Asset Class": [position["asset_class"] for position in positions],
                "Sector": [position["sector"] for position in positions],
                "Acquired": [position["acquired"] for position in positions],
                "Total Cost": [float(lots[position['id']]['totalCost']) for position in positions],
            },
            schema={
                "ID": pl.String,
                "Symbol": pl.String,
                "Quantity": pl.Float64,
                "Average Cost": pl.Float64,
                "Account": pl.String,
                "Asset Class": pl.String,
                "Sector": pl.String,
                "Acquired": pl.String,
                "Total Cost": pl.Float64,
            },
        )

    def add_position(self, symbol, quantity, average_cost, account=None,
                     asset_class=None, sector=None, acquired=None) -> dict:
        from quant.portfolio_contracts import PortfolioWrite
        # Compatibility boundary: legacy cost is per share, canonical cost is total.
        quantity, average_cost = Decimal(str(quantity)), Decimal(str(average_cost))
        if not quantity.is_finite() or not average_cost.is_finite() or quantity <= 0 or average_cost < 0:
            raise ValueError("Quantity must be positive and cost non-negative")
        account = _clean_optional(account) or "Unspecified"
        state = self.portfolio_state()
        actions = []
        if account not in {a['name'] for a in state['accounts']}:
            actions.append({'action': 'account', 'account': {'name': account}})
        actions.append({'action': 'add', 'lot': {'symbol': symbol, 'quantity': str(quantity),
            'totalCost': str(quantity * average_cost), 'account': account,
            'assetClass': asset_class, 'sector': sector, 'acquired': acquired}})
        result = self.apply_portfolio(PortfolioWrite(importKey=str(uuid.uuid4()),
            expectedRevision=state['revision'], effectiveDate=date.today(), source='legacy-holdings',
            reason='Record opening holding', actions=actions))
        identifier = result['changes'][-1]['lotId']
        return next(p for p in self.list_positions() if p['id'] == identifier)

    def remove_position(self, position_id: str) -> bool:
        from quant.portfolio_contracts import PortfolioWrite
        if self.read_only:
            raise sqlite3.OperationalError('attempt to write a readonly database')
        state = self.portfolio_state()
        if position_id not in {lot['id'] for lot in state['lots']}:
            return False
        self.apply_portfolio(PortfolioWrite(importKey=str(uuid.uuid4()), expectedRevision=state['revision'],
            effectiveDate=date.today(), source='legacy-holdings', reason='Remove erroneous holding',
            actions=[{'action': 'remove', 'lotId': position_id}]))
        return True

    def portfolio_state(self) -> dict:
        if not is_database_initialized(self.path):
            return {'revision': 0, 'accounts': [], 'lots': []}
        with self._connect() as db:
            db.execute('BEGIN')
            return self._portfolio_state(db)

    def _portfolio_state(self, db) -> dict:
        revision = db.execute('SELECT revision FROM portfolio_state WHERE user_id=?', (self.user_id,)).fetchone()
        accounts = [json.loads(r[0]) for r in db.execute(
            'SELECT data FROM portfolio_accounts WHERE user_id=? ORDER BY name', (self.user_id,))]
        lots = []
        for row in db.execute('SELECT * FROM positions WHERE user_id=? ORDER BY rowid', (self.user_id,)):
            details = json.loads(row['details'])
            if not details:
                details = {'symbol': row['symbol'], 'account': row['account'] or 'Unspecified',
                    'quantity': str(row['quantity']), 'totalCost': str(Decimal(str(row['quantity'])) * Decimal(str(row['average_cost']))),
                    'currency': 'USD', 'acquired': row['acquired'], 'assetClass': row['asset_class'],
                    'sector': row['sector'], 'source': 'legacy', 'asOf': None}
            lots.append({'id': row['id'], **details})
            if details['account'] not in {a['name'] for a in accounts}:
                accounts.append({'name': details['account'], 'currency': details['currency'], 'cash': None, 'asOf': None})
        return {'revision': revision[0] if revision else 0, 'accounts': accounts, 'lots': lots}

    def portfolio_history(self, limit=100, offset=0) -> list[dict]:
        if not 1 <= limit <= 1000 or offset < 0:
            raise ValueError('Invalid page')
        if not is_database_initialized(self.path):
            return []
        with self._connect() as db:
            rows = list(db.execute(
                'SELECT data FROM portfolio_changes WHERE user_id=? ORDER BY revision LIMIT ? OFFSET ?',
                (self.user_id, limit, offset)))
            if sum(len(row[0].encode()) for row in rows) > 8 * 1024 * 1024:
                raise ValueError('History page exceeds 8 MiB; request a smaller limit')
            return [json.loads(row[0]) for row in rows]

    def preview_portfolio(self, body) -> dict:
        from quant.record_store import RecordRepository, canonical
        if is_database_initialized(self.path):
            prior = RecordRepository(self.path, self.user_id).by_key('portfolio_write', body.importKey)
            if prior:
                if canonical(prior['payload']) != canonical(body.model_dump(mode='json')):
                    raise ValueError('Import identity already exists with different content')
                with self._connect() as db:
                    result = json.loads(db.execute('SELECT data FROM portfolio_changes WHERE user_id=? AND import_key=?',
                        (self.user_id, body.importKey)).fetchone()[0])
                return {**result, 'alreadyApplied': True}
        self._check_transaction_identities(body)
        return self._simulate_portfolio(self.portfolio_state(), body)

    def apply_portfolio(self, body) -> dict:
        from quant.record_store import RecordRepository, canonical
        if self.read_only:
            raise sqlite3.OperationalError('attempt to write a readonly database')
        self.initialize()
        def apply(db):
            self._check_transaction_identities(body, db)
            result = self._simulate_portfolio(self._portfolio_state(db), body)
            state = result['after']
            db.execute('DELETE FROM positions WHERE user_id=?', (self.user_id,))
            for lot in state['lots']:
                details = {k: v for k, v in lot.items() if k != 'id'}
                db.execute('INSERT INTO positions(id,user_id,symbol,quantity,average_cost,account,asset_class,sector,acquired,details) VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (lot['id'], self.user_id, lot['symbol'], float(lot['quantity']),
                     float(Decimal(lot['totalCost']) / Decimal(lot['quantity'])), lot['account'],
                     lot.get('assetClass'), lot.get('sector'), lot.get('acquired'), canonical(details)))
            for account in state['accounts']:
                db.execute('INSERT INTO portfolio_accounts VALUES (?,?,?) ON CONFLICT(user_id,name) DO UPDATE SET data=excluded.data',
                           (self.user_id, account['name'], canonical(account)))
            db.execute('INSERT INTO portfolio_state VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET revision=excluded.revision',
                       (self.user_id, state['revision']))
            db.execute('INSERT INTO portfolio_changes VALUES (?,?,?,?)',
                       (self.user_id, body.importKey, state['revision'], canonical(result)))
        RecordRepository(self.path, self.user_id).put('portfolio_write', body.importKey, body.model_dump(mode='json'), after_insert=apply)
        with self._connect() as db:
            return json.loads(db.execute('SELECT data FROM portfolio_changes WHERE user_id=? AND import_key=?',
                (self.user_id, body.importKey)).fetchone()[0])

    def _check_transaction_identities(self, body, db=None):
        if not hasattr(body, 'transactions'):
            return
        if db is None:
            if not is_database_initialized(self.path):
                return
            with self._connect() as connection:
                return self._check_transaction_identities(body, connection)
        known = {row[0] for row in db.execute(
            "SELECT json_extract(j.value,'$.sourceId') FROM portfolio_changes c, json_each(c.data,'$.changes') j WHERE c.user_id=? AND json_extract(c.data,'$.source')=?",
            (self.user_id, body.source)) if row[0]}
        if known.intersection(t.sourceId for t in body.transactions):
            raise ValueError('Transaction sourceId already imported from this source; reconcile the overlapping import')

    def transactions(self, account=None, start=None, end=None, limit=100, offset=0):
        if not 1 <= limit <= 10000 or offset < 0:
            raise ValueError('Invalid transaction page')
        if not is_database_initialized(self.path):
            return []
        with self._connect() as db:
            rows = db.execute(
                "SELECT c.import_key, c.revision, json_extract(c.data,'$.effectiveDate') AS effective, json_extract(c.data,'$.source') AS source, j.key AS entry_index, j.value FROM portfolio_changes c, json_each(c.data,'$.changes') j WHERE c.user_id=? ORDER BY c.revision, CAST(j.key AS INTEGER)",
                (self.user_id,))
            result = []
            for row in rows:
                change = json.loads(row['value'])
                item, kind = change.get('input', {}), change['action']
                if kind not in {'buy','sell','deposit','withdrawal','dividend','fee','split'}:
                    continue
                lot = change.get('before') if kind == 'sell' else item.get('lot')
                name = lot['account'] if lot else item['account']
                day = change.get('date', row['effective'])
                if account and account != name or start and day < start.isoformat() or end and day > end.isoformat():
                    continue
                entry = {'id': f"{row['import_key']}:{row['entry_index']}", 'revision': row['revision'],
                    'sourceId': change.get('sourceId', f"{row['import_key']}:{row['entry_index']}"),
                    'source': row['source'], 'date': day, 'account': name, 'kind': kind,
                    'symbol': lot['symbol'] if lot else item.get('symbol'), 'lotId': change.get('lotId'),
                    'quantity': item.get('quantity') if kind=='sell' else lot['quantity'] if lot else None,
                    'amount': change['proceeds'] if kind=='sell' else lot['totalCost'] if lot else item.get('amount','0'),
                    'fees': item.get('fees','0'), 'removedCost': change.get('removedCost'),
                    'realizedGain': change.get('realizedGain'), 'ratio':item.get('ratio')}
                result.append(entry)
            result.sort(key=lambda item: (item['date'], item['revision']))
            return result[offset:offset+limit]

    def corrections_between(self, account, start, end):
        if not is_database_initialized(self.path):
            return []
        with self._connect() as db:
            rows = db.execute("SELECT data FROM portfolio_changes WHERE user_id=?", (self.user_id,))
            issues = []
            for row in rows:
                batch = json.loads(row[0])
                for change in batch['changes']:
                    day = change.get('date', batch['effectiveDate'])
                    if not start.isoformat() < day <= end.isoformat() or change['action'] not in {'add','edit','remove','cash'}:
                        continue
                    state = change.get('after') or change.get('before') or {}
                    if state.get('account', state.get('name')) == account:
                        issues.append({'date':day,'action':change['action'],'importKey':batch['importKey']})
            return issues

    def _simulate_portfolio(self, before, body):
        if before['revision'] != body.expectedRevision:
            raise ValueError(f"Portfolio revision conflict: expected {body.expectedRevision}, current {before['revision']}")
        # Keep decimal arithmetic isolated from process-global Decimal settings.
        with localcontext() as context:
            context.prec = 40
            if hasattr(body, 'transactions'):
                from quant.portfolio_contracts import PortfolioWrite
                state, changes = before, []
                for index, transaction in enumerate(body.transactions):
                    part = PortfolioWrite(importKey=str(uuid.uuid5(uuid.NAMESPACE_URL, f'{body.importKey}:{index}')), expectedRevision=state['revision'],
                        effectiveDate=transaction.date, source=body.source, reason=body.reason,
                        actions=[transaction.entry])
                    result = self._simulate_decimal(state, part)
                    state = result['after']
                    changes.extend({**change, 'sourceId': transaction.sourceId, 'date': transaction.date.isoformat()} for change in result['changes'])
                state['revision'] = before['revision'] + 1
                result = {**result, 'importKey': body.importKey, 'before': before, 'after': state, 'changes': changes}
                from quant.record_store import canonical
                if len(canonical(result).encode()) > 8 * 1024 * 1024:
                    raise ValueError('Transaction import exceeds 8 MiB audit record limit')
                return result
            return self._simulate_decimal(before, body)

    def _simulate_decimal(self, before, body):
        if before['revision'] != body.expectedRevision:
            raise ValueError(f"Portfolio revision conflict: expected {body.expectedRevision}, current {before['revision']}")
        from quant.record_store import canonical
        state = copy.deepcopy(before)
        accounts = {a['name']: a for a in state['accounts']}
        lots = {lot['id']: lot for lot in state['lots']}
        changes = []
        effective = body.effectiveDate.isoformat()

        def account_for(name):
            if name not in accounts:
                raise ValueError(f'Unknown account: {name}; create it explicitly')
            account = accounts[name]
            if account.get('asOf') and effective < account['asOf']:
                raise ValueError('Backdated changes require chronological reconciliation; effectiveDate precedes account state')
            return account

        def change_cash(account, amount):
            if account.get('cash') is None:
                raise ValueError('Account cash is unknown; record a cash opening balance first')
            value = Decimal(account['cash']) + amount
            if value < 0 or value > 10**15:
                raise ValueError('Cash would be negative or exceed the supported bound')
            account['cash'] = str(value)

        for index, action in enumerate(body.actions):
            item = action.model_dump(mode='json')
            kind = action.action
            change = {'action': kind, 'input': item}
            if kind == 'account':
                data = item['account']
                if data['name'] in accounts:
                    raise ValueError('Account already exists; reuse its name')
                accounts[data['name']] = {**data, 'cash': None, 'asOf': effective}
                changes.append({**change, 'after': accounts[data['name']].copy()})
                continue
            if kind in {'add', 'buy', 'edit'}:
                lot = item['lot']
                account = account_for(lot['account'])
                if account['currency'] != lot['currency']:
                    raise ValueError('Lot currency must match its account; FX conversion is not implemented')
                if kind == 'edit':
                    identifier = action.lotId
                    if identifier not in lots:
                        raise ValueError('Unknown lot for this user')
                    old = lots[identifier]
                    if (lot['account'],lot['currency']) != (old['account'],old['currency']):
                        raise ValueError('Edits cannot transfer a lot between accounts or currencies')
                    change['before'] = copy.deepcopy(old)
                else:
                    identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, f'quant:{self.user_id}:{body.importKey}:{index}'))
                if lot.get('sourceLotId') and any(l['account']==lot['account'] and l.get('sourceLotId')==lot['sourceLotId'] and i!=identifier for i,l in lots.items()):
                    raise ValueError('Duplicate sourceLotId in account')
                if kind == 'buy':
                    # totalCost includes fees, matching the acquired lot basis.
                    if action.fees > Decimal(lot['totalCost']):
                        raise ValueError('Fees cannot exceed purchase totalCost')
                    change_cash(account, -Decimal(lot['totalCost']))
                lots[identifier] = {'id': identifier, **lot, 'asOf': effective, 'source': body.source}
                change.update(lotId=identifier, after=copy.deepcopy(lots[identifier]))
            elif kind in {'sell', 'remove'}:
                identifier = action.lotId
                if kind == 'sell' and identifier is None:
                    matches = [key for key,lot in lots.items() if lot['account']==action.account and lot.get('sourceLotId')==action.sourceLotId]
                    if len(matches)!=1:
                        raise ValueError('Source lot selector must match exactly one active lot in the account')
                    identifier = matches[0]
                if identifier not in lots:
                    raise ValueError('Unknown lot for this user')
                lot = lots[identifier]
                account = account_for(lot['account'])
                change.update(lotId=identifier, before=copy.deepcopy(lot))
                if kind == 'remove':
                    del lots[identifier]
                    change['after'] = None
                else:
                    quantity, cost = Decimal(lot['quantity']), Decimal(lot['totalCost'])
                    if action.quantity > quantity:
                        raise ValueError('Sale quantity exceeds selected lot')
                    proceeds = action.quantity * action.unitPrice - action.fees
                    if proceeds < 0:
                        raise ValueError('Sale fees exceed proceeds')
                    removed_cost = cost if action.quantity == quantity else (cost * action.quantity / quantity).quantize(Decimal('0.000000000001'))
                    change_cash(account, proceeds)
                    change.update(proceeds=str(proceeds), removedCost=str(removed_cost), realizedGain=str(proceeds-removed_cost))
                    if action.quantity == quantity:
                        del lots[identifier]
                        change['after'] = None
                    else:
                        lot.update(quantity=str(quantity-action.quantity), totalCost=str(cost-removed_cost), asOf=effective)
                        # A prior reported valuation describes the pre-sale lot.
                        lot.update(reportedPrice=None, reportedValue=None, priceDate=None)
                        change['after'] = copy.deepcopy(lot)
            elif kind == 'split':
                account = account_for(action.account)
                affected = [lot for lot in lots.values() if lot['account']==action.account and lot['symbol']==action.symbol]
                if not affected:
                    raise ValueError('Split requires an owned security in the account')
                change['before'] = copy.deepcopy(affected)
                from quant.portfolio_contracts import Quantity
                from pydantic import TypeAdapter
                for lot in affected:
                    quantity = TypeAdapter(Quantity).validate_python(Decimal(lot['quantity'])*action.ratio)
                    lot.update(quantity=str(quantity),asOf=effective,reportedPrice=None,reportedValue=None,priceDate=None)
                change['after'] = copy.deepcopy(affected)
            else:
                account = account_for(action.account)
                change['before'] = copy.deepcopy(account)
                if kind == 'cash':
                    account['cash'] = str(action.amount)
                else:
                    sign = -1 if kind in {'withdrawal','fee'} else 1
                    change_cash(account, sign * action.amount)
                change['after'] = copy.deepcopy(account)
            account['asOf'] = effective
            if kind in {'cash', 'deposit', 'withdrawal', 'dividend', 'fee'}:
                change['after'] = copy.deepcopy(account)
            changes.append(change)
        state.update(revision=before['revision']+1, accounts=list(accounts.values()), lots=list(lots.values()))
        result = {'importKey': body.importKey, 'effectiveDate': effective, 'source': body.source,
                'reason': body.reason, 'changes': changes, 'before': before, 'after': state,
                'warnings': ['Current-share backcasts are not actual account performance. Internal events do not establish complete prior transaction history.']}
        if len(lots) > 2000 or len(accounts) > 100 or len(canonical(result).encode()) > 8 * 1024 * 1024:
            raise ValueError('Portfolio exceeds 2000 lots, 100 accounts, or 8 MiB audit record limit')
        return result

    def _connect(self):
        return database_connection(self.path, read_only=self.read_only)


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None
