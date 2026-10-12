"""
salesforce.py — Salesforce through the official Salesforce MCP server
(`@salesforce/mcp`, started with npx). Authentication reuses the `sf` CLI
session for the org alias (`sf org login web|jwt --alias <alias>`); there is
no Salesforce secret in this codebase or its config.

  type: salesforce
  org_alias: prodOrg
  entities:
    renewal:
      fetch: {object: Opportunity, account_field: AccountId}        # SELECT FIELDS(ALL) ... WHERE AccountId = '<id>'
      list:  {soql: "SELECT Id, AccountId, ... FROM Opportunity WHERE IsClosed = false AND CloseDate <= {before} ..."}
      fields: {id: Id, amount: Amount, ...}

Every SOQL statement passes the read-only check, and every fetch gets a
`<date_field> <= as_of` bound injected into its WHERE clause (enforce_as_of)
so a backtest can never see records created after its as-of date.
"""
from __future__ import annotations

import os
import re

from ..core.connector import Connector, ConnectorError, HealthResult, register, render_template, safe_value
from .mcp_generic import McpSession


class GuardViolation(ConnectorError):
    """A query that would break a read-only / as-of rule."""


# ---------------------------------------------------------------------------
# SOQL guards
# ---------------------------------------------------------------------------

_QUOTED = re.compile(r"'(?:\\.|[^'\\])*'")
_DML = re.compile(r"\b(insert|update|delete|upsert|merge|undelete)\b", re.IGNORECASE)


def block_dml(soql: str) -> str:
    """Read-only or bust: a single SELECT, with no DML keyword outside string
    literals. (Literals are skipped so an account named 'Merge Labs' or
    'Update Partners' can still be searched for.)"""
    unquoted = _QUOTED.sub("''", soql)
    if ";" in unquoted.strip().rstrip(";") or not re.match(r"^\s*select\b", unquoted, re.IGNORECASE) or _DML.search(unquoted):
        raise GuardViolation(f"DML statement blocked by guard: {soql!r}")
    return soql


def enforce_as_of(soql: str, as_of_date: str, date_field: str = "CreatedDate") -> str:
    """Inject `<date_field> <= <as_of>T23:59:59Z` into a SOQL query's WHERE
    clause (creating one before ORDER BY / GROUP BY / LIMIT if needed)."""
    block_dml(soql)
    clause = f"{date_field} <= {str(as_of_date)[:10]}T23:59:59Z"
    unquoted = _QUOTED.sub(lambda m: "x" * len(m.group(0)), soql)  # same length, keywords hidden
    where_match = re.search(r"\bWHERE\b", unquoted, re.IGNORECASE)
    if where_match:
        idx = where_match.end()
        return f"{soql[:idx]} {clause} AND{soql[idx:]}"
    tail_match = re.search(r"\b(ORDER BY|GROUP BY|LIMIT)\b", unquoted, re.IGNORECASE)
    if tail_match:
        idx = tail_match.start()
        return f"{soql[:idx]}WHERE {clause} {soql[idx:]}"
    return f"{soql.rstrip()} WHERE {clause}"


def escape_soql(value) -> str:
    """Escape a value for a SOQL string literal (backslash first, then quote)."""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _soql_value(value) -> str:
    return escape_soql(safe_value(value))


def _soql_search_text(value) -> str:
    # Free text typed by a user: escaped, plus LIKE wildcards neutralised.
    return escape_soql(value).replace("%", "\\%").replace("_", "\\_")


def strip_attributes(record: dict) -> dict:
    out = {}
    for key, value in record.items():
        if key == "attributes":
            continue
        out[key] = strip_attributes(value) if isinstance(value, dict) else value
    return out


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------

_FROM = re.compile(r"\bFROM\s+([A-Za-z0-9_]+)", re.IGNORECASE)


