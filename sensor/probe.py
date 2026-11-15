"""MCP probe: initialize + tools/list against a configured server.

HTTP/SSE servers are probed automatically. stdio servers are spawned ONLY when
`probe_stdio` is enabled (and, if set, the server is on the allowlist), because
probing a stdio server means running its command on this machine.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from .parsers import RawServer

log = logging.getLogger("sensor.probe")


def stdio_allowed(server: RawServer, enabled: bool, allowlist: list[str]) -> bool:
    if server.transport != "stdio" or not enabled:
        return False
    return not allowlist or server.name in allowlist


async def _probe(server: RawServer, timeout: float) -> dict[str, Any]:
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    if server.transport == "http":
        target: Any = streamable_http_client(
            server.url, http_client=create_mcp_http_client(headers=server.headers or None)
        )
    elif server.transport == "sse":
        from mcp.client.sse import sse_client

        target = sse_client(server.url, headers=server.headers or None, timeout=timeout)
    else:
        from mcp import StdioServerParameters

        target = StdioServerParameters(
            command=server.command, args=server.args, env={**os.environ, **server.env} if server.env else None
        )

    async with asyncio.timeout(timeout):
        async with Client(target, read_timeout_seconds=timeout) as client:
            info = client.server_info
            tools = await client.list_tools()
            return {
                "server_info": {"name": getattr(info, "name", None), "version": getattr(info, "version", None)},
                "instructions": client.instructions,
                "tools": [
                    {
                        "name": t.name,
                        "description": (t.description or "")[:1000],
                        "input_schema": t.input_schema if hasattr(t, "input_schema") else getattr(t, "inputSchema", {}),
                    }
                    for t in tools.tools
                ],
            }


def probe(server: RawServer, timeout: float = 5.0) -> dict[str, Any]:
    """Blocking wrapper; always returns a dict (status ok|error)."""
    try:
        res = asyncio.run(_probe(server, timeout))
        return {"status": "ok", **res}
    except TimeoutError:
        return {"status": "error", "error": f"timeout after {timeout}s"}
    except BaseException as e:  # noqa: BLE001 - surface any probe failure as data
        if isinstance(e, BaseExceptionGroup):
            e = e.exceptions[0]
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:300]}"}
