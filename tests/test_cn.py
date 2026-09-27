import concurrent.futures
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from mercury import PaperTrader
from mercury.audit import audit
from mercury.analytics import performance

CALENDAR = json.loads((Path(__file__).resolve().parent.parent / "config/cn-calendar-2026.json").read_text(encoding="utf-8"))


class CNTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "cn.db"
        self.t = PaperTrader.create(self.path, market="CN", calendar=CALENDAR, cash="10000000",
                                    start="2026-09-24T10:00:00+08:00")
        self.instrument("600519.SH", "SH_MAIN_STOCK")
        self.session("600519.SH", "2026-09-24")
        self.quote("600519.SH", "10", "2026-09-24T10:00:00+08:00")

    def instrument(self, ticker, profile, **extra):
        return self.t.register_instrument(dict(symbol=ticker, profile=profile, source={"synthetic": True, "symbol": ticker, "reference": "test fixture", "available_at": "2026-09-24T09:00:00+08:00"}, **extra))

    def session(self, ticker, day, **extra):
        row = dict(symbol=ticker, day=day, reference_price="10", suspended=False,
                   risk_warning=False, listing_session=100, source={"synthetic": True, "symbol": ticker, "day": day, "reference": "test fixture", "available_at": day+"T09:00:00+08:00"})
        row.update(extra)
        return self.t.register_session(row)

    def quote(self, ticker, price, at, **extra):
        return self.t.import_quote(ticker=ticker, price=price, asof=at, currency="CNY", evidence={"synthetic": True, **extra})

    def fill_buy(self, ticker="600519.SH", qty=100, key="buy"):
        self.t.submit(ticker=ticker, side="buy", qty=qty, request_id=key, limit_price="11")
        self.quote(ticker, "10", "2026-09-24T10:01:00+08:00")
        self.t.advance("2026-09-24T10:01:00+08:00")
        return self.t.match()[0]

    def test_t1_uses_trading_day_and_fee_components(self):
        fill = self.fill_buy()
        self.assertEqual(Decimal(fill["price"]), Decimal("10.01"))
        self.assertEqual(Decimal(fill["fee"]), Decimal("5.01"))
        self.assertEqual(self.t.portfolio(record=True)["positions"][0]["sellable_qty"], 0)
        with self.assertRaisesRegex(ValueError, "T\\+1"):
            self.t.submit(ticker="600519.SH", side="sell", qty=100, request_id="same-day", limit_price="10")
        self.t.advance("2026-09-25T10:00:00+08:00")
        self.assertEqual(self.t.portfolio()["positions"][0]["sellable_qty"], 0)
        self.session("600519.SH", "2026-09-28")
        self.t.advance("2026-09-28T10:00:00+08:00")
        self.quote("600519.SH", "11", "2026-09-28T10:00:00+08:00")
        self.assertEqual(self.t.portfolio()["positions"][0]["sellable_qty"], 100)
        self.t.submit(ticker="600519.SH", side="sell", qty=100, request_id="sell", limit_price="10.5")
        self.quote("600519.SH", "11", "2026-09-28T10:01:00+08:00")
        self.t.advance("2026-09-28T10:01:00+08:00")
        sold = self.t.match()[0]
        self.assertEqual(Decimal(sold["fee"]), Decimal("5.56"))
        self.assertEqual(Decimal(sold["realized_pnl"]), Decimal("87.43"))
        self.t.portfolio(record=True)
        self.assertTrue(audit(self.t)["ok"], audit(self.t))
        self.assertEqual(performance(self.t)["fee_components"], {"commission": "10.00", "transfer": "0.02", "stamp_tax": "0.55"})

    def test_cny_usd_isolation(self):
        with self.assertRaises(ValueError):
            self.t.import_quote(ticker="AAPL", price=10, asof="2026-09-24T10:00:00+08:00", evidence={"test": True})
        with self.assertRaises(ValueError):
            self.instrument("000001.SZ", "SH_MAIN_STOCK")
        with self.assertRaises(ValueError):
            PaperTrader.create(Path(self.temp.name)/"bad.db", market="CN", currency="USD", calendar=CALENDAR, start="2026-09-24T10:00:00+08:00")

    def test_market_lot_rules(self):
        for qty in (1, 99, 101):
            with self.assertRaises(ValueError):
                self.t.submit(ticker="600519.SH", side="buy", qty=qty, request_id=str(qty), limit_price="10")
        for ticker, profile, qty in [("688001.SH", "SH_STAR_STOCK", 201), ("920001.BJ", "BJ_STOCK", 101)]:
            self.instrument(ticker, profile)
            self.session(ticker, "2026-09-24")
            self.quote(ticker, "10", "2026-09-24T10:00:00+08:00")
            self.assertEqual(self.t.submit(ticker=ticker, side="buy", qty=qty, request_id=ticker, limit_price="10")["status"], "open")
        with self.assertRaisesRegex(ValueError, "maximum"):
            self.t.submit(ticker="688001.SH", side="buy", qty=100001, request_id="too-large", limit_price="10")

    def test_calendar_and_sessions(self):
        for at in ["2026-09-24T12:00:00+08:00", "2026-09-24T14:57:00+08:00", "2026-09-25T10:00:00+08:00"]:
            self.t.advance(at)
            with self.assertRaises(ValueError):
                self.t.submit(ticker="600519.SH", side="buy", qty=100, request_id=at, limit_price="10")
        with self.assertRaisesRegex(ValueError, "calendar"):
            self.t.advance("2027-01-04T10:00:00+08:00")

    def test_day_expiry_and_auction_cancel_blackout(self):
        order = self.t.submit(ticker="600519.SH", side="buy", qty=100, request_id="expire", limit_price="10")
        self.t.advance("2026-09-24T14:58:00+08:00")
        with self.assertRaisesRegex(ValueError, "blackout"):
            self.t.cancel(order["id"])
        self.t.advance("2026-09-28T10:00:00+08:00")
        self.t.match()
        self.assertEqual(self.t.history()[0]["status"], "expired")
        self.assertEqual(Decimal(self.t.portfolio()["reserved_cash"]), 0)
        self.assertTrue(audit(self.t)["ok"], audit(self.t))

    def test_price_limits_tick_and_lock(self):
        for limit in ("11.01", "8.99", "10.005"):
            with self.assertRaises(ValueError):
                self.t.submit(ticker="600519.SH", side="buy", qty=100, request_id=limit, limit_price=limit)
        self.t.submit(ticker="600519.SH", side="buy", qty=100, request_id="lock", limit_price="11")
        self.quote("600519.SH", "11", "2026-09-24T10:01:00+08:00")
        self.t.advance("2026-09-24T10:01:00+08:00")
        self.assertEqual(self.t.match()[0]["status"], "open")
        self.assertEqual(self.t.history("fills"), [])

    def test_etf_t0_requires_profile_and_has_no_stock_taxes(self):
        ticker="511010.SH"
        self.instrument(ticker, "SH_BOND_ETF", etf_limit_percent="10")
        self.session(ticker, "2026-09-24")
        self.quote(ticker, "10", "2026-09-24T10:00:00+08:00")
        self.fill_buy(ticker)
        self.assertEqual(self.t.portfolio(record=True)["positions"][0]["sellable_qty"], 100)
        self.t.submit(ticker=ticker, side="sell", qty=100, request_id="t0-sell", limit_price="9")
        self.quote(ticker, "10", "2026-09-24T10:02:00+08:00")
        self.t.advance("2026-09-24T10:02:00+08:00")
        self.t.match()
        fees=performance(self.t)["fee_components"]
        self.assertEqual(fees["stamp_tax"], "0.00")
        self.assertEqual(fees["transfer"], "0.00")
        self.assertTrue(audit(self.t)["ok"], audit(self.t))

    def test_st_2026_limit_and_aggregate_quota(self):
        ticker="600001.SH"
        self.instrument(ticker, "SH_MAIN_STOCK")
        status=self.session(ticker, "2026-09-24", risk_warning=True)
        self.assertEqual(Decimal(status["limit_up"]), Decimal("11"))
        self.quote(ticker, "10", "2026-09-24T10:00:00+08:00")
        o=self.t.submit(ticker=ticker, side="buy", qty=500000, request_id="risk-max", limit_price="10")
        with self.assertRaisesRegex(ValueError,"quota"):
            self.t.submit(ticker=ticker, side="buy", qty=100, request_id="risk-over", limit_price="10")
        self.t.cancel(o["id"])
        self.t.submit(ticker=ticker, side="buy", qty=100, request_id="risk-after-cancel", limit_price="10")

    def test_missing_status_suspension_ipo_and_source(self):
        self.instrument("600002.SH", "SH_MAIN_STOCK")
        self.quote("600002.SH", "10", "2026-09-24T10:00:00+08:00")
        with self.assertRaisesRegex(ValueError,"missing"):
            self.t.submit(ticker="600002.SH", side="buy", qty=100, request_id="missing", limit_price="10")
        self.session("600002.SH", "2026-09-24", suspended=True)
        with self.assertRaisesRegex(ValueError,"suspended"):
            self.t.submit(ticker="600002.SH", side="buy", qty=100, request_id="halt", limit_price="10")
        self.instrument("688002.SH", "SH_STAR_STOCK")
        status=self.session("688002.SH", "2026-09-24", listing_session=5)
        self.assertIsNone(status["limit_up"])

    def test_minute_availability_prevents_close_lookahead(self):
        self.quote("600519.SH", "10.5", "2026-09-24T10:00:30+08:00", available_at="2026-09-24T02:05:30.000000+00:00")
        self.t.advance("2026-09-24T10:01:00+08:00")
        self.t.submit(ticker="600519.SH",side="buy",qty=100,request_id="observe",limit_price="11")
        with self.t._read() as db:
            q=self.t._quote(db,"600519.SH",self.t._now(self.t._account(db)),self.t._account(db))
        self.assertEqual(Decimal(q["price"]),Decimal("10"))

    def test_available_time_normalizes_timezone_offset(self):
        self.quote("600519.SH", "10.5", "2026-09-24T10:00:30+08:00", available_at="2026-09-23T23:05:30-03:00")
        self.t.advance("2026-09-24T10:01:00+08:00")
        with self.t._read() as db:
            q=self.t._quote(db,"600519.SH",self.t._now(self.t._account(db)),self.t._account(db))
        self.assertEqual(Decimal(q["price"]),Decimal("10"))

    def test_source_binding_and_future_metadata(self):
        with self.assertRaisesRegex(ValueError, "source"):
            self.t.register_instrument({"symbol":"600003.SH","profile":"SH_MAIN_STOCK","source":{"symbol":"WRONG","available_at":"2026-09-24T09:00:00+08:00","reference":"test"}})
        self.instrument("600003.SH","SH_MAIN_STOCK")
        self.session("600003.SH","2026-09-24",source={"symbol":"600003.SH","day":"2026-09-24","reference":"future test","available_at":"2026-10-01T00:00:00+08:00"})
        self.quote("600003.SH","10","2026-09-24T10:00:00+08:00")
        with self.assertRaisesRegex(ValueError,"not available"):
            self.t.submit(ticker="600003.SH",side="buy",qty=100,request_id="future-metadata",limit_price="11")

    def test_minimum_sell_fee_cannot_create_debt(self):
        self.t = PaperTrader.create(Path(self.temp.name)/"small.db",market="CN",calendar=CALENDAR,cash="6",fee_bps="0",slippage_bps="0",start="2026-09-24T10:00:00+08:00")
        self.instrument("600004.SH","SH_MAIN_STOCK")
        self.session("600004.SH","2026-09-24",reference_price="0.01",listing_session=1)
        self.quote("600004.SH","0.01","2026-09-24T10:00:00+08:00")
        self.t.submit(ticker="600004.SH",side="buy",qty=100,request_id="small-buy",limit_price="0.01")
        self.quote("600004.SH","0.01","2026-09-24T10:01:00+08:00")
        self.t.advance("2026-09-24T10:01:00+08:00")
        self.t.match()
        self.assertEqual(Decimal(self.t.portfolio()["cash"]),0)
        self.session("600004.SH","2026-09-28",reference_price="0.01",listing_session=2)
        self.t.advance("2026-09-28T10:00:00+08:00")
        self.quote("600004.SH","0.01","2026-09-28T10:00:00+08:00")
        self.t.submit(ticker="600004.SH",side="sell",qty=100,request_id="small-sell",limit_price="0.01")
        self.quote("600004.SH","0.01","2026-09-28T10:01:00+08:00")
        self.t.advance("2026-09-28T10:01:00+08:00")
        self.assertEqual(self.t.match()[0]["status"],"rejected")
        self.assertEqual(Decimal(self.t.portfolio()["cash"]),0)
        self.assertEqual(self.t.portfolio()["positions"][0]["qty"],100)
        self.assertTrue(audit(self.t)["ok"],audit(self.t))


if __name__ == "__main__":
    unittest.main()
