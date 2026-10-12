"""
mcp_generic.py — any MCP server as a data source (NetSuite, HubSpot, Jira,
an internal data MCP...). If a platform has an MCP server, it can be a source
without writing a connector.

  type: mcp
  server:
    command: npx                       # stdio transport
    args: ["-y", "some-mcp-server"]
    env: {API_TOKEN: "${ENV:X}"}
    # or: url: https://mcp.example.com/mcp   headers: {...}   (streamable HTTP)
  startup_timeout: 90
  call_timeout: 45
  entities:
    invoice:
      fetch:
        tool: run_suiteql
        args: {query: "SELECT id, total, duedate FROM transaction WHERE entity = '{link_value}'"}
        records_path: items            # where the record list sits in the tool's JSON result
      fields: {...}

The connector only ever calls the tools named in its own entity config — it
never lists and picks tools on its own — so a server's write tools are
unreachable unless an admin deliberately names one.

The MCP session is opened once and held by a background thread with its own
asyncio loop for the life of the process: starting an MCP server per query
costs seconds (measured ~3-4s with the Salesforce server).
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import threading
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import AsyncExitStack

from ..core.connector import Connector, ConnectorError, register, render_template, safe_value
from ..core.mapping import get_path


def first_text(content) -> str:
    if not content:
        return ""
    texts = [getattr(block, "text", None) for block in content]
    return "\n".join(t for t in texts if t) or repr(content[0])


def parse_json_payload(text: str):
    """Tool results are often 'Some prefix:\\n\\n{...json...}'. Find the first
    JSON object/array rather than relying on the prefix wording."""
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        raise ConnectorError(f"tool result has no JSON payload: {text[:300]!r}")
    try:
        return json.loads(text[min(starts):])
    except json.JSONDecodeError as exc:
        raise ConnectorError(f"tool result is not valid JSON: {text[:300]!r}") from exc


class McpSession:
    """A persistent MCP client session on a background event loop."""

    def __init__(self, label: str, server: dict, startup_timeout: float = 90, call_timeout: float = 45):
        try:
            from mcp import ClientSession  # noqa: F401
        except ImportError as exc:
            raise ConnectorError("MCP sources need `pip install mcp`") from exc
        self.label = label
        self.server = server
        self.call_timeout = call_timeout
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name=f"mcp-{label}")
        self._thread.start()
        self._exit_stack: AsyncExitStack | None = None
        self._session = None
        self._lock: asyncio.Lock | None = None
        self._closed = False
        future = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
        try:
            future.result(timeout=startup_timeout)
        except FuturesTimeoutError as exc:
            self.close()
            raise ConnectorError(
                f"MCP server for '{label}' did not start within {startup_timeout}s "
                "(first run can be slow while npx downloads the package — try again or raise startup_timeout)"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            self.close()
            raise ConnectorError(f"could not start MCP server for '{label}': {exc}") from exc
        atexit.register(self.close)

    async def _connect(self) -> None:
        from mcp import ClientSession

        self._exit_stack = AsyncExitStack()
        if self.server.get("url"):
            headers = self.server.get("headers") or {}
            try:  # mcp 2.x
                from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

                http_client = await self._exit_stack.enter_async_context(create_mcp_http_client(headers=headers))
                transport = streamable_http_client(self.server["url"], http_client=http_client)
            except ImportError:
                try:  # mcp 1.x
                    from mcp.client.streamable_http import streamablehttp_client
                except ImportError as exc:
                    raise ConnectorError("this mcp package version has no streamable HTTP client") from exc
                transport = streamablehttp_client(self.server["url"], headers=headers)
            streams = await self._exit_stack.enter_async_context(transport)
            read, write = streams[0], streams[1]
        else:
            from mcp.client.stdio import StdioServerParameters, stdio_client

            env = {**os.environ, **{k: str(v) for k, v in (self.server.get("env") or {}).items()}}
            params = StdioServerParameters(command=self.server["command"], args=[str(a) for a in self.server.get("args", [])], env=env)
            read, write = await self._exit_stack.enter_async_context(stdio_client(params))
        self._session = await self._exit_stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        self._lock = asyncio.Lock()

    async def _call_async(self, tool: str, arguments: dict):
        # One call at a time per session: concurrent calls on one shared
        # session were never verified safe for every server.
        async with self._lock:
            result = await self._session.call_tool(tool, arguments)
        if getattr(result, "is_error", None) or getattr(result, "isError", None):
            raise ConnectorError(f"MCP tool '{tool}' reported an error: {first_text(result.content)[:500]}")
        structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
        # Servers whose tools return a plain string get it wrapped as {"result": "<text>"}:
        # that is not structured data, so parse the text instead.
        if structured and not (len(structured) == 1 and isinstance(structured.get("result"), str)):
            return structured
        return parse_json_payload(first_text(result.content))

    def call(self, tool: str, arguments: dict):
        if self._closed:
            raise ConnectorError(f"MCP session '{self.label}' is closed")
        future = asyncio.run_coroutine_threadsafe(self._call_async(tool, arguments), self._loop)
        try:
            return future.result(timeout=self.call_timeout)
        except FuturesTimeoutError as exc:
            raise ConnectorError(f"MCP tool '{tool}' timed out after {self.call_timeout}s") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._exit_stack is not None:
            future = asyncio.run_coroutine_threadsafe(self._exit_stack.aclose(), self._loop)
            try:
                future.result(timeout=10)
            except Exception:  # noqa: BLE001 - best effort on shutdown
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


@register("mcp")
class McpConnector(Connector):
    escape = staticmethod(safe_value)

    def __init__(self, name: str, config: dict, session: McpSession | None = None):
        super().__init__(name, config)
        self._session = session
        self._session_lock = threading.Lock()

    @property
    def session(self) -> McpSession:
        with self._session_lock:
            if self._session is None:
                server = self.config.get("server") or {}
                if not server.get("command") and not server.get("url"):
                    raise ConnectorError(f"source '{self.name}': server.command or server.url is required")
                self._session = McpSession(
                    self.name, server,
                    float(self.config.get("startup_timeout", 90)), float(self.config.get("call_timeout", 45)),
                )
            return self._session

    def execute(self, request: dict, ctx: dict, mode: str) -> list[dict]:
        tool = request.get("tool")
        if not tool:
            raise ConnectorError(f"source '{self.name}': request needs `tool:`")
        args = render_template(request.get("args") or {}, ctx, escape=self.escape)
        payload = self.session.call(tool, args)
        data = payload if request.get("records_path") in (None, "", "$") else get_path(payload, request["records_path"])
        if data is None:
            return []
        if isinstance(data, dict):
            return [data]
        return [r for r in data if isinstance(r, dict)]

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
