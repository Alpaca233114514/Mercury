import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mercury.analytics import sharpe_for_schedule,trade_groups
from mercury.common import digest
from mercury.engine import PaperTrader
from mercury.evaluation import _account,aligned_benchmark,compare_agents
from mercury.examples import offline_template,synthetic_episode
from mercury.models import OfflineAgent
from mercury.observation import build_observation
from mercury.workflow import artifact,public_account,read_artifact,run_cycle


class WorkflowEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.spec=synthetic_episode()

    def account(self):return _account(self.root/'single.db',self.spec)

    def test_complete_cn_episode_and_identical_market_observations(self):
        agents={'a':OfflineAgent(offline_template(),name='a'),'b':OfflineAgent(offline_template('0.2'),name='b')}
        result=compare_agents(self.spec,agents,output=self.root/'compare')
        for model in result['models'].values():
            self.assertEqual(model['refused_cycles'],0);self.assertEqual(model['report']['fills'],2)
            self.assertEqual(len(model['report']['trade_groups']['closed']),1)
            self.assertGreater(float(model['report']['turnover']),0)
            self.assertEqual(model['report']['round_trip_win_rate'],1)
            self.assertIsNone(model['sampling']['annualized_sharpe'])
            self.assertEqual(model['benchmark']['coverage'],4)
        events=[read_artifact(p) for p in sorted((self.root/'compare').glob('event-*.json'))]
        for event in events:
            if event['cycles']:self.assertEqual(event['cycles']['a']['observation_id'],event['cycles']['b']['observation_id'])
        self.assertEqual(compare_agents(self.spec,agents,output=self.root/'compare'),result)

    def test_cycle_recovery_is_idempotent_and_tampering_rejected(self):
        trader=self.account();obs=build_observation(trader,self.spec['universe']);agent=OfflineAgent(offline_template())
        result=run_cycle(trader,agent,obs,output=self.root/'cycle')
        self.assertEqual(run_cycle(trader,agent,obs,output=self.root/'cycle'),result)
        self.assertEqual(len(trader.history('orders')),1)
        path=self.root/'cycle'/'receipt.json';path.write_text('{}',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'hash'):run_cycle(trader,agent,obs,output=self.root/'cycle')

    def test_cycle_records_audit_and_cached_cycle_rechecks_current_ledger(self):
        trader=self.account();obs=build_observation(trader,self.spec['universe']);agent=OfflineAgent(offline_template())
        receipt=run_cycle(trader,agent,obs,output=self.root/'audited')
        self.assertTrue(receipt['audit']['ok'])
        from mercury.audit import audit
        self.assertEqual(receipt['audit']['event_head'],audit(trader)['event_head'])
        # Deliberate corruption of a temporary test account, never user evidence.
        import sqlite3
        db=sqlite3.connect(trader.path)
        try:
            with db:db.execute("UPDATE account SET cash='1'")
        finally:db.close()
        with self.assertRaisesRegex(ValueError,'phase failed: audit'):
            run_cycle(trader,agent,obs,output=self.root/'audited')
        self.assertEqual(read_artifact(self.root/'audited'/'receipt.json'),receipt)
        self.assertTrue(list((self.root/'audited').glob('failure-audit-*.json')))

    def test_failed_audit_prevents_receipt_and_retains_fixed_failure(self):
        trader=self.account();obs=build_observation(trader,self.spec['universe']);agent=OfflineAgent(offline_template())
        with patch('mercury.workflow.audit',return_value={'ok':False,'errors':['external private text']}),self.assertRaisesRegex(ValueError,'phase failed: audit'):
            run_cycle(trader,agent,obs,output=self.root/'failed-audit')
        self.assertFalse((self.root/'failed-audit'/'receipt.json').exists())
        failure=read_artifact(next((self.root/'failed-audit').glob('failure-audit-*.json')))
        self.assertEqual(failure['phase'],'audit');self.assertNotIn('external private text',json.dumps(failure))
        # Resume reuses the committed intent and audits before completing, without a duplicate order.
        receipt=run_cycle(trader,agent,obs,output=self.root/'failed-audit')
        self.assertTrue(receipt['audit']['ok']);self.assertEqual(len(trader.history('orders')),1)

    def test_completed_receipt_requires_the_ledger_intent_and_feedback_binding(self):
        trader=self.account();obs=build_observation(trader,self.spec['universe']);agent=OfflineAgent(offline_template())
        receipt=run_cycle(trader,agent,obs,output=self.root/'receipt')
        self.assertIn('previous-review.json',receipt['artifact_refs'])
        with patch.object(trader,'intent_result',return_value=None),self.assertRaisesRegex(ValueError,'ledger intent'):
            run_cycle(trader,agent,obs,output=self.root/'receipt')
        with self.assertRaisesRegex(ValueError,'immutable'):
            run_cycle(trader,agent,obs,output=self.root/'receipt',feedback=receipt)

    def test_interruption_after_ledger_intent_before_receipt_recovers(self):
        trader=self.account();obs=build_observation(trader,self.spec['universe']);agent=OfflineAgent(offline_template())
        from mercury import workflow
        original=workflow.artifact
        def fail(path,value):
            if Path(path).name=='risk.json':raise OSError('synthetic interruption')
            return original(path,value)
        with patch('mercury.workflow.artifact',side_effect=fail),self.assertRaises(OSError):run_cycle(trader,agent,obs,output=self.root/'cycle')
        run_cycle(trader,agent,obs,output=self.root/'cycle')
        self.assertEqual(len(trader.history('orders')),1)

    def test_partial_seed_recovery_does_not_duplicate_or_overwrite(self):
        original=PaperTrader.register_session
        calls=0
        def fail_once(trader,data):
            nonlocal calls
            calls+=1
            if calls==2:raise OSError('synthetic seed interruption')
            return original(trader,data)
        with patch.object(PaperTrader,'register_session',fail_once),self.assertRaises(OSError):self.account()
        trader=self.account()
        with trader._read() as db:self.assertEqual(db.execute('SELECT count(*) FROM instrument_days').fetchone()[0],2)
        self.assertEqual(len(build_observation(trader,self.spec['universe'])['series']['600519.SH']),3)

    def test_invalid_model_output_records_fixed_phase_diagnostic(self):
        trader=self.account();obs=build_observation(trader,self.spec['universe']);agent=OfflineAgent(offline_template())
        with patch.object(agent,'decide',side_effect=ValueError('do not log provider secret')),self.assertRaisesRegex(ValueError,'model'):
            run_cycle(trader,agent,obs,output=self.root/'bad')
        failure=read_artifact(next((self.root/'bad').glob('failure-model-*.json')))
        self.assertEqual(failure['phase'],'model');self.assertNotIn('provider secret',json.dumps(failure))
        self.assertEqual(trader.history('orders'),[])

    def test_model_sees_public_constraints_and_previous_local_feedback(self):
        trader=self.account();obs=build_observation(trader,self.spec['universe']);agent=OfflineAgent(offline_template())
        with patch.object(agent,'decide',wraps=agent.decide) as call:
            receipt=run_cycle(trader,agent,obs,output=self.root/'first')
            context=call.call_args.args[1]
        self.assertEqual(context['constraints']['risk']['max_position_weight'],'0.3')
        self.assertNotIn('ledger_state_sha256',context);self.assertNotIn(str(trader.path),json.dumps(context))
        with patch.object(agent,'decide',wraps=agent.decide) as call:
            run_cycle(trader,agent,obs,output=self.root/'second',feedback=receipt)
        self.assertIn('risk_reasons',call.call_args.args[2]);self.assertIn('execution',call.call_args.args[2])

    def test_live_snapshot_preserves_actual_time_and_slow_model_does_not_refreeze(self):
        trader=self.account();obs=build_observation(trader,self.spec['universe']);agent=OfflineAgent(offline_template())
        snapshot=trader.portfolio();snapshot.update(mode='live',time='2026-09-24T02:00:01.000000+00:00')
        with patch.object(trader,'portfolio',return_value=snapshot):context=public_account(trader,obs)
        self.assertEqual(context['time'],snapshot['time']);self.assertEqual(context['snapshot_at'],snapshot['time'])
        # The post-model path must not rebuild the five-second-bound live context.
        with patch('mercury.workflow.public_account',side_effect=[context,ValueError('late by 30 seconds')]) as frozen:
            run_cycle(trader,agent,obs,output=self.root/'slow')
        self.assertEqual(frozen.call_count,1)

    def test_benchmark_missing_or_future_not_filled_with_zero(self):
        points=[{'time':event['time'],'equity':'100'} for event in self.spec['events']]
        missing=copy.deepcopy(self.spec['benchmark']);missing['series']=missing['series'][:-1]
        self.assertIsNone(aligned_benchmark(missing,points,self.spec['universe'])['return'])
        future=copy.deepcopy(self.spec['benchmark']);future['series'][0]['available_at']='2026-09-28T10:01:00+08:00'
        self.assertIsNone(aligned_benchmark(future,points,self.spec['universe'])['return'])

    def test_partial_sell_fill_fraction_is_not_round_trip_win_rate(self):
        fills=[{'id':str(i),'symbol':'600519.SH','side':side,'qty':qty,'notional':str(amount),'fee':'1','time':str(i)}
               for i,(side,qty,amount) in enumerate([('buy',100,1000),('sell',40,500),('sell',60,400)])]
        result=trade_groups(fills)
        self.assertEqual(result['round_trip_win_rate'],0);self.assertEqual(len(result['closed']),1)
        self.assertEqual(result['closed'][0]['net_pnl'],'-103')

    def test_sharpe_declared_daily_rejects_minute_samples(self):
        from datetime import datetime,timedelta,timezone
        first=datetime(2026,9,24,2,tzinfo=timezone.utc)
        points=[{'time':(first+timedelta(minutes=i)).isoformat(),'equity':str(100+i+(i%2))} for i in range(21)]
        schedule={'frequency':'daily','expected_times':[p['time'] for p in points],'periods_per_year':252,'min_returns':20,'calendar':self.spec['calendar']}
        self.assertIsNone(sharpe_for_schedule(points,schedule)['annualized_sharpe'])

    def test_sharpe_only_complete_calendar_with_enough_returns(self):
        days=self.spec['calendar']['trading_days'][:22]
        points=[{'time':day+'T07:00:00+00:00','equity':str(100+i+(i%2))} for i,day in enumerate(days)]
        schedule={'frequency':'daily','expected_times':[p['time'] for p in points],'periods_per_year':252,'min_returns':20,'calendar':self.spec['calendar']}
        self.assertIsNotNone(sharpe_for_schedule(points,schedule)['annualized_sharpe'])
        self.assertIsNone(sharpe_for_schedule(points[:-1],schedule)['annualized_sharpe'])

    def test_regular_weekend_or_night_sharpe_stays_null(self):
        from datetime import datetime,timedelta,timezone
        for first in (datetime(2026,9,26,tzinfo=timezone.utc),datetime(2026,9,24,15,tzinfo=timezone.utc)):
            points=[{'time':(first+timedelta(hours=i)).isoformat(),'equity':str(100000+i*100+i*i)} for i in range(21)]
            schedule={'frequency':'regular','expected_times':[p['time'] for p in points],'periods_per_year':1008,'min_returns':20}
            self.assertIsNone(sharpe_for_schedule(points,schedule)['annualized_sharpe'])

    def test_existing_account_with_other_capital_or_fee_contract_rejected(self):
        for field,value in [('cash','200000'),('fee_bps','4'),('slippage_bps','3')]:
            folder=self.root/field;folder.mkdir()
            kwargs={'cash':self.spec['cash'],'fee_bps':self.spec['fee_bps'],'slippage_bps':self.spec['slippage_bps']};kwargs[field]=value
            PaperTrader.create(folder/'account.db',market='CN',mode='replay',start=self.spec['events'][0]['time'],calendar=self.spec['calendar'],
                               minimum_commission=self.spec['minimum_commission'],risk_policy=self.spec['risk_policy'],**kwargs)
            with self.assertRaisesRegex(ValueError,'initialization/policies'):_account(folder/'account.db',self.spec)

    def test_live_portfolio_valuation_uses_frozen_observation_not_later_mark(self):
        trader=self.account();obs=build_observation(trader,self.spec['universe']);agent=OfflineAgent(offline_template())
        run_cycle(trader,agent,obs,output=self.root/'buy')
        trader.advance(self.spec['events'][1]['time']);trader.match()
        frozen=build_observation(trader,self.spec['universe']);portfolio=trader.portfolio()
        from datetime import datetime,timedelta
        portfolio.update(mode='live',time=(datetime.fromisoformat(frozen['as_of'])+timedelta(seconds=3)).isoformat())
        portfolio['positions'][0]['market_value']='999999'
        portfolio['equity']='999999'
        with patch.object(trader,'portfolio',return_value=portfolio):context=public_account(trader,frozen)
        qty=context['positions'][0]['qty']
        from decimal import Decimal
        expected=Decimal(frozen['series']['600519.SH'][-1]['price'])*qty
        self.assertEqual(Decimal(context['positions'][0]['market_value']),expected)
        self.assertEqual(Decimal(context['equity']),Decimal(context['cash'])+expected)
        self.assertEqual(context['valuation_cutoff'],frozen['as_of'])

    def test_new_observation_waits_for_existing_unfilled_order(self):
        self.spec['quotes'][3]['price']='10.02'
        trader=self.account();agent=OfflineAgent(offline_template())
        first=run_cycle(trader,agent,build_observation(trader,self.spec['universe']),output=self.root/'first-wait')
        trader.advance(self.spec['events'][1]['time']);trader.match()
        second=run_cycle(trader,agent,build_observation(trader,self.spec['universe']),output=self.root/'second-wait',feedback=first)
        self.assertNotEqual(first['observation_id'],second['observation_id'])
        self.assertEqual(len(trader.history('orders')),1)
        self.assertEqual(trader.history('fills'),[])
        intent=read_artifact(self.root/'second-wait'/'intent.json')
        self.assertEqual(intent['orders'],[]);self.assertIn('outstanding order remainder',intent['reason'])

    def test_partial_remainder_waits_without_duplicate_order(self):
        self.spec['execution_policy']={'max_fill_qty':100}
        trader=self.account();agent=OfflineAgent(offline_template())
        first=run_cycle(trader,agent,build_observation(trader,self.spec['universe']),output=self.root/'partial-first')
        trader.advance(self.spec['events'][1]['time']);trader.match()
        obs=build_observation(trader,self.spec['universe']);context=public_account(trader,obs)
        self.assertEqual(context['open_orders'][0]['remaining_qty'],800)
        second=run_cycle(trader,agent,obs,output=self.root/'partial-second',feedback=first)
        self.assertEqual(len(trader.history('orders')),1)
        self.assertEqual(read_artifact(self.root/'partial-second'/'intent.json')['orders'],[])
