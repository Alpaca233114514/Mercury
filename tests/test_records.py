import csv
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout, redirect_stderr
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from mercury import records
from mercury.__main__ import main
from mercury.audit import audit
from mercury.common import canonical, digest
from mercury.engine import PaperTrader
from mercury.evaluation import _account, compare_agents
from mercury.examples import synthetic_episode, offline_template
from mercury.models import OfflineAgent, agent_from_config
from mercury.observation import build_observation
from mercury.strategy import compile_intents
from mercury.workflow import run_cycle, public_account


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.spec = synthetic_episode()
        self.trader = _account(self.root/'account.db', self.spec)
        self.agent = OfflineAgent(offline_template(), name='model-a')

    def cycle(self, name='cycle', agent=None):
        observation = build_observation(self.trader, self.spec['universe'])
        return run_cycle(self.trader, agent or self.agent, observation, output=self.root/name)

    def next_quote(self, at='2026-09-24T10:01:00+08:00'):
        self.trader.advance(at)

    def cli(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            result = main(['--db', str(self.trader.path), 'records', *args])
        self.assertEqual(result, 0, error.getvalue())
        return json.loads(output.getvalue())

    def test_actual_signal_trace_preserves_intent_and_decimal_calculations(self):
        obs = build_observation(self.trader, self.spec['universe'])
        account = public_account(self.trader, obs)
        strategy = self.agent.decide(obs, account, {})['strategy']
        trace = {}
        before = compile_intents(strategy, obs, account)
        after = compile_intents(strategy, obs, account, trace=trace)
        self.assertEqual(before, after)
        signal = trace['signals'][0]['values'][0]
        self.assertEqual(Decimal(signal['value']), Decimal('10.01')/Decimal('9.99')-1)
        target = trace['targets'][0]
        self.assertEqual(target['rounded_target'], 900)
        self.assertEqual(target['qty'], 900)
        self.assertEqual(target['unrounded_target'], str(Decimal('100000')*Decimal('.1')/Decimal('10.01')))
        self.assertEqual(trace['code']['sha256'], records.code_identity()['sha256'])

    def test_later_independent_match_keeps_original_cycle_and_full_cost_evidence(self):
        first = self.cycle()
        self.next_quote()
        fills = self.trader.match()
        self.assertEqual(len(fills), 1)
        fill = records.show(self.trader, fill_id=fills[0]['id'])
        self.assertEqual(fill['cycle']['cycle_id'], first['cycle_id'])
        self.assertEqual(fill['cycle']['model'], 'model-a')
        self.assertEqual(fill['cycle']['status'], 'filled')
        calc = fill['fill']['calculation']['payload']
        self.assertEqual(calc['remaining_after'], 0)
        self.assertEqual(Decimal(calc['before']['cash']) - Decimal(calc['after']['cash']),
                         Decimal(fills[0]['notional'])+Decimal(fills[0]['fee']))
        self.assertEqual(Decimal(calc['after']['cost']), Decimal(fills[0]['notional'])+Decimal(fills[0]['fee']))
        self.assertEqual(calc['fee']['incremental_fee'], fills[0]['fee'])
        self.assertTrue(any(e['kind'] == 'ledger_event' and e['payload']['event_kind'] == 'risk_decision'
                            for e in fill['cycle']['execution_entries']))
        self.assertTrue(audit(self.trader)['ok'])

    def test_full_buy_t1_sell_reconstructs_cash_cost_pnl_and_separate_models(self):
        result = compare_agents(self.spec, {'a':self.agent, 'b':OfflineAgent(offline_template('.2'), name='model-b')}, output=self.root/'compare')
        for name in ('a','b'):
            t = PaperTrader(self.root/'compare'/('agent-'+digest(name)[:16]+'.db'))
            data = records.read(t)
            self.assertEqual(len(data['fills']), 2)
            cash, cost, qty = Decimal('100000'), Decimal(0), 0
            for fill in data['fills']:
                trace = fill['calculation']['payload']
                self.assertEqual((Decimal(trace['before']['cash']), Decimal(trace['before']['cost']), trace['before']['qty']), (cash,cost,qty))
                amount, fee = Decimal(fill['notional']), Decimal(fill['fee'])
                if fill['side'] == 'buy':
                    cash -= amount+fee
                    cost += amount+fee
                    qty += fill['qty']
                else:
                    self.assertEqual(Decimal(fill['realized_pnl']), amount-fee-cost)
                    cash += amount-fee
                    cost, qty = Decimal(0), 0
                self.assertEqual((Decimal(trace['after']['cash']), Decimal(trace['after']['cost']), trace['after']['qty']), (cash,cost,qty))
            self.assertEqual(cash, Decimal(result['models'][name]['report']['portfolio']['cash']))
            self.assertTrue(audit(t)['ok'])
            self.assertEqual(records.listing(t, model='model-'+name)['total'], 2)
            self.assertEqual(records.listing(t, model='model-'+('b' if name=='a' else 'a'))['total'], 0)

    def test_record_failure_rolls_back_economic_transaction_and_recovery_once(self):
        self.cycle()
        self.next_quote()
        before = records.read(self.trader)
        original = records.append
        def fail(db, key, kind, payload, **kwargs):
            if kind == 'fill_calculation':
                raise records.RecordError('injected record failure')
            return original(db, key, kind, payload, **kwargs)
        with patch('mercury.records.append', side_effect=fail), self.assertRaises(records.RecordError):
            self.trader.match()
        self.assertEqual(records.read(self.trader), before)
        self.assertTrue(audit(self.trader)['ok'])
        self.trader.match()
        self.trader.match()
        self.assertEqual(len(self.trader.history('fills')), 1)

    def test_submit_record_failure_is_not_swallowed_as_risk_rejection(self):
        original = records.append
        def fail(db, key, kind, payload, **kwargs):
            if kind == 'order_calculation':
                raise records.RecordError('injected write failure')
            return original(db, key, kind, payload, **kwargs)
        with patch('mercury.records.append', side_effect=fail), self.assertRaises(ValueError):
            self.cycle()
        self.assertEqual(self.trader.history('orders'), [])
        with self.trader._read() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM intents').fetchone()[0], 0)
        self.cycle()
        self.assertEqual(len(self.trader.history('orders')), 1)

    def test_report_interruption_recovery_uses_committed_intent_and_trace(self):
        from mercury import workflow
        original = workflow.artifact
        def fail(path, value):
            if Path(path).name == 'risk.json':
                raise OSError('never persist private provider text')
            return original(path, value)
        with patch('mercury.workflow.artifact', side_effect=fail), self.assertRaises(OSError):
            self.cycle()
        self.assertEqual(records.listing(self.trader, status='failed')['total'], 1)
        with patch('mercury.strategy.compile_intents', side_effect=AssertionError('must not recompute')):
            self.cycle()
        self.assertEqual(len(self.trader.history('orders')), 1)
        data = records.read(self.trader)
        self.assertNotIn('private provider', canonical(data))
        self.assertTrue(any(e['kind'] == 'failure' for e in data['entries']))

    def test_recovery_code_identity_is_per_actual_calculation(self):
        with patch('mercury.records.code_identity', return_value={'sha256':'old'}), patch.object(self.agent,'decide',side_effect=ValueError('stop')):
            with self.assertRaises(ValueError): self.cycle()
        with patch('mercury.records.code_identity', return_value={'sha256':'new'}): self.cycle()
        c = records.read(self.trader)['cycles'][0]
        calc = next(e['payload'] for e in c['entries'] if e['kind']=='calculations')
        self.assertEqual(c['code']['sha256'], 'old')
        self.assertEqual(calc['code']['sha256'], 'new')

    def test_partial_fill_cancel_concurrency_and_recorded_capacity(self):
        spec = synthetic_episode()
        spec['execution_policy'] = {'max_fill_qty':100}
        self.trader = _account(self.root/'partial.db', spec)
        receipt = self.cycle()
        self.next_quote()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: self.trader.match(), range(2)))
        self.assertEqual(len(self.trader.history('fills')),1)
        c = records.show(self.trader, cycle_id=receipt['cycle_id'])['cycle']
        self.assertEqual(c['status'],'partially_filled')
        self.assertEqual(c['fills'][0]['calculation']['payload']['capacity_remaining_before'],100)
        order = self.trader.history('orders')[0]
        self.trader.cancel(order['id'])
        c = records.show(self.trader, cycle_id=receipt['cycle_id'])['cycle']
        self.assertEqual(c['status'],'closed_with_remainder')
        self.assertTrue(any(e['payload'].get('event_kind')=='cancel' for e in c['execution_entries']))
        self.assertTrue(audit(self.trader)['ok'])

    def test_hold_risk_rejection_missing_signal_and_unvalidated_output(self):
        self.cycle('hold', OfflineAgent(offline_template('0'),name='hold'))
        self.cycle('risk', OfflineAgent(offline_template('.9'),name='risk'))
        broken = offline_template()
        broken['signals'][0]['window']=60
        with self.assertRaises(ValueError): self.cycle('missing', OfflineAgent(broken,name='missing'))
        with patch.object(self.agent,'decide',return_value={'malformed':'PRIVATE_PROVIDER_CONTENT'}), self.assertRaises(ValueError):
            self.cycle('bad-model')
        data = records.read(self.trader)
        statuses = {c['model']:c['status'] for c in data['cycles']}
        self.assertEqual(statuses['hold'],'holding')
        self.assertEqual(statuses['risk'],'risk_rejected')
        self.assertEqual(statuses['missing'],'failed')
        self.assertNotIn('PRIVATE_PROVIDER_CONTENT',canonical(data))
        self.assertTrue(any(e['kind']=='partial_calculation' for e in data['entries']))

    def test_unfilled_limit_retains_executed_price_and_unexecuted_fee(self):
        spec = synthetic_episode()
        spec['slippage_bps'] = '2'
        self.trader = _account(self.root/'slippage.db', spec)
        self.cycle()
        self.next_quote()
        result = self.trader.match()[0]
        self.assertEqual(result['reason'], 'limit not reached after slippage')
        entry = [e for e in records.read(self.trader)['entries'] if e['kind']=='execution_attempt'][-1]
        calc = entry['payload']['calculation']
        self.assertEqual(calc['price']['rounded_price'], '10.02')
        self.assertEqual(calc['price_status'], 'executed')
        self.assertEqual(calc['fee_status'], 'not_executed')
        self.assertEqual(self.trader.history('fills'), [])
        self.assertTrue(audit(self.trader)['ok'])

    def test_legacy_complete_receipt_recovery_does_not_fabricate_current_history(self):
        receipt = self.cycle()
        obs = build_observation(self.trader,self.spec['universe'])
        self.remove_test_record_tables()
        records.upgrade(self.trader,self.root/'legacy-backup.db')
        self.assertEqual(run_cycle(self.trader,self.agent,obs,output=self.root/'cycle'),receipt)
        self.assertEqual(records.read(self.trader)['cycles'],[])

    def test_same_intent_from_another_model_does_not_reassign_fill(self):
        receipt = self.cycle('original')
        # A failed run after model/intent journaling can share precisely the original
        # intent. Exercise original-intent replay with another explicit cycle context.
        c = records.read(self.trader)['cycles'][0]
        intent = next(e['payload'] for e in c['entries'] if e['kind']=='intent')
        obs = build_observation(self.trader,self.spec['universe'])
        binding = dict(c['binding'], agent={'provider':'offline','model':'other'})
        with records.cycle_context(self.trader,binding,obs,self.root/'other'):
            records.stage('intent',intent)
            self.trader.submit_intent(intent,obs)
        self.next_quote()
        fill = self.trader.match()[0]
        self.assertEqual(records.show(self.trader,fill_id=fill['id'])['cycle']['cycle_id'], receipt['cycle_id'])
        output = self.root/'other-model.json'
        records.export(self.trader,output,model='other')
        exported = json.loads(output.read_text())
        self.assertEqual(exported['fills'],[])
        self.assertTrue(all(e['attributed_cycle_id']==digest(binding) for e in exported['entries']))

    def test_readonly_cli_pagination_export_and_conflicting_output(self):
        receipt = self.cycle()
        self.next_quote()
        fill = self.trader.match()[0]
        before = hashlib.sha256(self.trader.path.read_bytes()).hexdigest()
        self.assertEqual(self.cli('list','--limit','1')['total'],1)
        self.assertEqual(self.cli('list','--offset','1')['cycles'],[])
        self.assertEqual(self.cli('show','--fill-id',fill['id'])['cycle']['cycle_id'],receipt['cycle_id'])
        export = self.root/'records.json'
        self.cli('export','--format','json','--out',str(export))
        self.cli('export','--format','json','--out',str(export))
        self.assertEqual(len(json.loads(export.read_text())['fills']),1)
        self.cli('export','--format','csv','--out',str(self.root/'csv'))
        self.assertEqual(hashlib.sha256(self.trader.path.read_bytes()).hexdigest(),before)
        with self.assertRaises(ValueError): records.export(self.trader,export,model='absent')
        with self.assertRaises(ValueError): records.listing(self.trader,limit=0)
        with self.assertRaises(ValueError): records.listing(self.trader,since='2026-09-25T00:00:00Z',until='2026-09-24T00:00:00Z')
        unknown = self.root/'unknown.json'
        records.export(self.trader,unknown,model='absent')
        exported = json.loads(unknown.read_text())
        self.assertEqual(exported['fills'],[])
        self.assertEqual(exported['snapshots'],[])

    def test_csv_formula_cells_and_declared_session_identity(self):
        values = ['=1+1','+cmd','-cmd','@SUM(1)','\t=1','  =1','normal']
        result = records._csv([{'x':x} for x in values],['x']).decode('utf-8-sig')
        rows = list(csv.DictReader(io.StringIO(result)))
        self.assertTrue(all(r['x'].startswith("'") for r in rows[:-1]))
        agent = agent_from_config({'provider':'offline','name':'adapter','strategy':offline_template(),
            'provenance':{'model':'declared-model','decision_maker':'codex-session','session_source':'session-test'}})
        self.cycle(agent=agent)
        c = records.read(self.trader)['cycles'][0]
        self.assertEqual((c['model'],c['decision_maker'],c['session_source'],c['adapter']),
                         ('declared-model','codex-session','session-test','offline'))

    def remove_test_record_tables(self):
        # This helper only removes instrumentation in this test's disposable database
        # to reproduce a pre-feature account; it never touches a user database.
        with self.trader._tx() as db:
            for name in ('record_entries','record_cycles','record_intent_links','record_meta'):
                db.execute('DROP TABLE '+name)

    def test_verified_additive_migration_idempotent_and_legacy_history_import(self):
        receipt = self.cycle()
        self.next_quote()
        self.trader.match()
        self.remove_test_record_tables()
        with closing(sqlite3.connect(self.trader.path)) as db:
            before = '\n'.join(db.iterdump())
        self.assertEqual(records.read(self.trader)['fills'][0]['calculation_status'],'not_recorded')
        backup = self.root/'backup.db'
        result = records.upgrade(self.trader,backup)
        self.assertEqual(result['backup_sha256'], hashlib.sha256(backup.read_bytes()).hexdigest())
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual('\n'.join(db.iterdump()),before)
        self.assertEqual(records.upgrade(self.trader,backup)['status'],'already_enabled')
        self.assertEqual(records.read(self.trader)['cycles'],[])
        records.import_run(self.trader,self.root/'cycle')
        self.assertEqual(records.import_run(self.trader,self.root/'cycle')['status'],'already_recorded')
        c = records.show(self.trader,cycle_id=receipt['cycle_id'])['cycle']
        self.assertEqual(c['origin'],'imported_history')
        self.assertIsNone(c['code'])
        self.assertEqual(c['fills'][0]['calculation_status'],'not_recorded')
        self.assertTrue(audit(self.trader)['ok'])

    def test_migration_failure_keeps_backup_and_original_tables(self):
        self.remove_test_record_tables()
        with closing(sqlite3.connect(self.trader.path)) as db:
            before = '\n'.join(db.iterdump())
        original = records.initialize
        def fail(db):
            original(db)
            raise records.RecordError('DDL failure')
        backup = self.root/'backup.db'
        with patch('mercury.records.initialize',side_effect=fail), self.assertRaises(records.RecordError):
            records.upgrade(self.trader,backup)
        with closing(sqlite3.connect(self.trader.path)) as db:
            self.assertEqual('\n'.join(db.iterdump()),before)
        self.assertTrue(backup.exists())
        with self.assertRaises(FileExistsError): records.upgrade(self.trader,backup)
        records.upgrade(self.trader,self.root/'backup-retry.db')

    def test_records_hash_tamper_detected(self):
        self.cycle()
        with self.trader._tx() as db:
            db.execute("UPDATE record_entries SET payload='{}' WHERE kind='calculations'")
        with self.assertRaisesRegex(ValueError,'hash mismatch'): records.read(self.trader)


if __name__ == '__main__':
    unittest.main()
