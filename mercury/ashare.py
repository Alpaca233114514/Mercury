"""Read-only pinned A-share MCP bridge and conservative historical adapters.

The core remains standard-library only; the isolated helper owns the MCP SDK.
Captures preserve unsuccessful provider responses for evidence, never for fills.
"""

import json
import hashlib
import os
import re
import subprocess
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from .common import digest, number, timestamp, utcnow
from .market import unwrap
from .ashare_runtime import verify_runtime, ENV_ALLOWLIST, subprocess_env

QUOTE_TOOLS = {"get_realtime_quote", "mercury_get_realtime_quote"}
TOOLS = QUOTE_TOOLS | {"get_daily_kline", "get_minute_kline", "get_stock_info"}
COMMIT = "614b380606e18eaf35747a0c3b2f27ec551caaa3"
CN_TIME = timezone(timedelta(hours=8))
MAX_RESPONSE = 16 * 1024 * 1024


class AshareMCP:
    def __init__(self, config_path=None):
        root = Path(__file__).resolve().parent.parent
        path = Path(config_path) if config_path else root / "data/integrations/ashare-runtime.json"
        try:
            self.config = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raise ValueError("ashare MCP runtime configuration unavailable") from None
        if self.config.get("commit") != COMMIT:
            raise ValueError("ashare MCP runtime commit is not the reviewed pinned version")
        for field in ("python", "launcher"):
            configured = Path(self.config.get(field, ""))
            if not configured.is_absolute() or not configured.is_file():
                raise ValueError("ashare runtime requires absolute existing python/launcher paths")
        self.runtime_identity = verify_runtime(self.config)
        self.helper = root / "scripts/ashare_mcp_call.py"

    def _bridge(self, method, arguments):
        env = subprocess_env(os.environ)
        env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        request = {"launcher": self.config["launcher"], "method": method,
                   "arguments": arguments, "commit": COMMIT, "runtime": self.config}
        try:
            proc = subprocess.run([self.config["python"], "-I", str(self.helper)],
                                  input=json.dumps(request), capture_output=True, text=True,
                                  encoding="utf-8", env=env, timeout=100)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("ashare MCP bridge unavailable or timed out; no market capture") from None
        if proc.returncode or len(proc.stdout.encode("utf-8")) > MAX_RESPONSE:
            raise ValueError("ashare MCP bridge failed or response exceeds size limit")
        try:
            response = json.loads(proc.stdout)
        except ValueError:
            raise ValueError("ashare MCP bridge returned invalid JSON") from None
        if not isinstance(response, dict) or response.get("bridge_error"):
            raise ValueError("ashare MCP bridge protocol/transport failure; no market capture")
        if "result" not in response:
            raise ValueError("ashare MCP bridge response missing result")
        received = timestamp(response["received_at"])
        if received > utcnow():
            raise ValueError("ashare bridge collection time is in the future")
        return {"provider": "ashare-mcp", "transport": "stdio", "tool": method,
                "arguments": arguments, "received_at": received,
                "result": response["result"], "commit": COMMIT,
                "market": "CN", "adjustment": arguments.get("adjust", "raw"),
                "frequency": {"get_daily_kline": "day", "get_minute_kline": str(arguments.get("period", "5"))+"min", "get_realtime_quote": "snapshot", "mercury_get_realtime_quote": "snapshot"}.get(method, "metadata"),
                "rules_version": "eastmoney-stock-get-ui-v1" if method == "mercury_get_realtime_quote" else "ashare-availability-v1", "source_sha256": digest(response["result"]),
                "runtime_identity": self.runtime_identity}

    def tools(self):
        result = self._bridge("tools/list", {})["result"]
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            raise ValueError("ashare MCP tool catalog malformed")
        return result["tools"]

    def call(self, name, arguments):
        if name not in TOOLS or not isinstance(arguments, dict):
            raise ValueError("tool is outside the read-only ashare allowlist")
        if name in {"get_daily_kline", "get_minute_kline"} and arguments.get("adjust") != "":
            raise ValueError("explicit unadjusted prices (adjust='') required")
        return self._bridge(name, arguments)


