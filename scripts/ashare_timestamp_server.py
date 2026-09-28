"""Versioned local extension; register alongside the pinned upstream's tools."""

from ashare_mcp import server
from mercury_eastmoney_extension import fetch_quotes


@server.mcp.tool
def mercury_get_realtime_quote(symbols: list[str]) -> dict:
    """Read SH/SZ quotes with the provider's own trade timestamp and raw evidence.

    Mercury extension v1, not an upstream ashare-mcp tool. Explicit qualified
    symbols required; no orders, credentials, inferred timestamps or retries.
    """
    return fetch_quotes(symbols)


if __name__ == "__main__":
    server.main()
