"""Read-only Financial Datasets hosted MCP, plus explicit offline capture adapters.

Only official HTTPS hosted API is contacted. Tokens never enter arguments or evidence.
No SDK or installation is needed; no broker endpoints exist in this module.
"""

import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .common import canonical, digest, number, symbol, timestamp, utcnow

MAX_RESPONSE = 16 * 1024 * 1024
TOOLS = {"get_stock_price", "get_stock_prices", "get_income_statement", "get_balance_sheet",
         "get_cash_flow_statement", "get_news", "get_financial_metrics"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward credentials to redirect destinations.
        return None


class FinancialDatasetsMCP:
    """Synchronous Streamable HTTP client for the official API-key endpoint.

    Hosted schemas are discovered, not assumed equivalent to the legacy stdio server.
    """

    endpoint = "https://mcp.financialdatasets.ai/api"

    def __init__(self):
        self._key = os.environ.get("FINANCIAL_DATASETS_API_KEY")
        if not self._key:
            raise ValueError("FINANCIAL_DATASETS_API_KEY is not set; configure it outside source/logs")
        self._session = None
        self._version = "2025-03-26"
        self._id = 0
        self._opener = urllib.request.build_opener(NoRedirect())
        self._catalog = None

    def _request(self, method, params, *, notification=False):
        self._id += 1
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            payload["id"] = self._id
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "User-Agent": "mercury-paper/0.1.0",
                   "X-API-KEY": self._key, "MCP-Protocol-Version": self._version}
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        request = urllib.request.Request(self.endpoint, canonical(payload).encode(), headers, method="POST")
        try:
            with self._opener.open(request, timeout=30) as response:
                self._session = response.headers.get("Mcp-Session-Id", self._session)
                content_type = response.headers.get("Content-Type", "")
                if "text/event-stream" in content_type:
                    # Stop at our response instead of waiting for a persistent SSE connection to close.
                    used, data, result = 0, [], None
                    while used <= MAX_RESPONSE:
                        line = response.readline(MAX_RESPONSE + 1)
                        used += len(line)
                        if used > MAX_RESPONSE:
                            raise ValueError("MCP response exceeds size limit")
                        if not line:
                            break
                        line = line.decode("utf-8").rstrip("\r\n")
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip())
                        elif not line and data:
                            event = json.loads("\n".join(data))
                            data = []
                            if isinstance(event, dict) and event.get("id") == payload.get("id"):
                                result = event
                                break
                    if result is None:
                        raise ValueError("MCP stream ended without matching response")
                else:
                    raw = response.read(MAX_RESPONSE + 1)
                    if len(raw) > MAX_RESPONSE:
                        raise ValueError("MCP response exceeds size limit")
                    if notification and not raw:
                        return None
                    result = json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise ValueError(f"Financial Datasets MCP HTTP {exc.code}; response body omitted") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ValueError("Financial Datasets MCP connection failed or timed out") from None
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError("MCP response is not valid UTF-8 JSON") from None
        if notification:
            return None
        if not isinstance(result, dict) or result.get("id") != payload["id"] or "error" in result:
            raise ValueError("MCP protocol error or response ID mismatch")
        if "result" not in result:
            raise ValueError("MCP response missing result")
        return result["result"]

    def tools(self):
        if self._catalog is None:
            init = self._request("initialize", {"protocolVersion": self._version, "capabilities": {},
                                                "clientInfo": {"name": "mercury-paper", "version": "0.1.0"}})
            version = init.get("protocolVersion")
            if version not in {"2024-11-05", "2025-03-26", "2025-06-18"}:
                raise ValueError("unsupported MCP protocol version")
            self._version = version
            self._request("notifications/initialized", {}, notification=True)
            catalog, cursor, seen = [], None, set()
            for _ in range(20):
                page = self._request("tools/list", {"cursor": cursor} if cursor else {})
                catalog.extend(page.get("tools", []))
                cursor = page.get("nextCursor")
                if not cursor:
                    break
                if cursor in seen:
                    raise ValueError("MCP tool pagination loop")
                seen.add(cursor)
            else:
                raise ValueError("MCP tool catalog exceeds page limit")
            self._catalog = catalog
        return self._catalog

    def call(self, name, arguments):
        if name not in TOOLS:
            raise ValueError("tool is outside the read-only market-data allowlist")
        definition = next((t for t in self.tools() if t.get("name") == name), None)
        if definition is None:
            raise ValueError("expected hosted MCP tool unavailable; inspect mcp-tools")
        schema = definition.get("inputSchema", {})
        required = set(schema.get("required", []))
        if not required.issubset(arguments) or not set(arguments).issubset(schema.get("properties", {})):
            raise ValueError("arguments do not match discovered MCP tool schema")
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise ValueError("Financial Datasets tool reported an error; body omitted")
        if name in {"get_stock_price", "get_stock_prices"}:
            # The hosted service can return billing errors as ordinary text without isError.
            # Reject these before they can be reported as a successful market capture.
            unwrap(result)
        return {"provider": "financial-datasets-mcp", "transport": "streamable-http",
                "tool": name, "arguments": arguments, "received_at": utcnow(), "result": result}