def _ticker(ticker):
    if not isinstance(ticker, str) or not re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", ticker):
        raise ValueError("explicit exchange-qualified ticker required, e.g. 600519.SH")
    return ticker


def _matches(value, ticker):
    # Bare code is the upstream canonical form; the exchange is caller-supplied.
    return value in {ticker, ticker[:6]}


def _envelope(capture, ticker, tools):
    _ticker(ticker)
    if (not isinstance(capture, dict) or capture.get("provider") != "ashare-mcp" or
            capture.get("transport") != "stdio" or capture.get("commit") != COMMIT or
            capture.get("tool") not in tools):
        raise ValueError("ashare provenance envelope invalid")
    timestamp(capture.get("received_at"))
    args = capture.get("arguments")
    if not isinstance(args, dict):
        raise ValueError("ashare request arguments missing")
    if capture["tool"] in QUOTE_TOOLS:
        requested = args.get("symbols")
        if not isinstance(requested, list) or not any(_matches(v, ticker) for v in requested):
            raise ValueError("ashare requested ticker mismatch")
    elif not _matches(args.get("symbol"), ticker):
        raise ValueError("ashare requested ticker mismatch")
    data = unwrap(capture.get("result"))
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise ValueError("ashare provider reported unavailable/error data (ok is not true)")
    if not isinstance(data.get("source"), str) or not data["source"].strip():
        raise ValueError("ashare response source missing")
    if "currency" in data and data["currency"] != "CNY":
        raise ValueError("ashare top-level currency is not CNY")
    if "symbol" in data and not _matches(data["symbol"], ticker):
        raise ValueError("ashare returned ticker mismatch")
    return data


def snapshot(capture, ticker):
    data = _envelope(capture, ticker, QUOTE_TOOLS)
    if capture["tool"] == "mercury_get_realtime_quote":
        extension = capture.get("runtime_identity", {}).get("extension", {})
        if extension.get("version") != "eastmoney-timestamp-v1":
            raise ValueError("timestamped quote extension identity missing")
    rows = data.get("data")
    if not isinstance(rows, list):
        raise ValueError("ashare quote rows missing")
    matched = [r for r in rows if isinstance(r, dict) and _matches(r.get("code"), ticker)]
    if len(matched) != 1:
        raise ValueError("ashare quote ticker missing or duplicate")
    row = matched[0]
    if capture["tool"] == "mercury_get_realtime_quote":
        from .eastmoney import parse_response, SOURCE, EXTENSION_VERSION
        if data.get("source") != SOURCE or data.get("extension_version") != EXTENSION_VERSION:
            raise ValueError("timestamped quote source/version mismatch")
        body = row.get("raw_http_body")
        if not isinstance(body, str) or hashlib.sha256(body.encode("utf-8")).hexdigest() != row.get("raw_http_sha256"):
            raise ValueError("timestamped quote raw HTTP hash mismatch")
        try:
            raw = json.loads(body)
        except ValueError:
            raise ValueError("timestamped quote raw HTTP JSON invalid") from None
        if raw != row.get("raw_response"):
            raise ValueError("timestamped quote raw response mismatch")
        parsed = parse_response(raw, ticker, row.get("received_at"))
        if timestamp(parsed["received_at"]) > timestamp(capture["received_at"]):
            raise ValueError("HTTP receipt occurs after MCP collection")
        for field in set(parsed) | {"currency"}:
            if row.get(field) != parsed.get(field):
                raise ValueError("timestamped quote normalization disagrees with raw response")
        expected_request = {"endpoint": parsed["source_request"]["url"], "method": "GET",
                            "parameters": parsed["source_request"]["params"]}
        if row.get("request") != expected_request:
            raise ValueError("timestamped quote request metadata mismatch")
    if "symbol" in row and not _matches(row["symbol"], ticker):
        raise ValueError("ashare quote ticker mismatch")
    # Pinned upstream currently has no quote timestamp: reject, do not use capture time.
    raw_time = row.get("time")
    if not isinstance(raw_time, str) or len(raw_time) <= 10:
        raise ValueError("ashare quote lacks a complete provider timestamp; cannot trade")
    asof = timestamp(raw_time)
    received = timestamp(capture["received_at"])
    currency = row.get("currency", data.get("currency"))
    if currency is None:
        instrument = capture.get("instrument", {})
        if instrument.get("symbol") != ticker or instrument.get("currency_basis") != "registered CN instrument":
            raise ValueError("missing quote currency requires a registered CN instrument contract")
        currency = instrument.get("currency")
    if asof > received or currency != "CNY":
        raise ValueError("ashare quote future time or wrong currency")
    return {"ticker": ticker, "price": str(number(row.get("price"), positive=True)),
            "asof": asof, "received_at": received, "currency": "CNY", "evidence": capture,
            "source": "ashare-mcp", "kind": "mcp"}


