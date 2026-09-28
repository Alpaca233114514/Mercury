"""Bounded read-only Eastmoney stock/get bridge using official UI field semantics.

Independent of the pinned upstream and all SDKs. No order, wallet or funding APIs.
Only explicitly qualified Shanghai/Shenzhen codes are accepted; classification and
currency are provided by the registered local instrument contract, not inferred here.
"""

import base64
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal

ENDPOINT = "https://push2.eastmoney.com/api/qt/stock/get"
FIELDS = "f43,f57,f59,f86,f107,f600"
MAX_BODY = 1024 * 1024
TIMEOUT = 10
RULES_VERSION = "eastmoney-stock-get-ui-v1"
EXTENSION_VERSION = "eastmoney-timestamp-v1"
SOURCE = "eastmoney / Mercury timestamp extension"
SOURCE_REVIEW = "docs/ashare-timestamp-source.md"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_parameters(ticker):
    if not isinstance(ticker, str) or not re.fullmatch(r"[0-9]{6}\.(SH|SZ)", ticker):
        raise ValueError("explicit SH/SZ ticker required; other exchanges unsupported")
    market = 1 if ticker.endswith(".SH") else 0
    return {"secid": f"{market}.{ticker[:6]}", "fields": FIELDS, "fltt": "1", "invt": "2"}


def parse_quote(payload, ticker):
    """Validate identity/time/precision in the same response, never combine snapshots."""
    params = request_parameters(ticker)
    if not isinstance(payload, dict) or type(payload.get("rc")) is not int or payload["rc"] != 0:
        raise ValueError("provider response unsuccessful or malformed")
    row = payload.get("data")
    market = int(params["secid"].split(".")[0])
    if (not isinstance(row, dict) or row.get("f57") != ticker[:6] or
            type(row.get("f107")) is not int or row["f107"] != market):
        raise ValueError("provider code/market missing or mismatched")
    precision, raw_price, epoch = row.get("f59"), row.get("f43"), row.get("f86")
    if type(precision) is not int or not 0 <= precision <= 8:
        raise ValueError("provider price precision missing or unsupported")
    if type(raw_price) is not int or not 0 < raw_price <= 10**15:
        raise ValueError("provider price must be positive raw fixed-point integer")
    if type(epoch) is not int or epoch <= 0:
        raise ValueError("provider f86 must be positive Unix seconds")
    try:
        at = datetime.fromtimestamp(epoch, timezone.utc)
    except (ValueError, OSError, OverflowError):
        raise ValueError("provider timestamp out of range") from None
    price = Decimal(raw_price) / (Decimal(10) ** precision)
    result = {"code": ticker[:6], "symbol": ticker, "price": format(price, "f"),
              "time": at.isoformat(timespec="microseconds"),
              "provider_time_raw": epoch, "provider_time_field": "f86",
              "provider_time_basis": "official UI trading time, Unix seconds",
              "price_raw": raw_price, "price_precision": precision,
              "market_raw": market, "source": SOURCE, "rules_version": RULES_VERSION,
              "source_review": SOURCE_REVIEW, "cache_status": "unknown",
              "extension_version": EXTENSION_VERSION,
              "raw_response": payload,
              "source_request": {"url": ENDPOINT, "params": params}}
    # f600 is the official UI currency label. Missing/placeholder labels need the
    # local registered contract. Unknown explicit labels fail closed.
    if "f600" in row:
        result["provider_currency_raw"] = row["f600"]
        if row["f600"] not in (None, "", "-"):
            if not isinstance(row["f600"], str) or row["f600"] not in {"CNY", "人民币"}:
                raise ValueError("provider currency label not verified as CNY")
            result["currency"] = "CNY"
    return result


def parse_response(payload, ticker, received_at):
    row = parse_quote(payload, ticker)
    try:
        received = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise ValueError("invalid collection timestamp") from None
    if received.tzinfo is None:
        raise ValueError("collection timestamp requires timezone")
    if datetime.fromisoformat(row["time"]) > received:
        raise ValueError("provider timestamp is in the future")
    row["received_at"] = received.astimezone(timezone.utc).isoformat(timespec="microseconds")
    return row


def _error_evidence(ticker, category, params, received_at, *, raw=None, status=None):
    error = {"symbol": ticker, "error": category,
             "source_request": {"url": ENDPOINT, "params": params},
             "received_at": received_at}
    if status is not None:
        error["http_status"] = status
    if raw is not None:
        kept = raw[:MAX_BODY]
        error.update(raw_http_sha256=hashlib.sha256(kept).hexdigest(),
                     raw_http_body_truncated=len(raw) > MAX_BODY)
        try:
            error["raw_http_body"] = kept.decode("utf-8")
        except UnicodeError:
            error["raw_http_body_base64"] = base64.b64encode(kept).decode("ascii")
    return error


def fetch_quotes(symbols):
    """At most three requests, one attempt each, 10 s timeout and 1 MiB body cap.

    Any unavailable/malformed response makes the batch unsuccessful. Bounded received
    bodies are evidence even on validation failure; headers and exception text omitted.
    """
    if (not isinstance(symbols, list) or not 1 <= len(symbols) <= 3 or
            any(not isinstance(value, str) for value in symbols) or len(set(symbols)) != len(symbols)):
        return {"ok": False, "error": "invalid_symbols", "source": SOURCE, "data": []}
    try:
        requests = [(ticker, request_parameters(ticker)) for ticker in symbols]
    except ValueError:
        return {"ok": False, "error": "unsupported_exchange_or_ticker", "source": SOURCE, "data": []}
    opener = urllib.request.build_opener(NoRedirect())
    rows, errors = [], []
    for ticker, params in requests:
        url = ENDPOINT + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                       "User-Agent": "mercury-readonly/0.1"}, method="GET")
        received = None
        raw = None
        try:
            with opener.open(request, timeout=TIMEOUT) as response:
                raw = response.read(MAX_BODY + 1)
            received = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            if len(raw) > MAX_BODY:
                raise ValueError("body exceeds limit")
            text = raw.decode("utf-8")
            payload = json.loads(text)
            row = parse_response(payload, ticker, received)
            row.update(raw_http_body=text,
                       raw_http_sha256=hashlib.sha256(raw).hexdigest(),
                       request={"endpoint": ENDPOINT, "method": "GET", "parameters": params})
            rows.append(row)
        except urllib.error.HTTPError as exc:
            # HTTPError is a URLError subtype, but it owns a response body. Preserve
            # bounded evidence before the generic connection-error branch.
            category = "http_failure"
            try:
                with exc:
                    raw = exc.read(MAX_BODY + 1)
            except Exception:
                raw = None
                category = "http_failure_body_unavailable"
            received = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            errors.append(_error_evidence(ticker, category, params, received, raw=raw, status=exc.code))
        except (urllib.error.URLError, TimeoutError, OSError):
            errors.append({"symbol": ticker, "error": "connection_or_http_failure"})
        except (ValueError, UnicodeError, TypeError):
            errors.append(_error_evidence(ticker, "invalid_or_unverified_provider_response", params,
                                          received, raw=raw, status=200))
    return {"ok": not errors, "source": SOURCE, "data": rows, "errors": errors,
            "rules_version": RULES_VERSION, "extension_version": EXTENSION_VERSION,
            "scope": "explicit SH/SZ codes only",
            "warning": "provider trading timestamp is not an exchange execution guarantee; cache status unknown"}
