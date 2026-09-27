import concurrent.futures
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from mercury import PaperTrader
from mercury.analytics import performance
from mercury.audit import audit


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "paper.db"
        self.t = PaperTrader.create(self.path, start="2026-01-05T15:00:00Z", cash="10000")
        self.quote("100", "15:00:00")

    def quote(self, price, clock, ticker="TEST"):
        return self.t.import_quote(ticker=ticker, price=price, asof=f"2026-01-05T{clock}Z", evidence={"test": True})

    def advance(self, price="100", clock="15:01:00"):
        self.quote(price, clock)
        self.t.advance(f"2026-01-05T{clock}Z")

    def buy(self, qty=10, key="buy", **kwargs):
        return self.t.submit(ticker="TEST", side="buy", qty=qty, request_id=key, **kwargs)

    def test_fees_slippage_cost_pnl_and_restart(self):
        self.buy()
        self.assertEqual(self.t.match()[0]["reason"], "waiting for next quote")
        self.advance()
        fill = self.t.match()[0]
        self.assertEqual(Decimal(fill["price"]), Decimal("100.02"))
        self.assertEqual(Decimal(fill["fee"]), Decimal("0.5001"))
        self.assertEqual(Decimal(self.t.portfolio()["cash"]), Decimal("8999.2999"))
        self.t = PaperTrader(self.path)
        self.t.submit(ticker="TEST", side="sell", qty=4, request_id="sell")
        self.advance("110", "15:02:00")
        fill = self.t.match()[0]
        self.assertEqual(Decimal(fill["price"]), Decimal("109.978"))
        self.assertEqual(Decimal(fill["realized_pnl"]), Decimal("39.41200400"))
        p = self.t.portfolio(record=True)
        self.assertEqual(p["positions"][0]["qty"], 6)
        self.assertEqual(Decimal(p["positions"][0]["cost"]), Decimal("600.42006"))
        self.assertEqual(Decimal(p["equity"]) - Decimal(p["initial_cash"]), Decimal(p["realized_pnl"]) + Decimal(p["unrealized_pnl"]))
        self.assertTrue(audit(self.t)["ok"])

    def test_reservations_cancel_and_no_short(self):
        o = self.buy(qty=90)
        with self.assertRaisesRegex(ValueError, "cash"):
            self.buy(qty=20, key="over")
        self.t.cancel(o["id"])
        self.t.cancel(o["id"])
        self.assertEqual(Decimal(self.t.portfolio()["available_cash"]), Decimal("10000"))
        self.buy(qty=10, key="next")
        self.advance()
        self.t.match()
        self.t.submit(ticker="TEST", side="sell", qty=8, request_id="sell1")
        with self.assertRaisesRegex(ValueError, "position"):
            self.t.submit(ticker="TEST", side="sell", qty=3, request_id="sell2")
        self.assertTrue(audit(self.t)["ok"])

    def test_limit_and_cancel_filled(self):
        o = self.buy(limit_price="100")
        self.advance("100")
        self.assertEqual(self.t.match()[0]["status"], "open")
        self.advance("99", "15:02:00")
        self.assertIn("price", self.t.match()[0])
        with self.assertRaises(ValueError):
            self.t.cancel(o["id"])
        self.assertEqual(self.t.match(), [])

    def test_price_gap_rejects_without_negative_cash(self):
        self.buy(qty=99)
        self.advance("200")
        self.assertEqual(self.t.match()[0]["status"], "rejected")
        self.assertEqual(Decimal(self.t.portfolio()["cash"]), Decimal("10000"))
        self.assertTrue(audit(self.t)["ok"])

    def test_stale_future_and_missing_marks(self):
        self.buy()
        self.advance()
        self.t.match()
        self.t.advance("2026-01-05T16:00:00Z")
        self.quote("1000", "17:00:00")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.buy(key="later")
        p = self.t.portfolio(record=True)
        self.assertIsNone(p["equity"])
        self.assertEqual(p["missing_marks"], ["TEST"])
        self.assertIsNone(performance(self.t)["observed_max_drawdown"])
        with self.assertRaises(ValueError):
            self.t.advance("2026-01-05T15:00:00Z")

    def test_idempotence_and_conflicting_retries(self):
        first = self.buy()
        self.assertEqual(first, self.buy())
        with self.assertRaises(ValueError):
            self.buy(qty=11)
        self.advance()
        self.t.match()
        self.assertEqual(self.buy()["status"], "filled")
        self.assertEqual(len(self.t.history("fills")), 1)

    def test_concurrent_requests_and_match(self):
        def submit(_):
            return PaperTrader(self.path).submit(ticker="TEST", side="buy", qty=10, request_id="concurrent")
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            orders = list(pool.map(submit, range(16)))
        self.assertEqual(len({o["id"] for o in orders}), 1)
        self.advance()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: PaperTrader(self.path).match(), range(16)))
        self.assertEqual(len(self.t.history("fills")), 1)
        self.assertTrue(audit(self.t)["ok"])

    def test_bad_values_and_existing_file(self):
        for price in ("NaN", "Infinity", "-1", "0", True, "0.000000001"):
            with self.subTest(price=price), self.assertRaises(ValueError):
                self.quote(price, "15:00:00")
        for qty in (0, -1, True, 1.2):
            with self.assertRaises(ValueError):
                self.buy(qty=qty)
        with self.assertRaises(FileExistsError):
            PaperTrader.create(self.path, start="2026-01-05T15:00:00Z")
        with self.assertRaises(ValueError):
            self.t.import_quote(ticker="TEST", price="1", asof="2026-01-05", evidence={"test": True})

    def test_live_rejects_fixture_and_user_clock(self):
        live = PaperTrader.create(Path(self.temp.name) / "live.db", mode="live")
        with self.assertRaises(ValueError):
            live.import_quote(ticker="TEST", price="100", asof="2026-01-05T15:00:00Z", evidence={"test": True})
        with self.assertRaises(ValueError):
            live.advance("2027-01-05T15:00:00Z")
        with self.assertRaisesRegex(ValueError, "provenance"):
            live.import_quote(ticker="TEST", price="100", asof="2026-01-05T15:00:00Z", evidence={"test": True},
                              kind="mcp", source="financial-datasets-mcp")

    def test_audit_detects_tampering(self):
        self.buy()
        self.advance()
        self.t.match()
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute("UPDATE account SET cash='999999'")
                db.execute("UPDATE quotes SET price='1'")
        result = audit(self.t)
        self.assertFalse(result["ok"])
        self.assertTrue(any("hash" in e for e in result["errors"]))
        self.assertIn("cash differs from replayed fills", result["errors"])

    def test_full_exit_has_zero_cost(self):
        self.buy(qty=3)
        self.advance()
        self.t.match()
        self.t.submit(ticker="TEST", side="sell", qty=3, request_id="exit")
        self.advance("105", "15:02:00")
        self.t.match()
        self.assertEqual(self.t.portfolio()["positions"], [])
        self.assertTrue(audit(self.t)["ok"])


if __name__ == "__main__":
    unittest.main()