def collect_quote(trader, ticker):
    with trader._read() as db:
        cn = trader._policy(db)
        if cn is None:
            raise ValueError("ashare live quote requires a separate CNY account")
        instrument = cn.instrument(db, ticker)
        from .cn import require_available
        require_available(instrument, trader._now(trader._account(db)))
    capture = AshareMCP().call("mercury_get_realtime_quote", {"symbols": [ticker]})
    capture["instrument"] = {"symbol": ticker, "currency": "CNY", "currency_basis": "registered CN instrument", "profile": instrument["profile"], "source": instrument["source"]}
    return trader._store_quote(**snapshot(capture, ticker))


def _local_time(value):
    if not isinstance(value, str) or len(value) <= 10:
        raise ValueError("minute bar requires full provider time")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid minute bar time") from None
    return dt.replace(tzinfo=CN_TIME) if dt.tzinfo is None else dt


def historical(capture, ticker):
    data = _envelope(capture, ticker, {"get_daily_kline", "get_minute_kline"})
    args = capture["arguments"]
    if args.get("adjust") != "" or ("adjust" in data and data["adjust"] != ""):
        raise ValueError("adjusted/missing adjustment historical prices rejected")
    if "symbol" not in data or not _matches(data["symbol"], ticker):
        raise ValueError("historical returned symbol missing or mismatched")
    daily = capture["tool"] == "get_daily_kline"
    period = args.get("period", "5")
    if not daily and (period not in {"1", "5", "15", "30", "60"} or str(data.get("period")) != period):
        raise ValueError("minute period missing or mismatched")
    bars = data.get("data")
    if not isinstance(bars, list) or not bars:
        raise ValueError("historical response has no price rows")
    rows, seen = [], set()
    for bar in bars:
        if not isinstance(bar, dict):
            raise ValueError("historical row is not an object")
        if "currency" in bar and bar["currency"] != "CNY":
            raise ValueError("historical row currency is not CNY")
        for key in ("code", "symbol"):
            if key in bar and not _matches(bar[key], ticker):
                raise ValueError("historical row ticker mismatch")
        if daily:
            raw_time = bar.get("date")
            if not isinstance(raw_time, str) or len(raw_time) != 10:
                raise ValueError("daily bar requires YYYY-MM-DD")
            day = date.fromisoformat(raw_time)
            dt = datetime.combine(day, time(15), CN_TIME)
            available = dt
            for key, lower in (("start_date", True), ("end_date", False)):
                bound = args.get(key)
                if bound:
                    limit = date.fromisoformat(bound) if "-" in bound else datetime.strptime(bound, "%Y%m%d").date()
                    if (lower and day < limit) or (not lower and day > limit):
                        raise ValueError("historical row outside requested date range")
        else:
            raw_time = bar.get("time")
            dt = _local_time(raw_time)
            available = dt + timedelta(minutes=int(period))
            for key, lower in (("start_date", True), ("end_date", False)):
                bound = args.get(key)
                if bound:
                    if len(bound) == 10:
                        bound += " 09:30:00" if lower else " 15:00:00"
                    limit = _local_time(bound)
                    if (lower and dt < limit) or (not lower and dt > limit):
                        raise ValueError("minute row outside requested range")
        asof = timestamp(dt.isoformat())
        if asof in seen or asof > timestamp(capture["received_at"]):
            raise ValueError("duplicate or future historical row")
        seen.add(asof)
        values = {key: number(bar.get(key), positive=True) for key in ("open", "high", "low", "close")}
        volume = number(bar.get("volume"))
        if (volume < 0 or values["low"] > min(values["open"], values["close"]) or
                values["high"] < max(values["open"], values["close"])):
            raise ValueError("invalid historical OHLCV")
        rows.append({"asof": asof, "available_at": timestamp(available.isoformat()),
                     "provider_time": raw_time, "volume": str(volume), "raw": bar,
                     "time_basis": "session-close" if daily else "provider-reported-time",
                     **{k: str(v) for k, v in values.items()}})
    rows.sort(key=lambda row: row["asof"])
    return {"ticker": ticker, "market": "CN", "provider": "ashare-mcp", "source": data["source"],
            "adjustment": "", "frequency": "day" if daily else period + "min",
            "rows": rows, "capture_sha256": digest(capture), "pagination_complete": False,
            "availability_rule": "session-close" if daily else "provider time plus period (bar-end unverified)",
            "rules_version": "ashare-availability-v1",
            "warning": "historical research only; coverage, calendar and security classification unverified"}