def unwrap(result):
    """Accept structured JSON or one JSON text content block; never invent missing fields."""
    if isinstance(result, dict) and result.get("isError"):
        raise ValueError("MCP error response")
    if isinstance(result, dict) and "structuredContent" in result:
        result = result["structuredContent"]
    elif isinstance(result, dict) and "content" in result:
        blocks = result["content"]
        if not isinstance(blocks, list) or len(blocks) != 1 or blocks[0].get("type") != "text":
            raise ValueError("expected one JSON text block or structuredContent")
        result = blocks[0].get("text")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            lowered = result.lower()
            if "credit" in lowered and any(word in lowered for word in ("balance", "insufficient", "exhausted", "required")):
                raise ValueError("Financial Datasets returned a credits/balance error; check the account dashboard; no market data received") from None
            raise ValueError("provider returned text instead of JSON data") from None
    return result


def snapshot(capture, ticker):
    ticker = symbol(ticker)
    if (capture.get("provider") != "financial-datasets-mcp" or
            capture.get("transport") != "streamable-http" or capture.get("tool") != "get_stock_price" or
            capture.get("arguments", {}).get("ticker") != ticker):
        raise ValueError("snapshot provenance envelope or requested ticker mismatch")
    data = unwrap(capture["result"])
    if isinstance(data, dict) and "snapshot" in data:
        data = data["snapshot"]
    if not isinstance(data, dict) or data.get("ticker") != ticker:
        raise ValueError("snapshot ticker missing or mismatched")
    asof = timestamp(data["time"])
    if data.get("time_milliseconds") is not None:
        epoch = datetime.fromtimestamp(float(number(data["time_milliseconds"])) / 1000, timezone.utc)
        if abs((datetime.fromisoformat(asof) - epoch).total_seconds()) > 1:
            raise ValueError("provider timestamps disagree")
    received = timestamp(capture["received_at"])
    if asof > received:
        raise ValueError("provider quote is in the future")
    currency = data.get("currency")
    if currency is None:
        contract = capture.get("instrument", {})
        if contract.get("ticker") != ticker or contract.get("currency_basis") != "explicit instrument contract":
            raise ValueError("provider omitted currency; explicit instrument currency contract required")
        currency = contract.get("currency")
    if currency != "USD":
        raise ValueError("only USD snapshots supported")
    return {"ticker": ticker, "price": str(number(data["price"], positive=True)), "asof": asof,
            "received_at": received, "currency": "USD", "evidence": capture,
            "source": "financial-datasets-mcp", "kind": "mcp"}


