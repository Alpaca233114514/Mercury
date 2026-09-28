import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from mercury import PaperTrader
from mercury.audit import audit
from mercury.common import digest, timestamp
from mercury.observation import build_observation
from test_strategy import seal, strategy_for

CALENDAR = json.loads((Path(__file__).resolve().parent.parent/'config/cn-calendar-2026.json').read_text())
POLICY = {'version':'cn-risk-v1','currency':'CNY','max_position_weight':'.2',
          'max_order_notional':'5000','max_daily_turnover':'.2','max_daily_loss':'.01'}
TICKER='600519.SH'

class RiskTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.t=self.create('risk.db', risk_policy=POLICY)

    def create(self,name,**options):
        t=PaperTrader.create(Path(self.temp.name)/name,market='CN',calendar=CALENDAR,cash='100000',
             fee_bps='0',slippage_bps='0',start='2026-09-24T10:00:00+08:00',**options)
        source={'synthetic':True,'symbol':TICKER,'reference':'controlled fixture','available_at':'2026-09-24T09:00:00+08:00'}
        t.register_instrument({'symbol':TICKER,'profile':'SH_MAIN_STOCK','source':source})
        t.register_session({'symbol':TICKER,'day':'2026-09-24','reference_price':'10','suspended':False,
                'risk_warning':False,'listing_session':100,'source':dict(source,day='2026-09-24')})
        t.import_quote(ticker=TICKER,price='10',asof='2026-09-24T10:00:00+08:00',currency='CNY',evidence={'synthetic':True})
        return t

    def intent(self,orders=None,t=None):
        t=t or self.t
        obs=build_observation(t,[TICKER])
        intent=seal({'version':'cn-trade-intent-v1','observation_id':obs['observation_id'],'as_of':obs['as_of'],
            'strategy_id':digest('controlled strategy'),'reason':'Controlled risk regression',
            'holding':{'max_sessions':5},'invalidation':{'expires_at':timestamp('2026-09-24T15:00:00+08:00'),
            'max_quote_age_seconds':300,'min_signal':'-1'},'orders':orders if orders is not None else [self.order()],
            'evidence_ids':[obs['series'][TICKER][-1]['quote_id']]},'intent_id')
        return intent,obs

    def order(self,qty=100,**extra):
        return dict(symbol=TICKER,side='buy',qty=qty,limit_price='10',target_weight='.1',**extra)

    def next_quote(self,t=None,minute='01',price='10'):
        t=t or self.t
        at='2026-09-24T10:'+minute+':00+08:00'
        t.import_quote(ticker=TICKER,price=price,asof=at,currency='CNY',evidence={'synthetic':True})
        if t.portfolio()["time"] != timestamp(at):
            t.advance(at)

    def test_batch_atomic_idempotent_immutable_and_no_direct_bypass(self):
        with self.assertRaisesRegex(ValueError,'submit_intent'):
            self.t.submit(ticker=TICKER,side='buy',qty=100,request_id='bypass',limit_price='10')
        with self.assertRaisesRegex(ValueError,'immutable'):
            self.t.configure_risk(POLICY)
        second = "000001.SZ"
        source={'synthetic':True,'symbol':second,'reference':'controlled fixture','available_at':'2026-09-24T09:00:00+08:00'}
        self.t.register_instrument({'symbol':second,'profile':'SZ_MAIN_STOCK','source':source})
        self.t.register_session({'symbol':second,'day':'2026-09-24','reference_price':'10','suspended':False,
                'risk_warning':False,'listing_session':100,'source':dict(source,day='2026-09-24')})
        self.t.import_quote(ticker=second,price='10',asof='2026-09-24T10:00:00+08:00',currency='CNY',evidence={'synthetic':True})
        intent,obs=self.intent([self.order(100),dict(self.order(500),symbol=second)])
        obs=build_observation(self.t,[TICKER,second])
        intent=seal(dict(intent,observation_id=obs['observation_id']), 'intent_id')
        receipt=self.t.submit_intent(intent,obs)
        self.assertEqual(receipt['status'],'rejected')
        self.assertEqual(self.t.history(),[])
        self.assertEqual(self.t.submit_intent(intent,obs),receipt)
        intent,obs=self.intent()
        receipt=self.t.submit_intent(intent,obs)
        self.assertEqual(receipt['status'],'accepted',receipt)
        self.assertEqual(self.t.intent_result(intent['intent_id']),receipt)
        self.assertEqual(self.t.submit_intent(intent,obs),receipt)
        self.assertEqual(len(self.t.history()),1)
        self.assertTrue(audit(self.t)['ok'],audit(self.t))

    def test_stale_economic_state_rejected_without_orders(self):
        intent,obs=self.intent()
        receipt=self.t.submit_intent(intent,obs,account_state_sha256='0'*64)
        self.assertEqual(receipt['status'],'rejected')
        self.assertIn('economic state',receipt['reasons'][0])
        self.assertEqual(self.t.history(),[])

    def test_partial_cumulative_fee_capacity_cancel_and_replay(self):
        t=self.create('partial.db',execution_policy={'max_fill_qty':60})
        order=t.submit(ticker=TICKER,side='buy',qty=200,request_id='partial',limit_price='10')
        self.next_quote(t)
        first=t.match()[0]
        self.assertEqual((first['qty'],Decimal(first['fee'])),(60,Decimal('5.01')))
        self.assertEqual(Decimal(t.portfolio(record=True)['reserved_cash']),Decimal('1400.01'))
        self.assertEqual(t.match()[0]['status'],'open')
        # A recapture with the same provider time cannot increase capacity.
        t.import_quote(ticker=TICKER,price='10',asof='2026-09-24T10:01:00+08:00',currency='CNY',evidence={'synthetic':True,'recapture':2})
        self.assertEqual(t.match()[0]['status'],'open')
        self.next_quote(t,minute='02')
        second=t.match()[0]
        self.assertEqual((second['qty'],Decimal(second['fee'])),(60,Decimal('0')))
        t.cancel(order['id'])
        self.assertEqual(Decimal(t.portfolio(record=True)['reserved_cash']),0)
        self.assertEqual(t.history()[0]['qty'],200)
        self.assertEqual(t.portfolio()['positions'][0]['qty'],120)
        self.assertTrue(audit(t)['ok'],audit(t))

    def test_latency_requires_new_provider_time(self):
        t=self.create('latency.db',execution_policy={'latency_seconds':90})
        t.submit(ticker=TICKER,side='buy',qty=100,request_id='latency',limit_price='10')
        self.next_quote(t)
        t.advance('2026-09-24T10:02:00+08:00')
        self.assertEqual(t.match()[0]['status'],'open')
        self.next_quote(t,minute='02')
        self.assertEqual(t.match()[0]['qty'],100)
        self.assertTrue(audit(t)['ok'],audit(t))

    def test_match_rechecks_single_order_after_price_change(self):
        intent,obs=self.intent([self.order(400)])
        receipt=self.t.submit_intent(intent,obs)
        self.assertEqual(receipt['status'],'accepted',receipt)
        self.next_quote(price='10.5')
        self.assertEqual(self.t.match()[0]['status'],'open') # limit prevents execution, no price guessed
        self.assertEqual(self.t.history('fills'),[])
        self.assertTrue(audit(self.t)['ok'],audit(self.t))

    def test_protected_partial_and_audit_tamper(self):
        t=self.create('guarded-partial.db',risk_policy=POLICY,execution_policy={'max_fill_qty':60})
        intent,obs=self.intent([self.order(200)],t=t)
        self.assertEqual(t.submit_intent(intent,obs)['status'],'accepted')
        self.next_quote(t)
        self.assertEqual(t.match()[0]['qty'],60)
        t.portfolio(record=True)
        self.next_quote(t,minute='02')
        self.assertEqual(t.match()[0]['qty'],60)
        t.portfolio(record=True)
        self.assertTrue(audit(t)['ok'],audit(t))
        with t._tx() as db:
            db.execute('UPDATE order_progress SET remaining_qty=1')
        self.assertFalse(audit(t)['ok'])

    def test_daily_loss_blocks_new_buys_and_match(self):
        policy=dict(POLICY,max_position_weight='.5',max_order_notional='20000',max_daily_turnover='1')
        t=self.create('loss.db',risk_policy=policy)
        intent,obs=self.intent([self.order(1000)],t=t)
        self.assertEqual(t.submit_intent(intent,obs)['status'],'accepted')
        self.next_quote(t)
        t.match()
        # Pending second buy is admitted before loss; execution must recheck later.
        intent,obs=self.intent(t=t)
        self.assertEqual(t.submit_intent(intent,obs)['status'],'accepted')
        self.next_quote(t,minute='02',price='9')
        receipt=t.match()[0]
        self.assertEqual(receipt['status'],'rejected',receipt)
        self.assertIn('daily loss',receipt['reason'])
        intent,obs=self.intent(t=t)
        result=t.submit_intent(intent,obs)
        self.assertEqual(result['status'],'rejected',result)
        self.assertIn('daily loss',result['reasons'][0])
        self.assertTrue(audit(t)['ok'],audit(t))

    def test_concurrent_same_intent_only_one_order_and_daily_commitment(self):
        from concurrent.futures import ThreadPoolExecutor
        intent,obs=self.intent()
        with ThreadPoolExecutor(max_workers=4) as pool:
            receipts=list(pool.map(lambda _:self.t.submit_intent(intent,obs),range(8)))
        self.assertTrue(all(x==receipts[0] for x in receipts))
        self.assertEqual(len(self.t.history()),1)
        # A restrictive daily budget counts already open orders and fees.
        policy=dict(POLICY,max_daily_turnover='.015')
        t=self.create('turnover.db',risk_policy=policy)
        intent,obs=self.intent(t=t)
        self.assertEqual(t.submit_intent(intent,obs)['status'],'accepted')
        intent=seal(dict(intent,reason='Another independently identified request'),'intent_id')
        rejected=t.submit_intent(intent,obs)
        self.assertEqual(rejected['status'],'rejected',rejected)
        self.assertIn('daily turnover',rejected['reasons'][0])
        self.assertEqual(len(t.history()),1)

    def test_capacity_shared_between_orders(self):
        t=self.create('capacity.db',execution_policy={'max_fill_qty':60})
        for key in ('a','b'):
            t.submit(ticker=TICKER,side='buy',qty=100,request_id=key,limit_price='10')
        self.next_quote(t)
        result=t.match()
        self.assertEqual(sum(x.get('qty',0) for x in result),60)
        self.assertEqual(len(t.history('fills')),1)
        self.assertTrue(audit(t)['ok'],audit(t))

    def test_position_cap_rejects_without_clipping(self):
        policy=dict(POLICY,max_position_weight='.01')
        t=self.create('position.db',risk_policy=policy)
        intent,obs=self.intent([dict(self.order(),target_weight='.005')],t=t)
        receipt=t.submit_intent(intent,obs)
        self.assertEqual(receipt['status'],'rejected',receipt)
        self.assertIn('security weight',receipt['reasons'][0])
        self.assertEqual(t.history(),[])

    def test_spread_is_explicit_and_independently_replayed(self):
        t=self.create('spread.db',execution_policy={'spread_bps':'20'})
        t.submit(ticker=TICKER,side='buy',qty=100,request_id='spread',limit_price='10.1')
        self.next_quote(t)
        fill=t.match()[0]
        self.assertEqual(Decimal(fill['price']),Decimal('10.01'))
        self.assertTrue(audit(t)['ok'],audit(t))

    def test_forged_observation_price_rejected(self):
        intent,obs=self.intent()
        obs=copy.deepcopy(obs)
        obs['series'][TICKER][0]['price']='9'
        obs=seal(obs,'observation_id')
        intent=seal(dict(intent,observation_id=obs['observation_id']),'intent_id')
        receipt=self.t.submit_intent(intent,obs)
        self.assertEqual(receipt['status'],'rejected',receipt)
        self.assertIn('differs from execution account evidence',receipt['reasons'][0])

    def test_legacy_database_not_migrated_and_original_api_preserved(self):
        import sqlite3
        from mercury.engine import SCHEMA
        from contextlib import closing
        path=Path(self.temp.name)/'legacy.db'
        with closing(sqlite3.connect(path)) as db:
            db.executescript(SCHEMA)
            db.execute('INSERT INTO account VALUES(1,?,?,?,?,?,?,?,?)',('100000.00000000','100000.00000000','5','2','demo',timestamp('2026-09-24T10:00:00+08:00'),300,'USD'))
            db.commit()
        t=PaperTrader(path)
        with t._tx() as db:
            t._event(db,timestamp('2026-09-24T10:00:00+08:00'),'init',dict(cash='100000.00000000',fee_bps='5',slippage_bps='2',mode='demo',currency='USD',max_age=300))
        with self.assertRaisesRegex(ValueError,'no legacy migration'):
            t.configure_risk(POLICY)
        t.import_quote(ticker='AAPL',price='10',asof='2026-09-24T10:00:00+08:00',evidence={'synthetic':True})
        t.submit(ticker='AAPL',side='buy',qty=10,request_id='old',limit_price='11')
        t.import_quote(ticker='AAPL',price='10',asof='2026-09-24T10:01:00+08:00',evidence={'synthetic':True})
        t.advance('2026-09-24T10:01:00+08:00')
        self.assertEqual(t.match()[0]['qty'],10)
        self.assertTrue(audit(t)['ok'],audit(t))
        with t._read() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='ledger_version'").fetchone())

    def test_missing_prior_baseline_blocks_overnight_buy(self):
        intent,obs=self.intent()
        self.t.submit_intent(intent,obs)
        self.next_quote()
        self.t.match()
        # Failure injection in a disposable test ledger: remove last valuation.
        with self.t._tx() as db:
            db.execute('DELETE FROM risk_state')
        source={'synthetic':True,'symbol':TICKER,'day':'2026-09-28','reference':'controlled fixture','available_at':'2026-09-28T09:00:00+08:00'}
        self.t.register_session({'symbol':TICKER,'day':'2026-09-28','reference_price':'10','suspended':False,
                'risk_warning':False,'listing_session':101,'source':source})
        self.t.advance('2026-09-28T10:00:00+08:00')
        self.t.import_quote(ticker=TICKER,price='10',asof='2026-09-28T10:00:00+08:00',currency='CNY',evidence={'synthetic':True})
        intent,obs=self.intent()
        intent=seal(dict(intent,invalidation=dict(intent['invalidation'],expires_at=timestamp('2026-09-28T15:00:00+08:00'))),'intent_id')
        receipt=self.t.submit_intent(intent,obs)
        self.assertEqual(receipt['status'],'rejected',receipt)
        self.assertIn('baseline_missing',receipt['reasons'][0])
        self.assertFalse(audit(self.t)['ok']) # Missing prior evidence remains a detectable defect.

    def test_partial_sell_reserves_only_remaining_t1_and_cumulative_tax(self):
        t=self.create('sell-partial.db',execution_policy={'max_fill_qty':60})
        t.submit(ticker=TICKER,side='buy',qty=200,request_id='buy',limit_price='10')
        for minute in ('01','02','03','04'):
            self.next_quote(t,minute=minute)
            t.match()
        self.assertEqual(t.portfolio()['positions'][0]['sellable_qty'],0)
        source={'synthetic':True,'symbol':TICKER,'day':'2026-09-28','reference':'controlled fixture','available_at':'2026-09-28T09:00:00+08:00'}
        t.register_session({'symbol':TICKER,'day':'2026-09-28','reference_price':'10','suspended':False,
                'risk_warning':False,'listing_session':101,'source':source})
        t.advance('2026-09-28T10:00:00+08:00')
        t.import_quote(ticker=TICKER,price='10',asof='2026-09-28T10:00:00+08:00',currency='CNY',evidence={'synthetic':True})
        sell=t.submit(ticker=TICKER,side='sell',qty=200,request_id='sell',limit_price='10')
        for minute in ('01','02'):
            at='2026-09-28T10:'+minute+':00+08:00'
            t.import_quote(ticker=TICKER,price='10',asof=at,currency='CNY',evidence={'synthetic':True})
            t.advance(at)
            self.assertEqual(t.match()[0]['qty'],60)
            self.assertEqual(t.portfolio(record=True)['positions'][0]['available_to_sell'],0)
        self.assertEqual(sum(Decimal(f['fee']) for f in t.history('fills') if f['side']=='sell'),Decimal('5.61'))
        t.cancel(sell['id'])
        self.assertEqual(t.portfolio(record=True)['positions'][0]['available_to_sell'],80)
        self.assertTrue(audit(t)['ok'],audit(t))

    def test_slow_model_live_clock_hold_6_to_30_seconds(self):
        from unittest.mock import patch
        # Mocked local live clock, no quote collection, network or fills.
        for seconds in (6,30):
            with self.subTest(seconds=seconds):
                start=timestamp('2026-09-24T10:00:00+08:00')
                with patch('mercury.engine.utcnow',return_value=start):
                    t=PaperTrader.create(Path(self.temp.name)/('live-'+str(seconds)+'.db'),market='CN',
                        calendar=CALENDAR,mode='live',risk_policy=POLICY)
                    source={'symbol':TICKER,'reference':'controlled local live-clock mock, no real source',
                            'available_at':'2026-09-24T09:00:00+08:00'}
                    t.register_instrument({'symbol':TICKER,'profile':'SH_MAIN_STOCK','source':source})
                    obs=build_observation(t,[TICKER])
                intent=seal({'version':'cn-trade-intent-v1','observation_id':obs['observation_id'],'as_of':obs['as_of'],
                    'strategy_id':digest('controlled slow hold'),'reason':'No market data, hold cash',
                    'holding':{'max_sessions':5},'invalidation':{'expires_at':timestamp('2026-09-24T15:00:00+08:00'),
                    'max_quote_age_seconds':60,'min_signal':'-1'},'orders':[],
                    'evidence_ids':[obs['instruments'][TICKER]['source_id']]},'intent_id')
                with patch('mercury.engine.utcnow',return_value=timestamp('2026-09-24T10:00:'+str(seconds).zfill(2)+ '+08:00')):
                    self.assertEqual(t.submit_intent(intent,obs)['status'],'accepted')
                self.assertEqual(t.history('fills'),[])
                self.assertTrue(audit(t)['ok'],audit(t))

    def test_intent_expiry_releases_unreachable_limit_without_quote(self):
        intent,obs=self.intent([dict(self.order(),limit_price='9')])
        intent=seal(dict(intent,invalidation=dict(intent['invalidation'],expires_at=timestamp('2026-09-24T10:00:30+08:00'))),'intent_id')
        self.assertEqual(self.t.submit_intent(intent,obs)['status'],'accepted')
        self.t.advance('2026-09-24T10:01:00+08:00')
        result=self.t.match()[0]
        self.assertEqual(result['status'],'rejected')
        self.assertIn('expired',result['reason'])
        self.assertEqual(Decimal(self.t.portfolio()['reserved_cash']),0)
        self.assertTrue(audit(self.t)['ok'],audit(self.t))

    def test_intent_short_quote_ttl_rejects_before_limit_test(self):
        intent,obs=self.intent([dict(self.order(),limit_price='9')])
        intent=seal(dict(intent,invalidation=dict(intent['invalidation'],max_quote_age_seconds=30)),'intent_id')
        self.assertEqual(self.t.submit_intent(intent,obs)['status'],'accepted')
        self.t.advance('2026-09-24T10:01:00+08:00')
        result=self.t.match()[0]
        self.assertEqual(result['status'],'rejected')
        self.assertIn('too old',result['reason'])
        self.assertTrue(audit(self.t)['ok'],audit(self.t))

    def test_independent_replay_rejects_self_consistent_wrong_baseline(self):
        from mercury.audit import _replay_risk
        intent,obs=self.intent()
        self.assertEqual(self.t.submit_intent(intent,obs)['status'],'accepted')
        with self.t._tx() as db:
            db.execute("UPDATE risk_days SET baseline_equity='90000'")
            events=[dict(r) for r in db.execute('SELECT * FROM events ORDER BY seq')]
            for event in events:
                if event['kind']=='risk_decision':
                    payload=json.loads(event['payload'])
                    for check in payload['decision'].get('checks',[]):
                        check['baseline_equity']='90000'
                    event['payload']=json.dumps(payload)
            quotes={r['id']:dict(r) for r in db.execute('SELECT * FROM quotes')}
            errors=_replay_risk(db,events,quotes,self.t._account(db),self.t._policy(db))
            # Both claims agree, but independent historical arithmetic disagrees.
            self.assertTrue(any('independent prior valuation' in e for e in errors),errors)

    def test_live_fill_uses_one_clock_in_risk_and_fill(self):
        from datetime import datetime, timedelta
        from unittest.mock import patch
        def parse(evidence,ticker):
            return dict(price='10',asof=evidence['controlled_at'],received_at=evidence['controlled_at'],currency='CNY')
        # Controlled parser/clock only; never a real MCP source or execution.
        with patch('mercury.ashare.snapshot',side_effect=parse):
            at=timestamp('2026-09-24T10:00:00+08:00')
            with patch('mercury.engine.utcnow',return_value=at):
                t=PaperTrader.create(Path(self.temp.name)/'live-fill-clock.db',market='CN',calendar=CALENDAR,
                    mode='live',risk_policy=POLICY,slippage_bps='0')
                source={'symbol':TICKER,'reference':'controlled clock/parser test, not real data','available_at':'2026-09-24T09:00:00+08:00'}
                t.register_instrument({'symbol':TICKER,'profile':'SH_MAIN_STOCK','source':source})
                t.register_session({'symbol':TICKER,'day':'2026-09-24','reference_price':'10','suspended':False,
                    'risk_warning':False,'listing_session':100,'source':dict(source,day='2026-09-24')})
                t._store_quote(ticker=TICKER,price='10',asof=at,currency='CNY',received_at=at,
                    source='ashare-mcp',kind='mcp',evidence={'synthetic':True,'controlled_at':at})
                intent,obs=self.intent(t=t)
                self.assertEqual(t.submit_intent(intent,obs)['status'],'accepted')
            at=timestamp('2026-09-24T10:01:00+08:00')
            with patch('mercury.engine.utcnow',return_value=at):
                t._store_quote(ticker=TICKER,price='10',asof=at,currency='CNY',received_at=at,
                    source='ashare-mcp',kind='mcp',evidence={'synthetic':True,'controlled_at':at})
            clock=datetime.fromisoformat(at)
            def ticking():
                nonlocal clock
                result=clock.isoformat(timespec='microseconds')
                clock+=timedelta(milliseconds=1)
                return result
            with patch('mercury.engine.utcnow',side_effect=ticking):
                fill=t.match()[0]
            self.assertEqual(fill['time'],at)
            self.assertTrue(audit(t)['ok'],audit(t))
