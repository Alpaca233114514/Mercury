import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from mercury import PaperTrader
from mercury.audit import audit
from mercury.common import canonical, digest, utcnow
from mercury.market import collect_quote, load_history


class Regressions(unittest.TestCase):
    def test_historical_batch_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            trader = PaperTrader.create(Path(directory) / "paper.db", start="2026-01-01T00:00:00Z")
            bars = [{"time": "2026-01-01", "open": "1", "close": "1", "high": "2", "low": "0.5", "volume": 1},
                    {"time": "2026-01-02", "open": "1", "close": "1.000000001", "high": "2", "low": "0.5", "volume": 1}]
            capture = {"arguments": {"ticker": "TEST"}, "received_at": utcnow(), "result": bars}
            with self.assertRaises(ValueError):
                load_history(trader, capture, "TEST")
            with trader._read() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0], 0)
            self.assertEqual(len(trader.history("events")), 1)

    def test_live_collection_uses_provider_price_not_trader_price(self):
        with tempfile.TemporaryDirectory() as directory:
            trader = PaperTrader.create(Path(directory) / "live.db", mode="live")
            now = datetime.now(timezone.utc)
            capture = {"provider": "financial-datasets-mcp", "transport": "streamable-http", "tool": "get_stock_price",
                       "arguments": {"ticker": "TEST"}, "received_at": utcnow(),
                       "result": {"structuredContent": {"ticker": "TEST", "price": 123,
                                  "time": (now - timedelta(seconds=1)).isoformat()}}}
            with patch("mercury.market.FinancialDatasetsMCP") as client:
                client.return_value.call.return_value = capture
                collect_quote(trader, "TEST", currency="USD")
            trader.submit(ticker="TEST", side="buy", qty=1, request_id="live-mocked")
            fill = trader.match()[0]
            self.assertEqual(fill["price"], "123.02460000")
            trader.hold("hold without changing exposure")
            self.assertTrue(audit(trader)["ok"])

    def test_snapshot_reconstruction_detects_changed_equity_even_if_rehashed(self):
        with tempfile.TemporaryDirectory() as directory:
            trader = PaperTrader.create(Path(directory) / "paper.db", start="2026-01-01T00:00:00Z")
            trader.portfolio(record=True)
            with trader._tx() as db:
                event = dict(db.execute("SELECT * FROM events WHERE kind='snapshot'").fetchone())
                payload = json.loads(event["payload"])
                payload["equity"] = "100001"
                entry = dict(time=event["time"], kind="snapshot", payload=payload, previous_hash=event["previous_hash"])
                db.execute("UPDATE snapshots SET payload=?", (canonical(payload),))
                db.execute("UPDATE events SET payload=?,hash=? WHERE seq=?", (canonical(payload), digest(entry), event["seq"]))
            result = audit(trader)
            self.assertFalse(result["ok"])
            self.assertTrue(any("equity differs" in e for e in result["errors"]))


if __name__ == "__main__":
    unittest.main()