class SoqlConnector(Connector):
    """Shared by both Salesforce transports: SOQL building, guards, metadata.
    Subclasses implement `run_soql(soql) -> list[dict]`."""

    def run_soql(self, soql: str) -> list[dict]:
        raise NotImplementedError

    def _build_soql(self, request: dict, ctx: dict, mode: str) -> str:
        if request.get("soql"):
            escape = _soql_search_text if mode == "search" else _soql_value
            soql = render_template(request["soql"], ctx, escape=escape)
        else:
            obj = request.get("object")
            if not obj:
                raise ConnectorError(f"source '{self.name}': request needs `soql:` or `object:`")
            select = request.get("select", "FIELDS(ALL)")
            where = []
            if mode == "fetch":
                where.append(f"{request.get('account_field', 'AccountId')} = '{_soql_value(ctx['link_value'])}'")
            if request.get("where"):
                where.append(f"({render_template(request['where'], ctx, escape=_soql_value)})")
            soql = f"SELECT {select} FROM {obj}"
            if where:
                soql += " WHERE " + " AND ".join(where)
            if request.get("order_by"):
                soql += f" ORDER BY {request['order_by']}"
            # FIELDS(ALL) requires an explicit LIMIT, capped at 200 by Salesforce.
            soql += f" LIMIT {min(int(request.get('limit', 200)), int(ctx.get('limit', 200)) if mode != 'fetch' else 200)}"
        if mode == "fetch" and request.get("as_of", True):
            return enforce_as_of(soql, ctx["as_of"], request.get("date_field", "CreatedDate"))
        return block_dml(soql)

    def execute(self, request: dict, ctx: dict, mode: str) -> list[dict]:
        return [strip_attributes(r) for r in self.run_soql(self._build_soql(request, ctx, mode))]

    def objects_for(self, entity: str) -> list[str]:
        out = []
        for block in self._blocks(entity):
            for mode in ("fetch", "list"):
                request = block.get(mode) or {}
                obj = request.get("object") or (_FROM.search(request.get("soql", "")) or [None, None])[1]
                if obj and obj not in out:
                    out.append(obj)
        return out

    def field_metadata(self, entity: str) -> dict[str, dict]:
        """Labels, types and admin-written descriptions from FieldDefinition."""
        out: dict[str, dict] = {}
        for obj in self.objects_for(entity):
            rows = self.run_soql(
                "SELECT QualifiedApiName, Label, DataType, Description FROM FieldDefinition "
                f"WHERE EntityDefinition.QualifiedApiName = '{_soql_value(obj)}'"
            )
            for r in rows:
                out.setdefault(r["QualifiedApiName"], {"label": r.get("Label"), "type": r.get("DataType"), "description": r.get("Description")})
        return out

    def health(self) -> HealthResult:
        try:
            self.run_soql("SELECT Id FROM Account LIMIT 1")
            return HealthResult(True, "connected to Salesforce")
        except Exception as exc:  # noqa: BLE001
            return HealthResult(False, f"{type(exc).__name__}: {exc}")


@register("salesforce")
class SalesforceConnector(SoqlConnector):
    """Through the official Salesforce MCP server, authenticated by an `sf` CLI alias."""

    def __init__(self, name: str, config: dict, session=None):
        super().__init__(name, config)
        self.org_alias = config.get("org_alias")
        if not self.org_alias:
            raise ConnectorError(f"source '{name}': `org_alias` is required (an alias authorised with `sf org login`)")
        self._session = session
        # run_soql_query requires a real directory; any existing one works for data queries.
        self._directory = config.get("directory") or os.getcwd()

    @property
    def session(self):
        if self._session is None:
            toolsets = self.config.get("toolsets", "data")  # "data" has run_soql_query and no write/deploy tools
            self._session = McpSession(
                f"salesforce-{self.org_alias}",
                {"command": self.config.get("npx", "npx"), "args": ["-y", "@salesforce/mcp", "--orgs", self.org_alias, "--toolsets", toolsets]},
                float(self.config.get("startup_timeout", 90)), float(self.config.get("call_timeout", 45)),
            )
        return self._session

    def run_soql(self, soql: str) -> list[dict]:
        payload = self.session.call("run_soql_query", {"query": block_dml(soql), "usernameOrAlias": self.org_alias, "directory": self._directory})
        if not isinstance(payload, dict) or "records" not in payload:
            raise ConnectorError(f"run_soql_query returned an unrecognised shape: {str(payload)[:300]}")
        return payload["records"]

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
