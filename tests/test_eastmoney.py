import copy
import hashlib
import http.client
import io
import json
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from mercury.eastmoney import (ENDPOINT, EXTENSION_VERSION, MAX_BODY, NoRedirect,
                              fetch_quotes, parse_quote, parse_response, request_parameters)


def payload(code="600519", market=1, price=123700, precision=2):
    return {"rc": 0, "data": {"f57": code, "f107": market, "f43": price,
            "f59": precision, "f86": int(datetime(2026, 9, 24, 7, tzinfo=timezone.utc).timestamp())}}


class ParseTests(unittest.TestCase):
    def test_same_response_price_time_identity(self):
        raw = payload()
        row = parse_quote(raw, "600519.SH")
        self.assertEqual(row["price"], "1237")
        self.assertEqual(row["time"], "2026-09-24T07:00:00.000000+00:00")
        self.assertEqual(row["symbol"], "600519.SH")
        self.assertIs(row["raw_response"], raw)
        self.assertEqual(row["source_request"]["params"]["secid"], "1.600519")
        self.assertEqual(row["extension_version"], EXTENSION_VERSION)
        self.assertNotIn("currency", row)

    def test_precision_is_not_universal_hundred(self):
        self.assertEqual(parse_quote(payload(price=1536, precision=3), "600519.SH")["price"], "1.536")
        self.assertEqual(parse_quote(payload(price=17, precision=0), "600519.SH")["price"], "17")

    def test_sz_market_and_qualified_request(self):
        self.assertEqual(request_parameters("000001.SZ")["secid"], "0.000001")
        self.assertEqual(parse_quote(payload("000001", 0), "000001.SZ")["code"], "000001")
        for ticker in ("600519", "sh600519", "600519.BJ", "../600519.SH", None):
            with self.subTest(ticker=ticker), self.assertRaises(ValueError):
                request_parameters(ticker)

    def test_missing_code_and_market_or_mismatch(self):
        for key, value in (("f57", "000001"), ("f57", None), ("f107", 0),
                           ("f107", "1"), ("f107", True), ("f107", None)):
            raw = payload()
            raw["data"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                parse_quote(raw, "600519.SH")

    def test_price_precision_and_timestamp_types(self):
        for key, value in (("f43", True), ("f43", -1), ("f43", 1.5), ("f43", "123700"),
                           ("f59", True), ("f59", -1), ("f59", 9), ("f59", None),
                           ("f86", None), ("f86", "202609241500"), ("f86", True), ("f86", -1),
                           ("f86", 10**50)):
            raw = payload()
            raw["data"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                parse_quote(raw, "600519.SH")

    def test_no_data_unsuccessful_and_bad_envelope(self):
        for raw in (None, [], {}, {"rc": True, "data": payload()["data"]},
                    {"rc": 1, "data": payload()["data"]}, {"rc": 0, "data": None}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_quote(raw, "600519.SH")

    def test_future_provider_time_and_naive_collection_rejected(self):
        for received in ("2026-09-24T06:59:59+00:00", "2026-09-24T07:00:01", "broken", None):
            with self.subTest(received=received), self.assertRaises(ValueError):
                parse_response(payload(), "600519.SH", received)
        self.assertEqual(parse_response(payload(), "600519.SH", "2026-09-24T15:00:01+08:00")["received_at"],
                         "2026-09-24T07:00:01.000000+00:00")

    def test_currency_explicit_unknown_rejected_never_defaulted(self):
        for value in ("USD", "美元", 0, {}, []):
            raw = payload()
            raw["data"]["f600"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_quote(raw, "600519.SH")
        for value in (None, "", "-"):
            raw = payload()
            raw["data"]["f600"] = value
            self.assertNotIn("currency", parse_quote(raw, "600519.SH"))
        for value in ("CNY", "人民币"):
            raw = payload()
            raw["data"]["f600"] = value
            self.assertEqual(parse_quote(raw, "600519.SH")["currency"], "CNY")


class FetchTests(unittest.TestCase):
    def response(self, raw):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = raw
        return response

    def test_real_bytes_preserved_hashed_single_bounded_request(self):
        raw = json.dumps(payload(), separators=(",", ":")).encode("utf-8")
        opener = MagicMock()
        response = self.response(raw)
        opener.open.return_value = response
        with patch("mercury.eastmoney.urllib.request.build_opener", return_value=opener):
            result = fetch_quotes(["600519.SH"])
        self.assertTrue(result["ok"])
        row = result["data"][0]
        self.assertEqual(row["raw_http_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(row["raw_http_body"], raw.decode())
        self.assertEqual(row["raw_response"], payload())
        self.assertEqual(row["source_request"]["url"], ENDPOINT)
        response.read.assert_called_once_with(MAX_BODY + 1)
        opener.open.assert_called_once()
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 10)
        request = opener.open.call_args.args[0]
        self.assertIn("fltt=1", request.full_url)
        self.assertEqual(request.get_method(), "GET")
        self.assertNotIn("Authorization", request.headers)

    def test_invalid_lists_never_call_network(self):
        with patch("mercury.eastmoney.urllib.request.build_opener") as build:
            for symbols in ([], ["600519.SH"] * 2, ["600519.SH"] * 4, "600519.SH",
                            ["600519.BJ"], [None], ["600519"]):
                with self.subTest(symbols=symbols):
                    self.assertFalse(fetch_quotes(symbols)["ok"])
        build.assert_not_called()

    def test_fixed_error_does_not_leak_proxy_or_credentials(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError("https://user:secret@example.invalid proxy failure")
        with patch("mercury.eastmoney.urllib.request.build_opener", return_value=opener):
            result = fetch_quotes(["600519.SH"])
        self.assertFalse(result["ok"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("example.invalid", json.dumps(result))
        opener.open.assert_called_once()

    def test_http403_retains_status_bounded_body_hash_request_receipt(self):
        raw = b'{"error":"denied"}'
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError(ENDPOINT, 403, "secret exception",
                                                        {"Secret-Header": "secret-header"}, io.BytesIO(raw))
        with patch("mercury.eastmoney.urllib.request.build_opener", return_value=opener):
            result = fetch_quotes(["600519.SH"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["data"], [])
        error = result["errors"][0]
        self.assertEqual(error["http_status"], 403)
        self.assertEqual(error["raw_http_body"], raw.decode())
        self.assertEqual(error["raw_http_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertFalse(error["raw_http_body_truncated"])
        self.assertEqual(error["source_request"]["params"], request_parameters("600519.SH"))
        self.assertIsNotNone(datetime.fromisoformat(error["received_at"]).tzinfo)
        self.assertNotIn("secret", json.dumps(result))
        opener.open.assert_called_once()

    def test_oversize_http_error_keeps_prefix_hash_and_no_retry(self):
        raw = b"x" * (MAX_BODY + 9)
        class TrackingBody(io.BytesIO):
            calls = []

            def read(self, size=-1):
                self.calls.append(size)
                return super().read(size)

        body = TrackingBody(raw)
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError(ENDPOINT, 503, "unavailable", {}, body)
        with patch("mercury.eastmoney.urllib.request.build_opener", return_value=opener):
            result = fetch_quotes(["600519.SH"])
        error = result["errors"][0]
        self.assertFalse(result["ok"])
        self.assertEqual(error["http_status"], 503)
        self.assertTrue(error["raw_http_body_truncated"])
        self.assertEqual(error["raw_http_body"], raw[:MAX_BODY].decode())
        self.assertEqual(error["raw_http_sha256"], hashlib.sha256(raw[:MAX_BODY]).hexdigest())
        self.assertEqual(body.calls, [MAX_BODY + 1])
        opener.open.assert_called_once()

    def test_http_error_body_read_failure_still_has_status_request_receipt(self):
        body = MagicMock()
        body.read.side_effect = http.client.IncompleteRead(b"secret partial body", 12)
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError(ENDPOINT, 403, "secret exception", {}, body)
        with patch("mercury.eastmoney.urllib.request.build_opener", return_value=opener):
            result = fetch_quotes(["600519.SH"])
        error = result["errors"][0]
        self.assertFalse(result["ok"])
        self.assertEqual(error["http_status"], 403)
        self.assertEqual(error["error"], "http_failure_body_unavailable")
        self.assertNotIn("raw_http_body", error)
        self.assertNotIn("raw_http_sha256", error)
        self.assertEqual(error["source_request"]["params"], request_parameters("600519.SH"))
        self.assertIsNotNone(datetime.fromisoformat(error["received_at"]).tzinfo)
        self.assertNotIn("secret", json.dumps(result))

    def test_oversize_invalid_json_and_mismatch_fail_closed(self):
        mismatch = payload("000001", 0)
        for raw in (b"x" * (MAX_BODY + 1), b"not JSON", b"\xff", json.dumps(mismatch).encode()):
            opener = MagicMock()
            opener.open.return_value = self.response(raw)
            with self.subTest(raw_length=len(raw)), \
                    patch("mercury.eastmoney.urllib.request.build_opener", return_value=opener):
                result = fetch_quotes(["600519.SH"])
            self.assertFalse(result["ok"])
            self.assertEqual(result["data"], [])
            error = result["errors"][0]
            self.assertEqual(error["raw_http_sha256"], hashlib.sha256(raw[:MAX_BODY]).hexdigest())
            self.assertEqual(error["raw_http_body_truncated"], len(raw) > MAX_BODY)
            if raw == b"\xff":
                self.assertEqual(error["raw_http_body_base64"], "/w==")
            else:
                self.assertEqual(error["raw_http_body"], raw[:MAX_BODY].decode("utf-8"))

    def test_business_currency_failure_retains_body_but_no_valid_row(self):
        data = payload()
        data["data"]["f600"] = "美元"
        raw = json.dumps(data).encode()
        opener = MagicMock()
        opener.open.return_value = self.response(raw)
        with patch("mercury.eastmoney.urllib.request.build_opener", return_value=opener):
            result = fetch_quotes(["600519.SH"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["data"], [])
        self.assertEqual(result["errors"][0]["raw_http_body"], raw.decode())

    def test_independent_timestamps_and_partial_batch_not_success(self):
        first = payload()
        second = payload("000001", 0)
        second["data"]["f86"] -= 120
        opener = MagicMock()
        opener.open.side_effect = [self.response(json.dumps(first).encode()), self.response(json.dumps(second).encode())]
        with patch("mercury.eastmoney.urllib.request.build_opener", return_value=opener):
            result = fetch_quotes(["600519.SH", "000001.SZ"])
        self.assertTrue(result["ok"])
        self.assertNotEqual(result["data"][0]["time"], result["data"][1]["time"])
        opener.open.side_effect = [self.response(json.dumps(first).encode()), TimeoutError("secret")]
        with patch("mercury.eastmoney.urllib.request.build_opener", return_value=opener):
            result = fetch_quotes(["600519.SH", "000001.SZ"])
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["data"]), 1)
        self.assertNotIn("secret", json.dumps(result))

    def test_redirect_refused(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.invalid"))


if __name__ == "__main__":
    unittest.main()
