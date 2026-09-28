import copy
import json
import os
import hashlib
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mercury.ashare import (AshareMCP, COMMIT, analyze_history, historical,
                            load_history, snapshot)


def capture(tool="get_daily_kline"):
    minute = tool == "get_minute_kline"
    args = {"symbol": "600519", "adjust": "", "start_date": "2026-09-24",
            "end_date": "2026-09-25"}
    row = {"code": "600519", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100}
    row["time" if minute else "date"] = "2026-09-24 14:55:00" if minute else "2026-09-24"
    payload = {"ok": True, "symbol": "600519", "source": "akshare", "data": [row]}
    if minute:
        args["period"] = "5"
        payload["period"] = "5"
    else:
        payload["adjust"] = ""
    return {"provider": "ashare-mcp", "transport": "stdio", "commit": COMMIT,
            "received_at": "2026-09-27T03:00:00+00:00", "tool": tool,
            "arguments": args, "result": {"structuredContent": payload, "isError": False}}


class HistoryTests(unittest.TestCase):
    def test_daily_close_time_source_and_raw(self):
        cap = capture()
        data = historical(cap, "600519.SH")
        row = data["rows"][0]
        self.assertEqual(row["asof"], "2026-09-24T07:00:00.000000+00:00")
        self.assertEqual(row["available_at"], row["asof"])
        self.assertEqual(row["time_basis"], "session-close")
        self.assertEqual(row["raw"], cap["result"]["structuredContent"]["data"][0])
        self.assertFalse(data["pagination_complete"])

    def test_minute_delays_availability(self):
        cap = capture("get_minute_kline")
        row = historical(cap, "600519.SH")["rows"][0]
        self.assertEqual(row["asof"], "2026-09-24T06:55:00.000000+00:00")
        self.assertEqual(row["available_at"], "2026-09-24T07:00:00.000000+00:00")
        with self.assertRaises(ValueError):
            analyze_history(cap, "600519.SH", "2026-09-24T06:59:59+00:00")
        self.assertEqual(analyze_history(cap, "600519.SH", "2026-09-24T07:00:00+00:00")["rows"], 1)

    def test_ok_false_even_when_mcp_iserror_false(self):
        cap = capture()
        cap["result"]["structuredContent"] = {"ok": False, "error": "data_source_unavailable"}
        with self.assertRaisesRegex(ValueError, "ok is not true"):
            historical(cap, "600519.SH")

    def test_exchange_qualified_ticker_required(self):
        with self.assertRaises(ValueError):
            historical(capture(), "600519")

    def test_mismatched_request_top_level_and_row(self):
        for target in ("request", "response", "row"):
            cap = capture()
            if target == "request":
                cap["arguments"]["symbol"] = "000001"
            elif target == "response":
                cap["result"]["structuredContent"]["symbol"] = "000001"
            else:
                cap["result"]["structuredContent"]["data"][0]["code"] = "000001"
            with self.subTest(target=target), self.assertRaises(ValueError):
                historical(cap, "600519.SH")

    def test_adjusted_or_missing_request_adjustment_rejected(self):
        for adjustment in (None, "qfq", "hfq"):
            cap = capture()
            cap["arguments"]["adjust"] = adjustment
            with self.subTest(adjustment=adjustment), self.assertRaises(ValueError):
                historical(cap, "600519.SH")
        cap = capture()
        cap["result"]["structuredContent"]["adjust"] = "qfq"
        with self.assertRaises(ValueError):
            historical(cap, "600519.SH")

    def test_bad_ohlcv_duplicates_future_and_range(self):
        for invalid in ("ohlc", "duplicate", "future", "range"):
            cap = capture()
            row = cap["result"]["structuredContent"]["data"][0]
            if invalid == "ohlc":
                row["low"] = 20
            elif invalid == "duplicate":
                cap["result"]["structuredContent"]["data"].append(copy.copy(row))
            elif invalid == "future":
                cap["received_at"] = "2026-09-23T00:00:00+00:00"
            else:
                cap["arguments"]["end_date"] = "2026-09-23"
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                historical(cap, "600519.SH")

    def test_analysis_cutoff_and_local_arithmetic(self):
        cap = capture()
        cap["arguments"]["start_date"] = "2026-09-23"
        cap["result"]["structuredContent"]["data"][0]["date"] = "2026-09-23"
        second = copy.copy(cap["result"]["structuredContent"]["data"][0])
        second.update(date="2026-09-24", open=8, close=8, high=9, low=7)
        cap["result"]["structuredContent"]["data"].append(second)
        data = analyze_history(cap, "600519.SH", "2026-09-24T07:00:00+00:00")
        self.assertEqual(data["price_return"], "-0.2")
        self.assertEqual(data["max_drawdown"], "0.2")
        from mercury.analytics import drawdown
        self.assertEqual(float(data["max_drawdown"]), drawdown([10, 8]))
        self.assertIsNone(data["sma20"])
        self.assertEqual(analyze_history(cap, "600519.SH", "2026-09-24T06:59:59+00:00")["rows"], 1)

    def test_json_text_response(self):
        cap = capture()
        payload = cap["result"]["structuredContent"]
        cap["result"] = {"content": [{"type": "text", "text": json.dumps(payload)}]}
        self.assertEqual(historical(cap, "600519.SH")["rows"][0]["close"], "10")