def load_history(trader, capture, ticker):
    normalized = historical(capture, ticker)
    ids = []
    with trader._tx() as db:
        account = trader._account(db)
        if account["mode"] not in {"demo", "replay"} or account["currency"] != "CNY":
            raise ValueError("ashare history requires isolated demo/replay CNY account")
        for row in normalized["rows"]:
            evidence = {"capture": capture, "bar": row, "available_at": row["available_at"],
                        "market": "CN", "provider": "ashare-mcp", "source": normalized["source"],
                        "adjustment": "", "frequency": normalized["frequency"],
                        "rules_version": normalized["rules_version"], "source_sha256": normalized["capture_sha256"]}
            ids.append(trader._store_quote(ticker=ticker, price=row["close"], asof=row["asof"],
                       received_at=capture["received_at"], currency="CNY", evidence=evidence,
                       source="ashare-capture", kind="historical", _db=db)["quote_id"])
    return {"imported": len(ids), "quote_ids": ids, "pagination_complete": False,
            "warning": normalized["warning"]}


def analyze_history(capture, ticker, asof):
    normalized = historical(capture, ticker)
    cutoff = timestamp(asof)
    rows = [r for r in normalized["rows"] if r["available_at"] <= cutoff]
    if not rows:
        raise ValueError("no historical rows available at requested analysis time")
    prices = [number(r["close"], positive=True) for r in rows]
    peak, drawdown = prices[0], number(0)
    for price in prices:
        peak = max(peak, price)
        drawdown = max(drawdown, (peak - price) / peak)
    return {"ticker": ticker, "asof": cutoff, "rows": len(rows), "source": normalized["source"],
            "adjustment": "", "frequency": normalized["frequency"],
            "first_asof": rows[0]["asof"], "last_asof": rows[-1]["asof"],
            "price_return": str(prices[-1] / prices[0] - 1), "max_drawdown": str(drawdown),
            "sma20": str(sum(prices[-20:]) / 20) if len(prices) >= 20 else None,
            "capture_sha256": normalized["capture_sha256"], "pagination_complete": False,
            "warning": "local price-only statistics; no costs, trading performance or full-coverage claim"}
