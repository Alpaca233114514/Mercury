"""Isolated SDK bridge. JSON on stdin/stdout; no credentials or exception bodies."""

import asyncio
import datetime
import json
import os
import pathlib
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from mercury.ashare_runtime import verify_runtime, subprocess_env

TOOLS = {"get_realtime_quote", "mercury_get_realtime_quote", "get_daily_kline", "get_minute_kline", "get_stock_info"}


async def perform(request):
    verify_runtime(request.get("runtime", {}))
    method = request.get("method")
    if method not in TOOLS | {"tools/list"} or not isinstance(request.get("arguments"), dict):
        raise ValueError("method/arguments rejected")
    launcher = pathlib.Path(request["launcher"])
    if launcher.resolve() != pathlib.Path(request["runtime"].get("launcher", "")).resolve():
        raise ValueError("launcher differs from verified runtime")
    if not launcher.is_absolute() or not launcher.is_file():
        raise ValueError("launcher unavailable")
    env = subprocess_env(os.environ)
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    params = StdioServerParameters(command=sys.executable, args=[str(launcher)],
                                  cwd=str(launcher.parent), env=env)
    # Server logging is untrusted and may include URLs: never forward it to evidence.
    with open(os.devnull, "w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as client:
                await asyncio.wait_for(client.initialize(), 30)
                catalog = await asyncio.wait_for(client.list_tools(), 30)
                if method == "tools/list":
                    result = catalog
                else:
                    definition = next((t for t in catalog.tools if t.name == method), None)
                    if definition is None:
                        raise ValueError("tool unavailable")
                    schema = definition.inputSchema
                    arguments = request["arguments"]
                    if (not set(schema.get("required", [])).issubset(arguments) or
                            not set(arguments).issubset(schema.get("properties", {}))):
                        raise ValueError("arguments outside discovered schema")
                    if method in {"get_daily_kline", "get_minute_kline"} and arguments.get("adjust") != "":
                        raise ValueError("unadjusted prices required")
                    result = await asyncio.wait_for(client.call_tool(method, arguments), 45)
                return {"result": result.model_dump(mode="json"),
                        "received_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}


def main():
    try:
        raw = sys.stdin.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("request oversized")
        response = asyncio.run(perform(json.loads(raw)))
    except Exception as exc:
        response = {"bridge_error": True, "error_type": type(exc).__name__,
                    "received_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    # -I deliberately ignores PYTHONIOENCODING; ASCII JSON is portable on Windows consoles.
    sys.stdout.write(json.dumps(response, ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()