class SnapshotTests(unittest.TestCase):
    def quote(self):
        cap = capture()
        cap["tool"] = "get_realtime_quote"
        cap["arguments"] = {"symbols": ["600519"]}
        cap["result"]["structuredContent"]["data"] = [{"code": "600519", "price": 10}]
        return cap

    def test_missing_timestamp_cannot_use_collection_time(self):
        with self.assertRaisesRegex(ValueError, "provider timestamp"):
            snapshot(self.quote(), "600519.SH")

    def test_timestamp_extension_binds_normalization_to_raw_http(self):
        from mercury.eastmoney import parse_response, SOURCE, EXTENSION_VERSION
        from datetime import datetime
        cap = self.quote()
        cap["tool"] = "mercury_get_realtime_quote"
        cap["runtime_identity"] = {"extension": {"version": EXTENSION_VERSION}}
        epoch = int(datetime.fromisoformat("2026-09-24T10:00:00+08:00").timestamp())
        payload = {"rc": 0, "data": {"f57": "600519", "f107": 1, "f59": 2, "f43": 1000, "f86": epoch, "f600": "CNY"}}
        row = parse_response(payload, "600519.SH", "2026-09-24T02:00:01+00:00")
        row["request"] = {"endpoint": row["source_request"]["url"], "method": "GET", "parameters": row["source_request"]["params"]}
        body = json.dumps(payload)
        row.update(raw_http_body=body, raw_http_sha256=hashlib.sha256(body.encode()).hexdigest())
        cap["result"]["structuredContent"] = {"ok": True, "source": SOURCE, "extension_version": EXTENSION_VERSION, "data": [row]}
        self.assertEqual(snapshot(cap, "600519.SH")["price"], "10")
        for field, value in (("price", "999"), ("time", "2026-09-24T03:00:00+00:00"),
                             ("raw_http_sha256", "0" * 64), ("source_request", {}),
                             ("cache_status", "fresh"), ("provider_time_basis", "collected"),
                             ("source_review", "unverified"), ("request", {})):
            changed = copy.deepcopy(cap)
            changed["result"]["structuredContent"]["data"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                snapshot(changed, "600519.SH")
        del cap["runtime_identity"]
        with self.assertRaisesRegex(ValueError, "identity missing"):
            snapshot(cap, "600519.SH")

    def test_complete_provider_timestamp(self):
        cap = self.quote()
        cap["result"]["structuredContent"]["data"][0]["time"] = "2026-09-25T15:00:00+08:00"
        cap["result"]["structuredContent"]["data"][0]["currency"] = "CNY"
        result = snapshot(cap, "600519.SH")
        self.assertEqual(result["asof"], "2026-09-25T07:00:00.000000+00:00")
        self.assertEqual(result["currency"], "CNY")

    def test_currency_requires_provider_or_instrument_contract(self):
        cap = self.quote()
        cap["result"]["structuredContent"]["data"][0]["time"] = "2026-09-24T10:00:00+08:00"
        with self.assertRaisesRegex(ValueError, "currency"):
            snapshot(cap, "600519.SH")
        cap["instrument"] = {"symbol": "600519.SH", "currency": "CNY", "currency_basis": "registered CN instrument"}
        self.assertEqual(snapshot(cap, "600519.SH")["currency"], "CNY")
        cap["result"]["structuredContent"]["currency"] = "USD"
        with self.assertRaisesRegex(ValueError, "currency"):
            snapshot(cap, "600519.SH")

    def test_naive_date_only_future_currency_and_duplicate_rejected(self):
        for kind in ("naive", "date", "future", "currency", "duplicate"):
            cap = self.quote()
            row = cap["result"]["structuredContent"]["data"][0]
            row["time"] = "2026-09-25T15:00:00+08:00"
            if kind == "naive":
                row["time"] = "2026-09-25T15:00:00"
            elif kind == "date":
                row["time"] = "2026-09-25"
            elif kind == "future":
                row["time"] = "2026-09-28T15:00:00+08:00"
            elif kind == "currency":
                row["currency"] = "USD"
            else:
                cap["result"]["structuredContent"]["data"].append(copy.copy(row))
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                snapshot(cap, "600519.SH")


class FakeTrader:
    def __init__(self, mode="replay", currency="CNY", fail_at=None):
        self.account = {"mode": mode, "currency": currency}
        self.rows = []
        self.fail_at = fail_at

    @contextmanager
    def _tx(self):
        before = list(self.rows)
        try:
            yield self
        except Exception:
            self.rows = before
            raise

    def _account(self, db):
        return self.account

    def _store_quote(self, **values):
        if len(self.rows) == self.fail_at:
            raise ValueError("synthetic store failure")
        self.rows.append(values)
        return {"quote_id": str(len(self.rows))}


class ImportTests(unittest.TestCase):
    def test_import_has_availability_evidence_and_transaction(self):
        trader = FakeTrader()
        result = load_history(trader, capture("get_minute_kline"), "600519.SH")
        self.assertEqual(result["imported"], 1)
        stored = trader.rows[0]
        self.assertIs(stored["_db"], trader)
        self.assertGreater(stored["evidence"]["available_at"], stored["asof"])
        self.assertEqual(stored["currency"], "CNY")
        self.assertEqual(stored["kind"], "historical")

    def test_live_and_wrong_currency_rejected(self):
        for mode, currency in (("live", "CNY"), ("replay", "USD")):
            with self.assertRaises(ValueError):
                load_history(FakeTrader(mode, currency), capture(), "600519.SH")

    def test_whole_batch_transaction_rolls_back_on_store_failure(self):
        cap = capture()
        row = copy.copy(cap["result"]["structuredContent"]["data"][0])
        row["date"] = "2026-09-25"
        cap["result"]["structuredContent"]["data"].append(row)
        trader = FakeTrader(fail_at=1)
        with self.assertRaises(ValueError):
            load_history(trader, cap, "600519.SH")
        self.assertEqual(trader.rows, [])


class BridgeTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent
        python, launcher = root / "python.exe", root / "launch.py"
        config = root / "config.json"
        payload = json.dumps({"python": str(python), "launcher": str(launcher), "commit": COMMIT})
        with patch("mercury.ashare.Path.read_text", return_value=payload), \
                patch("mercury.ashare.Path.is_file", return_value=True), \
                patch("mercury.ashare.verify_runtime", return_value={"commit": COMMIT}):
            self.client = AshareMCP(config)

    def test_subprocess_env_no_credentials_and_failed_capture_preserved(self):
        response = {"received_at": "2026-09-27T03:00:00+00:00",
                    "result": {"isError": False, "structuredContent": {"ok": False}}}
        routing = {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
                   "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": "localhost,example.invalid"}
        with patch.dict(os.environ, {"FINANCIAL_DATASETS_API_KEY": "test-secret", "OTHER_TOKEN": "other", **routing}), \
                patch("mercury.ashare.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(response))) as run:
            cap = self.client.call("get_realtime_quote", {"symbols": ["600519"]})
        env = run.call_args.kwargs["env"]
        self.assertNotIn("FINANCIAL_DATASETS_API_KEY", env)
        self.assertNotIn("OTHER_TOKEN", env)
        for key, value in routing.items():
            self.assertEqual(env[key], value)
            self.assertNotIn(value, json.dumps(cap))
        self.assertFalse(cap["result"]["structuredContent"]["ok"])
        self.assertEqual(run.call_args.args[0][1], "-I")

    def test_disallowed_tool_and_implicit_adjustment_never_spawn(self):
        with patch("mercury.ashare.subprocess.run") as run:
            for name, args in (("place_order", {}), ("get_daily_kline", {"symbol": "600519"})):
                with self.assertRaises(ValueError):
                    self.client.call(name, args)
        run.assert_not_called()

    def test_authenticated_proxy_rejected_without_spawn_or_secret_echo(self):
        from mercury.ashare_runtime import subprocess_env
        for key in ("HTTP_PROXY", "https_proxy", "ALL_PROXY"):
            for value in ("http://audit_user:audit_password@localhost:9",
                          "audit_user%40example:audit_password@localhost:9",
                          "http://[audit_password"):
                with self.subTest(key=key, value="redacted"):
                    with self.assertRaises(ValueError) as error:
                        subprocess_env({key: value})
                    self.assertNotIn("audit_user", str(error.exception))
                    self.assertNotIn("audit_password", str(error.exception))
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://audit_user:audit_password@localhost:9"}), \
                patch("mercury.ashare.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "authenticated proxy URLs"):
                self.client.call("get_realtime_quote", {"symbols": ["600519"]})
        run.assert_not_called()


class RuntimeIdentityTests(unittest.TestCase):
    def test_modified_launcher_or_upstream_is_rejected(self):
        from mercury.ashare_runtime import verify_runtime
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "upstream/src/pkg/__init__.py"
            source.parent.mkdir(parents=True)
            source.write_text("# reviewed\n", encoding="utf-8")
            launcher = root / "launch.py"
            launcher.write_text("# reviewed launcher\n", encoding="utf-8")
            manifest = root / "source-manifest.json"
            manifest.write_text(json.dumps([{"path": "upstream/src/pkg/__init__.py", "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]), encoding="utf-8")
            (root / "commit.txt").write_text(COMMIT, encoding="utf-8")
            config = {"python": sys.executable, "launcher": str(launcher), "source_manifest": str(manifest), "commit": COMMIT,
                      "launcher_sha256": hashlib.sha256(launcher.read_bytes()).hexdigest(), "source_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()}
            verify_runtime(config)
            ext = root / "extensions/timestamp-v1"
            ext.mkdir(parents=True)
            files = []
            for name in ("ashare_timestamp_server.py", "mercury_eastmoney_extension.py"):
                p = ext / name
                p.write_text("# reviewed extension\n", encoding="utf-8")
                files.append({"path": p.relative_to(root).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
            config["extension"] = {"version": "eastmoney-timestamp-v1", "files": files}
            self.assertEqual(verify_runtime(config)["extension"], config["extension"])
            p.write_text("# changed extension\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "extension source changed"):
                verify_runtime(config)
            p.write_text("# reviewed extension\n", encoding="utf-8")
            unreviewed = ext / "extra.py"
            unreviewed.write_text("# unreviewed\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unreviewed Python"):
                verify_runtime(config)
            # Removing the extension declaration here only isolates the existing
            # upstream/launcher counterexamples; no runtime config is changed.
            del config["extension"]
            source.write_text("# changed\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source changed"):
                verify_runtime(config)
            launcher.write_text("# different launcher\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                verify_runtime(config)


if __name__ == "__main__":
    unittest.main()
