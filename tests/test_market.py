import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mercury.analytics import market_report
from mercury.common import digest
from mercury.market import FinancialDatasetsMCP, historical, read_capture, save_capture, snapshot, unwrap


def capture(data):
    return {"provider": "financial-datasets-mcp", "transport": "streamable-http", "tool": "get_stock_price", "arguments": {"ticker": "TEST"},
            "received_at": "2026-01-10T16:00:00Z", "result": {"content": [{"type": "text", "text": json.dumps(data)}]}}


def bars():
    return [{"time": f"2026-01-{day:02d}", "open": price, "high": price + 1, "low": price - 1,
             "close": price, "volume": 100} for day, price in [(5, 100), (6, 110), (7, 99)]]


class MarketTests(unittest.TestCase):
    def test_snapshot_real_schema_and_bad_time(self):
        c = capture({"ticker": "TEST", "price": 100.2, "time": "2026-01-10T15:59:00Z", "currency": "USD"})
        self.assertEqual(snapshot(c, "TEST")["price"], "100.2")
        for data in [{"ticker": "OTHER", "price": 100, "time": "2026-01-10T15:59:00Z"},
                     {"ticker": "TEST", "price": 100, "time": "2026-01-10"},
                     {"ticker": "TEST", "price": 100, "time": "2027-01-10T15:59:00Z"},
                     {"ticker": "TEST", "price": 100, "time": "2026-01-10T15:59:00Z", "time_milliseconds": 1}]:
            with self.assertRaises(ValueError):
                snapshot(capture(data), "TEST")

    def test_currency_and_ticker_contracts(self):
        c = capture({"ticker": "TEST", "price": 100, "time": "2026-01-10T15:59:00Z"})
        with self.assertRaisesRegex(ValueError, "currency"):
            snapshot(c, "TEST")
        c["instrument"] = {"ticker": "TEST", "currency": "USD", "currency_basis": "explicit instrument contract"}
        self.assertEqual(snapshot(c, "TEST")["currency"], "USD")
        with self.assertRaisesRegex(ValueError, "ticker"):
            historical(capture(bars()), "OTHER")

    def test_error_and_structured_data(self):
        with self.assertRaises(ValueError):
            unwrap({"isError": True, "content": []})
        with self.assertRaises(ValueError):
            unwrap("Unable to fetch current price")
        self.assertEqual(unwrap({"structuredContent": {"price": 100}}), {"price": 100})

    def test_history_asof_pagination_and_metrics(self):
        c = capture({"ticker": "TEST", "prices": bars(), "next_page_url": None})
        parsed = historical(c, "TEST")
        self.assertTrue(parsed["pagination_complete"])
        self.assertEqual(parsed["rows"][0]["available_at"], "2026-01-06T00:00:00.000000+00:00")
        result = market_report(c, "TEST", asof="2026-01-07T00:00:00Z")
        self.assertEqual(result["rows"], 2)
        self.assertAlmostEqual(result["price_return"], 0.1)
        result = market_report(c, "TEST", asof="2026-01-08T00:00:00Z")
        self.assertAlmostEqual(result["max_drawdown"], 0.1)
        self.assertFalse(historical(capture(bars()), "TEST")["pagination_complete"])

    def test_duplicate_invalid_history(self):
        for rows in [bars() + [bars()[0]], [{**bars()[0], "high": 1}], [{**bars()[0], "volume": -1}]]:
            with self.assertRaises(ValueError):
                historical(capture(rows), "TEST")

    def test_capture_hash_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "capture.json"
            c = capture(bars())
            save_capture(path, c)
            self.assertEqual(read_capture(path), c)
            with self.assertRaises(FileExistsError):
                save_capture(path, c)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["capture"]["tool"] = "changed"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                read_capture(path)

    def test_missing_key_and_readonly_allowlist(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "not set"):
                FinancialDatasetsMCP()
        with patch.dict(os.environ, {"FINANCIAL_DATASETS_API_KEY": "test-only-value"}):
            client = FinancialDatasetsMCP()
            with self.assertRaisesRegex(ValueError, "allowlist"):
                client.call("place_order", {})

    def test_mcp_handshake_schema_and_capture(self):
        with patch.dict(os.environ, {"FINANCIAL_DATASETS_API_KEY": "test-only-value"}):
            client = FinancialDatasetsMCP()
            responses = [{"protocolVersion": "2025-03-26"}, None,
                         {"tools": [{"name": "get_stock_price", "inputSchema": {
                             "required": ["ticker"], "properties": {"ticker": {"type": "string"}}}}]},
                         {"content": [{"type": "text", "text": '{"ticker":"TEST","price":100}'}]}]
            with patch.object(client, "_request", side_effect=responses) as req:
                result = client.call("get_stock_price", {"ticker": "TEST"})
            self.assertEqual(req.call_args_list[0].args[0], "initialize")
            self.assertEqual(req.call_args_list[1].args[0], "notifications/initialized")
            self.assertEqual(result["transport"], "streamable-http")
            self.assertNotIn("test-only-value", json.dumps(result))
            with self.assertRaisesRegex(ValueError, "schema"):
                client.call("get_stock_price", {"unknown": "TEST"})

    def test_json_and_sse_transport(self):
        class Response(io.BytesIO):
            def __init__(self, data, content_type):
                super().__init__(data)
                self.headers = {"Content-Type": content_type}
        with patch.dict(os.environ, {"FINANCIAL_DATASETS_API_KEY": "test-only-value"}):
            for data, kind in [(b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}', "application/json"),
                               (b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n', "text/event-stream")]:
                client = FinancialDatasetsMCP()
                with patch.object(client._opener, "open", return_value=Response(data, kind)) as opened:
                    self.assertEqual(client._request("tools/list", {}), {"ok": True})
                    self.assertEqual(opened.call_args.args[0].get_header("User-agent"), "mercury-paper/0.1.0")

    def test_billing_text_without_error_flag_is_not_a_successful_capture(self):
        with patch.dict(os.environ, {"FINANCIAL_DATASETS_API_KEY": "test-only-value"}):
            for name in ("get_stock_price", "get_stock_prices"):
                client = FinancialDatasetsMCP()
                client._catalog = [{"name": name, "inputSchema": {"required": ["ticker"], "properties": {"ticker": {"type": "string"}}}}]
                result = {"content": [{"type": "text", "text": "Example billing error: check credits balance. Private provider detail."}]}
                with patch.object(client, "_request", return_value=result):
                    with self.assertRaisesRegex(ValueError, "credits/balance") as raised:
                        client.call(name, {"ticker": "TEST"})
                self.assertNotIn("Private provider detail", str(raised.exception))
                self.assertNotIn("test-only-value", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