def collect_quote(trader, ticker, *, currency):
    if currency != "USD":
        raise ValueError("only verified USD instruments are supported")
    client = FinancialDatasetsMCP()
    capture = client.call("get_stock_price", {"ticker": symbol(ticker)})
    capture["instrument"] = {"ticker": ticker, "currency": currency, "currency_basis": "explicit instrument contract"}
    return trader._store_quote(**snapshot(capture, ticker))


def save_capture(path, capture):
    """Create-only evidence, no silently overwritten experimental observations."""
    payload = {"capture": capture, "sha256": digest(capture)}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(canonical(payload) + "\n")
    return {"path": str(path.resolve()), "sha256": payload["sha256"]}


def read_capture(path):
    if Path(path).stat().st_size > MAX_RESPONSE:
        raise ValueError("capture exceeds size limit")
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if digest(payload["capture"]) != payload["sha256"]:
        raise ValueError("capture SHA-256 mismatch")
    return payload["capture"]


def historical(capture, ticker):
    """Conservative daily EOD availability: next UTC midnight, never the day's open."""
    ticker = symbol(ticker)
    if capture.get("arguments", {}).get("ticker") != ticker:
        raise ValueError("historical capture request ticker mismatch")
    args = capture["arguments"]
    if args.get("interval", "day") != "day" or args.get("interval_multiplier", 1) != 1:
        raise ValueError("only one-day historical bars supported")
    data = unwrap(capture["result"])
    complete = False
    if isinstance(data, dict):
        if data.get("ticker", ticker) != ticker:
            raise ValueError("historical ticker mismatch")
        # A missing pagination field is unknown, not evidence of completeness.
        complete = "next_page_url" in data and data["next_page_url"] is None
        bars = data.get("prices")
    else:
        bars = data
    if not isinstance(bars, list) or not bars:
        raise ValueError("historical response has no price rows")
    rows, seen = [], set()
    for b in bars:
        raw_time = b["time"]
        if not isinstance(raw_time, str) or len(raw_time) != 10:
            raise ValueError("first version accepts daily YYYY-MM-DD EOD bars only")
        day = date.fromisoformat(raw_time)
        if (args.get("start_date") and day < date.fromisoformat(args["start_date"])) or (args.get("end_date") and day > date.fromisoformat(args["end_date"])):
            raise ValueError("historical row outside requested date range")
        at = datetime.combine(day + timedelta(days=1), datetime.min.time(), timezone.utc).isoformat()
        if raw_time in seen:
            raise ValueError("duplicate historical date")
        seen.add(raw_time)
        values = {k: number(b[k], positive=True) for k in ("open", "high", "low", "close")}
        volume = number(b["volume"])
        if volume < 0 or values["low"] > min(values["open"], values["close"]) or values["high"] < max(values["open"], values["close"]):
            raise ValueError("invalid OHLCV row")
        rows.append({"day": raw_time, "available_at": timestamp(at), "volume": str(volume),
                     **{k: str(v) for k, v in values.items()}})
    rows.sort(key=lambda r: r["day"])
    return {"ticker": ticker, "rows": rows, "pagination_complete": complete,
            "capture_sha256": digest(capture), "availability_rule": "next UTC midnight after US EOD"}


def load_history(trader, capture, ticker):
    data = historical(capture, ticker)
    ids = []
    with trader._tx() as db:
        if trader._account(db)["mode"] == "live":
            raise ValueError("historical captures cannot be imported into a live account")
        for row in data["rows"]:
            ids.append(trader._store_quote(ticker=ticker, price=row["close"], asof=row["available_at"],
                                          kind="historical", source="financial-datasets-capture", currency="USD",
                                          evidence={"capture": capture, "bar": row},
                                          received_at=capture["received_at"], _db=db)["quote_id"])
    return {"imported": len(ids), "pagination_complete": data["pagination_complete"],
            "warning": "historical simulation only; locally imported captures are not authenticated"}
