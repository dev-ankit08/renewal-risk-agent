"""
objects.py — "which object holds this data type?", per kind of system.

A data type (renewal, ticket, survey…) is assigned to one connected system and
one of its objects in connectors.yaml:

  assign:
    renewal: {source: crm, object: Opportunity, link: AccountId}
    ticket:  {source: helpdesk, object: tickets.csv, link: OrgId}

`link` is the field that ties a record to the customer (not needed for the
customer list itself). Each kind of system has one adapter in ADAPTERS below:
how to list its objects, the fields of one object, and the read config for an
assigned object. Adding a kind of system = one entry.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

LISTED = {"account", "customer", "renewal"}  # data types the agent lists, not only fetches per account
CUSTOMER_LIST = {"account", "customer"}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)?$")
_FROM = re.compile(r"\bFROM\s+([A-Za-z0-9_.$]+)", re.IGNORECASE)
_SF_HIDDEN = re.compile(r"(History|Share|Feed|ChangeEvent|Tag|__mdt|__e|__x|__b|__hd)$")
_HS_STANDARD = ["companies", "contacts", "deals", "tickets", "line_items", "products", "quotes", "meetings", "calls", "tasks"]
MAX_OBJECTS = 500


class ObjectError(ValueError):
    pass


def valid_name(name: str) -> str:
    """An object or field name safe to put in a query (identifiers only)."""
    if not _IDENT.match(str(name or "")):
        raise ObjectError(f"'{name}' is not a valid object or field name")
    return name


# ---- Salesforce (both transports) -----------------------------------------

def _sf_list(conn) -> list[dict]:
    # EntityDefinition can't be paged (no queryMore): one batch, so LIMIT it, and ask only for
    # customizable objects — every business and custom object, none of the system tables.
    rows = conn.run_soql("SELECT QualifiedApiName, Label FROM EntityDefinition "
                         "WHERE IsQueryable = true AND IsCustomizable = true LIMIT 2000")
    return sorted(({"name": r["QualifiedApiName"], "label": r.get("Label") or r["QualifiedApiName"]}
                   for r in rows if not _SF_HIDDEN.search(r["QualifiedApiName"])), key=lambda o: o["label"].lower())


def _sf_fields(conn, obj: str) -> list[dict]:
    out = []
    for name, info in sorted(conn.object_fields(obj).items()):
        kind = str(info.get("type") or "")
        out.append({"name": name, "label": info.get("label") or name,
                    "link": name == "Id" or "lookup" in kind.lower() or "reference" in kind.lower() or "master" in kind.lower()})
    return out


def _sf_block(entity: str, obj: str, link: str | None, fields: dict) -> dict:
    if entity in CUSTOMER_LIST:
        block = {"fetch": {"object": obj, "account_field": link or "Id", "as_of": False}}
    else:
        block = {"fetch": {"object": obj, "account_field": link or "AccountId"}}
    if entity in LISTED:
        block["list"] = {"object": obj}
    return block


def _sf_current(block: dict) -> str | None:
    for mode in ("fetch", "list"):
        request = block.get(mode) or {}
        if request.get("object"):
            return request["object"]
        found = _FROM.search(request.get("soql", ""))
        if found:
            return found.group(1)
    return None


# ---- SQL databases ----------------------------------------------------------

def _sql_list(conn) -> list[dict]:
    inspector = conn._sa.inspect(conn.engine)
    names = sorted(set(inspector.get_table_names()) | set(inspector.get_view_names()))
    return [{"name": n, "label": n} for n in names]


def _sql_fields(conn, obj: str) -> list[dict]:
    schema, _, table = valid_name(obj).rpartition(".")
    columns = conn._sa.inspect(conn.engine).get_columns(table, schema=schema or None)
    return [{"name": c["name"], "label": c["name"], "link": "id" in c["name"].lower()} for c in columns]


def _sql_block(entity: str, obj: str, link: str | None, fields: dict) -> dict:
    table = valid_name(obj)
    block = {"fetch": {"query": f"SELECT * FROM {table} WHERE {valid_name(link or 'id')} = :link_value"}}
    if entity in LISTED:
        block["list"] = {"query": f"SELECT * FROM {table}"}
    return block


def _sql_current(block: dict) -> str | None:
    found = _FROM.search((block.get("fetch") or block.get("list") or {}).get("query", ""))
    return found.group(1) if found else None


# ---- CSV / Excel uploads ----------------------------------------------------

def _csv_list(conn) -> list[dict]:
    folder = Path(conn.folder)
    files = sorted(p.name for p in folder.glob("*") if p.suffix.lower() in (".csv", ".xlsx")) if folder.exists() else []
    return [{"name": f, "label": f} for f in files]


def _csv_fields(conn, obj: str) -> list[dict]:
    rows = conn._read(obj)
    header = list(rows[0]) if rows else []
    return [{"name": h, "label": h, "link": any(w in h.lower() for w in ("account", "customer", "client", "org", "id"))} for h in header]


def _csv_block(entity: str, obj: str, link: str | None, fields: dict) -> dict:
    block: dict = {}
    if entity in LISTED or not link:
        block["list"] = {"file": obj}
    if link:
        block["fetch"] = {"file": obj, "match": link}
    return block


def _csv_current(block: dict) -> str | None:
    return (block.get("fetch") or block.get("list") or {}).get("file")


# ---- HubSpot ----------------------------------------------------------------

def _hs_list(conn) -> list[dict]:
    custom = conn._send("GET", f"{conn.base_url}/crm/v3/schemas", {}, None).get("results") or []
    objects = [{"name": n, "label": n.replace("_", " ").title()} for n in _HS_STANDARD]
    return objects + [{"name": s.get("fullyQualifiedName") or s["objectTypeId"], "label": (s.get("labels") or {}).get("plural") or s["name"]}
                      for s in custom]


def _hs_fields(conn, obj: str) -> list[dict]:
    return []  # records are tied to the customer by their company association, not a field


def _hs_block(entity: str, obj: str, link: str | None, fields: dict) -> dict:
    props = sorted({str(e).split("|")[0].strip()[len("properties."):] for e in fields.values()
                    if isinstance(e, str) and e.startswith("properties.")})
    base = f"/crm/v3/objects/{obj}"
    block = {"describe": {"request": {"path": f"/crm/v3/properties/{obj}", "records_path": "results", "pagination": {"type": "none"}},
                          "path_prefix": "properties."}}
    if entity in CUSTOMER_LIST:
        block["fetch"] = {"request": {"path": base + "/{link_value}", "params": {"properties": ",".join(props)}, "records_path": "$",
                                      "pagination": {"type": "none"}}}
    else:
        block["fetch"] = {"request": {"method": "POST", "path": base + "/search", "records_path": "results",
                                      "json": {"filterGroups": [{"filters": [{"propertyName": "associations.company", "operator": "EQ",
                                                                              "value": "{link_value}"}]}],
                                               "properties": props, "limit": 100},
                                      "pagination": {"type": "cursor", "cursor_path": "paging.next.after", "param": "after", "in": "body"}}}
    if entity in LISTED:
        block["list"] = {"request": {"path": base, "params": {"limit": 100, "properties": ",".join(props)}, "records_path": "results"}}
    return block


def _hs_current(block: dict) -> str | None:
    found = re.search(r"/crm/v3/objects/([^/{]+)", str(((block.get("fetch") or block.get("list") or {}).get("request") or {}).get("path", "")))
    return found.group(1) if found else None


@dataclass(frozen=True)
class Adapter:
    label: str
    list: Callable          # (connector) -> [{name, label}]
    fields: Callable        # (connector, object) -> [{name, label, link}]
    block: Callable         # (entity, object, link, fields) -> read config
    current: Callable       # (read config) -> object it reads, or None
    id_field: str
    created_field: str | None
    links: bool             # does the admin pick a link field?


ADAPTERS: dict[str, Adapter] = {
    "salesforce": Adapter("Salesforce object", _sf_list, _sf_fields, _sf_block, _sf_current, "Id", "CreatedDate | date", True),
    "sql": Adapter("Table or view", _sql_list, _sql_fields, _sql_block, _sql_current, "id", None, True),
    "csv": Adapter("File", _csv_list, _csv_fields, _csv_block, _csv_current, "id", None, True),
    "hubspot": Adapter("HubSpot object", _hs_list, _hs_fields, _hs_block, _hs_current, "id", "createdAt | date", False),
}


def kind_of(source: dict) -> str | None:
    """Which adapter serves a resolved source (None: no object picker, e.g. REST or MCP)."""
    if source.get("_preset") == "hubspot":
        return "hubspot"
    kind = source.get("type")
    return "salesforce" if kind in ("salesforce", "salesforce_rest") else kind if kind in ADAPTERS else None


def default_fields(adapter: Adapter, entity: str, link: str | None) -> dict:
    fields = {"id": adapter.id_field}
    if adapter.created_field:
        fields["created_at"] = adapter.created_field
    if link and entity not in CUSTOMER_LIST:
        fields["account_id"] = link
    return fields


def apply(assign: dict, sources: dict[str, dict], overrides: dict[str, dict], problems: list[str]) -> None:
    """Serve each assigned data type from exactly its chosen object. The preset's own
    object keeps the preset's read config and field expressions; another object
    gets a generated read config and the admin's (or a starter) field mapping."""
    for entity, a in (assign or {}).items():
        src = sources.get(a.get("source"))
        adapter = ADAPTERS.get(kind_of(src)) if src else None
        if adapter is None:
            problems.append(f"connectors.yaml assign.{entity}: source '{a.get('source')}' is missing or has no objects to pick")
            continue
        entities = src.setdefault("entities", {})
        existing = entities.get(entity)
        if not (isinstance(existing, dict) and adapter.current(existing) == a.get("object")):
            mapped = {k: v for k, v in ((overrides.get(a["source"]) or {}).get(entity) or {}).get("fields", {}).items() if v is not None}
            fields = mapped or default_fields(adapter, entity, a.get("link"))
            try:
                entities[entity] = {**adapter.block(entity, a["object"], a.get("link"), fields), "fields": fields}
            except ObjectError as exc:
                problems.append(f"connectors.yaml assign.{entity}: {exc}")
                continue
        for name, other in sources.items():
            if name != a["source"]:
                (other.get("entities") or {}).pop(entity, None)
