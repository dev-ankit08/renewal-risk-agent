"""
salesforce_client.py — one interface, one real implementation.

McpSalesforceClient connects to the official Salesforce MCP server
(`@salesforce/mcp`, run via `npx`), which reuses the `sf` CLI session already
authenticated on this machine — no separate Salesforce credential needed.
The connection is started once and kept alive for the process's lifetime (a
background thread running its own asyncio event loop holds the MCP session)
rather than spawning a new process per query — that per-call subprocess
approach (the previous `sf`-CLI-based design) had confirmed ~3-4s of fixed
startup overhead on every single read, which is what made bulk screens slow.

Every read passes through guard.soql_guard / guard.strip_banned_fields first.
Specialists never see an unguarded record. This contract is unchanged from
the previous `sf`-CLI-based implementation — only the transport underneath
soql_query/get_record changed.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import threading
from abc import ABC, abstractmethod
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

import config
import guard


class SalesforceClient(ABC):
    @abstractmethod
    def soql_query(self, soql: str, as_of_date: str) -> list[dict]:
        ...

    @abstractmethod
    def get_record(self, object_name: str, record_id: str) -> dict | None:
        ...

    @abstractmethod
    def describe_object(self, object_name: str) -> dict:
        ...


class SalesforceQueryError(Exception):
    """Raised when the MCP server / underlying Salesforce session reports a
    failure (bad SOQL, org not found/authorized, timeout, malformed
    response, etc.)."""


def _strip_attributes(record: dict) -> dict:
    return {k: v for k, v in record.items() if k != "attributes"}


def _first_text(content) -> str:
    """An MCP CallToolResult's content is a list of blocks. run_soql_query
    returns one text block. Confirmed live against the real server."""
    if not content:
        return "(no content)"
    block = content[0]
    return getattr(block, "text", None) or repr(block)


def _extract_json_from_text(text: str) -> dict:
    """run_soql_query's text is 'SOQL query results:\\n\\n' followed by a
    JSON object — this exact prefix is not documented anywhere; confirmed
    live via a schema-discovery smoke test against the real server. Find the
    first '{' rather than assume the prefix wording is stable."""
    brace = text.find("{")
    if brace == -1:
        raise SalesforceQueryError(f"run_soql_query's response has no JSON payload: {text!r}")
    try:
        return json.loads(text[brace:])
    except json.JSONDecodeError as exc:
        raise SalesforceQueryError(f"run_soql_query returned malformed JSON: {text!r}") from exc


def _extract_records(parsed: dict) -> list[dict]:
    """Confirmed live shape: {"records": [...], "totalSize": N, "done": true}
    — the tool result IS the query result directly (unlike the old `sf data
    query --json` CLI output, which nested this under a "result" key)."""
    if "records" in parsed:
        return parsed["records"]
    raise SalesforceQueryError(f"run_soql_query returned an unrecognized shape: {parsed!r}")


class McpSalesforceClient(SalesforceClient):
    """The only Salesforce integration in this repo. No mock/live toggle —
    this is always the real org identified by `org_alias`, reached through a
    persistent connection to the official Salesforce MCP server."""

    def __init__(self, org_alias: str):
        self.org_alias = org_alias
        # run_soql_query requires a "directory" argument (the tool's own
        # schema calls it "the directory to run this tool from") — this
        # repo isn't an actual sfdx project, but the smoke test confirmed
        # any valid, real directory path works fine for a plain data query.
        self._directory = os.path.dirname(os.path.abspath(__file__))
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name=f"mcp-sf-{org_alias}"
        )
        self._thread.start()
        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._call_lock: asyncio.Lock | None = None
        self._closed = False
        self._connect()
        atexit.register(self.close)

    def _connect(self) -> None:
        future = asyncio.run_coroutine_threadsafe(self._connect_async(), self._loop)
        try:
            future.result(timeout=config.SF_MCP_STARTUP_TIMEOUT_SECONDS)
        except FuturesTimeoutError as exc:
            raise SalesforceQueryError(
                f"Salesforce MCP server did not start within "
                f"{config.SF_MCP_STARTUP_TIMEOUT_SECONDS}s — first run can be slow "
                "while npx downloads the package; it's cached after that. Try again, "
                "or raise RENEWAL_AGENT_SF_MCP_STARTUP_TIMEOUT_SECONDS."
            ) from exc
        except Exception as exc:
            raise SalesforceQueryError(
                f"Could not start the Salesforce MCP server for org '{self.org_alias}': {exc}\n"
                "Confirm `npx --version` works and `sf org list` shows this org as Connected."
            ) from exc

    async def _connect_async(self) -> None:
        self._exit_stack = AsyncExitStack()
        params = StdioServerParameters(
            command="npx",
            args=["-y", "@salesforce/mcp", "--orgs", self.org_alias, "--toolsets", config.SF_MCP_TOOLSETS],
        )
        read, write = await self._exit_stack.enter_async_context(stdio_client(params))
        self._session = await self._exit_stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        self._call_lock = asyncio.Lock()

    def _call_tool_sync(self, name: str, arguments: dict) -> str:
        if self._closed:
            raise SalesforceQueryError("Salesforce MCP client is closed")
        future = asyncio.run_coroutine_threadsafe(self._call_tool_async(name, arguments), self._loop)
        try:
            return future.result(timeout=config.SF_MCP_CALL_TIMEOUT_SECONDS)
        except FuturesTimeoutError as exc:
            raise SalesforceQueryError(
                f"MCP tool '{name}' timed out after {config.SF_MCP_CALL_TIMEOUT_SECONDS}s"
            ) from exc

    async def _call_tool_async(self, name: str, arguments: dict) -> str:
        # Serialized through one lock: there's no confirmed evidence yet that
        # concurrent calls on one shared session are safe (SDK + server
        # combination untested for that), and the speed win this class
        # exists for comes from eliminating per-call process startup, not
        # from query-level parallelism — see DOCUMENTATION.md.
        async with self._call_lock:
            result = await self._session.call_tool(name, arguments)
        if result.is_error:
            raise SalesforceQueryError(f"MCP tool '{name}' reported an error: {_first_text(result.content)}")
        return _first_text(result.content)

    def _run_soql(self, soql: str) -> list[dict]:
        raw_text = self._call_tool_sync("run_soql_query", {
            "query": soql,
            "usernameOrAlias": self.org_alias,
            "directory": self._directory,
        })
        parsed = _extract_json_from_text(raw_text)
        return _extract_records(parsed)

    def soql_query(self, soql: str, as_of_date: str, date_field: str = "CreatedDate") -> list[dict]:
        guarded = guard.soql_guard(soql, as_of_date, date_field)
        records = self._run_soql(guarded)
        cleaned = []
        for record in records:
            record = _strip_attributes(record)
            safe_record, _stripped = guard.strip_banned_fields(record)
            cleaned.append(safe_record)
        return cleaned

    def get_record(self, object_name: str, record_id: str) -> dict | None:
        # No as_of_date in this method's signature, so the full soql_guard
        # (which requires one) doesn't apply here — but the DML block always does.
        soql = guard.block_dml(f"SELECT FIELDS(ALL) FROM {object_name} WHERE Id = '{record_id}' LIMIT 1")
        records = self._run_soql(soql)
        if not records:
            return None
        record = _strip_attributes(records[0])
        safe_record, _stripped = guard.strip_banned_fields(record)
        return safe_record

    def describe_object(self, object_name: str) -> dict:
        raise NotImplementedError(
            "describe_object is unused by the real pipeline (agent.py/prescreen.py/"
            "nlquery.py only ever call soql_query) and the 'data' toolset this client "
            "requests has no schema-describe tool. Add the 'metadata' toolset and check "
            "session.list_tools() again before implementing this for a real use case."
        )

    def close(self) -> None:
        """Idempotent — safe to call from atexit and explicitly."""
        if self._closed:
            return
        self._closed = True
        if self._exit_stack is not None:
            future = asyncio.run_coroutine_threadsafe(self._exit_stack.aclose(), self._loop)
            try:
                future.result(timeout=10)
            except Exception:
                pass  # best-effort on shutdown
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


# Constructing a client is now expensive (spawns a process, does a session
# handshake) — memoized so every caller/thread shares one persistent
# connection per org alias instead of spawning redundant MCP servers.
_clients: dict[str, SalesforceClient] = {}
_clients_lock = threading.Lock()


def get_client(org_alias: str | None = None) -> SalesforceClient:
    """Refuses to run against any org alias other than the known synthetic
    test sandbox unless config.ZDR_CONFIRMED is set — see config.py. Real
    Salesforce data reaches the Anthropic API in specialist prompts, so
    pointing this at a real org is a deliberate, confirmed act, not a
    side effect of changing an env var."""
    alias = org_alias or config.SALESFORCE_ORG_ALIAS
    known_test_aliases = {"renewalRiskSandbox"}
    if alias not in known_test_aliases and not config.ZDR_CONFIRMED:
        raise RuntimeError(
            f"Refusing to query org alias '{alias}' — it isn't the known synthetic "
            "test sandbox, so it may hold real customer data, which this pipeline "
            "sends to the Anthropic API in specialist prompts. Confirm your "
            "Anthropic API organization has a Zero Data Retention agreement in "
            "place (contact Anthropic to arrange one — this is an account-level "
            "agreement, not something this codebase can configure), then set "
            "RENEWAL_AGENT_ZDR_CONFIRMED=true to proceed."
        )
    with _clients_lock:
        client = _clients.get(alias)
        if client is None:
            client = McpSalesforceClient(alias)
            _clients[alias] = client
        return client
