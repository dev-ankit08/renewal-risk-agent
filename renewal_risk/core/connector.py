"""
connector.py — the contract every data source implements.

A connector knows how to *execute a request* against its platform (a SOQL
query, a REST call, a SQL statement, an MCP tool call, a CSV read). Everything
else — which request to run per entity, mapping raw records into the standard
model, as-of filtering, the read-only rule — is shared and lives here.

Each source's config has `entities:`, one block per standard entity it
provides (the block may also be a list, to union several raw objects into one
entity, e.g. Salesforce Task + Event -> activity):

  ticket:
    fetch:  {...}     records for ONE account (ctx: link_value, as_of, ...)
    list:   {...}     records across all accounts (account / customer / renewal listing)
    search: {...}     account search by name (ctx: text)            [optional]
    fields: {...}     standard_field: field_spec (see mapping.py)

What goes inside fetch/list/search is connector-specific (`soql:`, `request:`,
`query:`, `tool:`, `file:`). Template variables available to every request:
  {link_value} {as_of} {as_of_iso} {as_of_epoch} {text} {before} {after} {limit}

Third-party connectors register under the entry-point group
`renewal_risk.connectors` (name = connector type, value = Connector subclass).
"""
from __future__ import annotations

import re
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timezone

from . import mapping, model


class ConnectorError(Exception):
    """Any failure talking to a source: auth, network, bad query, bad shape."""


@dataclass
class HealthResult:
    ok: bool
    detail: str


# Values substituted into query strings must look like ids, domains, emails or
# dates. Anything else is rejected rather than escaped: there is no escaping
# rule that is correct for every platform a template might target.
_SAFE_VALUE = re.compile(r"^[\w.@:+\-/ ]{1,200}$")


def safe_value(value) -> str:
    text = "" if value is None else str(value)
    if not _SAFE_VALUE.match(text) or ".." in text:
        raise ConnectorError(f"refusing to template unsafe value into a query: {text!r}")
    return text


def build_context(link_value=None, as_of: str | None = None, **extra) -> dict:
    as_of = (as_of or date.today().isoformat())[:10]
    end_of_day = datetime.strptime(as_of, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
    ctx = {
        "as_of": as_of,
        "as_of_iso": end_of_day.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of_epoch": int(end_of_day.timestamp()),
        "limit": 200,
    }
    if link_value is not None:
        ctx["link_value"] = link_value
    ctx.update({k: v for k, v in extra.items() if v is not None})
    return ctx


def render_template(value, ctx: dict, escape=safe_value):
    """Fill {name} placeholders. A string that is exactly one placeholder keeps
    the context value's type (so `limit: "{limit}"` stays an int)."""
    if isinstance(value, str):
        whole = re.fullmatch(r"\{([a-z_]+)\}", value)
        if whole and whole.group(1) in ctx:
            v = ctx[whole.group(1)]
            return v if isinstance(v, (int, float)) else escape(v)

        def repl(match):
            name = match.group(1)
            if name not in ctx:
                return match.group(0)
            v = ctx[name]
            return str(v) if isinstance(v, (int, float)) else escape(v)
        return re.sub(r"\{([a-z_]+)\}", repl, value)
    if isinstance(value, dict):
        return {k: render_template(v, ctx, escape) for k, v in value.items()}
    if isinstance(value, list):
        return [render_template(v, ctx, escape) for v in value]
    return value


class Connector(ABC):
    """Base class. Subclasses implement `execute` (and optionally `close`)."""

    type_name = "base"

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.entities_config: dict = config.get("entities") or {}

    # -- subclass hooks ------------------------------------------------------

    @abstractmethod
    def execute(self, request: dict, ctx: dict, mode: str) -> list[dict]:
        """Run one request block (the dict under fetch/list/search) and return
        raw records. `mode` is "fetch", "list" or "search"."""

    def health(self) -> HealthResult:
        """Default health check: read one record of the first listable entity."""
        if not self.provides():
            return HealthResult(False, "source provides no entities")
        listable = [e for e in self.provides() if self.has_mode(e, "list")]
        if not listable:
            return HealthResult(True, "configured — nothing listable to test; records are read per account")
        try:
            self.sample(listable[0], limit=1)
            return HealthResult(True, f"read {listable[0]} OK")
        except Exception as exc:  # noqa: BLE001
            return HealthResult(False, f"{type(exc).__name__}: {exc}")

    def close(self) -> None:
        pass

    # -- shared behaviour ----------------------------------------------------

    def provides(self) -> list[str]:
        return [e for e in self.entities_config if e in model.ENTITIES]

    def _blocks(self, entity: str) -> list[dict]:
        spec = self.entities_config.get(entity)
        if spec is None:
            raise ConnectorError(f"source '{self.name}' does not provide '{entity}'")
        return spec if isinstance(spec, list) else [spec]

    def _run(self, entity: str, mode: str, ctx: dict) -> list[dict]:
        out: list[dict] = []
        ran_any = False
        for block in self._blocks(entity):
            request = block.get(mode)
            if request is None:
                continue
            ran_any = True
            raws = self.execute(request, ctx, mode)
            fields = block.get("fields") or {}
            for raw in raws:
                record = mapping.map_record(raw, entity, fields, self.name)
                if record is not None:
                    out.append(record)
        if not ran_any:
            raise ConnectorError(f"source '{self.name}' entity '{entity}' has no '{mode}' request configured")
        return out

    def has_mode(self, entity: str, mode: str) -> bool:
        try:
            return any(b.get(mode) is not None for b in self._blocks(entity))
        except ConnectorError:
            return False

    def fetch(self, entity: str, link_value, as_of: str) -> list[dict]:
        """Standard records of `entity` for one account, as of a date. Records
        created after as_of are dropped here even if the source filtered
        server-side — a second line of defence, not the only one."""
        ctx = build_context(link_value, as_of)
        records = self._run(entity, "fetch", ctx)
        kept = []
        for r in records:
            created = r.get(model.AS_OF_FIELD)
            if created and created > ctx["as_of"]:
                continue
            if r.get("account_id") in (None, ""):
                r["account_id"] = str(link_value)
            kept.append(r)
        return kept

    def list(self, entity: str, **ctx_extra) -> list[dict]:
        return self._run(entity, "list", build_context(None, None, **ctx_extra))

    def search(self, entity: str, text: str) -> list[dict]:
        """Name search. Falls back to list + substring filter when the source
        has no server-side search configured."""
        if self.has_mode(entity, "search"):
            return self._run(entity, "search", build_context(None, None, text=text))
        needle = text.strip().lower()
        return [r for r in self.list(entity) if needle in str(r.get("name") or "").lower()]

    def sample(self, entity: str, limit: int = 3, link_value=None) -> list[dict]:
        """Raw (unmapped) records, for mapping suggestions and health checks.
        Uses the list request when there is one, else fetch with `link_value`
        (or the source's `sample_link_value`)."""
        link_value = link_value if link_value is not None else self.config.get("sample_link_value")
        for block in self._blocks(entity):
            for mode in ("list", "fetch"):
                request = block.get(mode)
                if request is None or (mode == "fetch" and link_value is None):
                    continue
                ctx = build_context(link_value, None, limit=limit, before="2999-12-31", after="1900-01-01")
                return self.execute(request, ctx, mode)[:limit]
        raise ConnectorError(
            f"cannot sample '{entity}' from '{self.name}': no list request, and no account to fetch for yet"
        )

    def field_metadata(self, entity: str) -> dict[str, dict]:
        """Platform metadata per raw field path: {path: {label, description, type, options}}.
        Overridden by connectors that can ask the platform (Salesforce
        FieldDefinition, HubSpot properties, Zendesk ticket fields...)."""
        return {}

    def describe(self, entity: str, link_value=None, sample_size: int = 20) -> dict:
        """What this source has for one entity: every raw field with its label,
        description, type, allowed values and example values. Free-text fields
        are listed (so an admin sees them) but never carry example values."""
        from .guard import is_banned

        samples = self.sample(entity, limit=sample_size, link_value=link_value)
        fields: dict[str, dict] = {}
        for raw in samples:
            for path, value in _flatten(raw).items():
                info = fields.setdefault(path, {"name": path, "examples": [], "type": None, "free_text": is_banned(path)})
                if value in (None, "", []) or info["free_text"]:
                    continue
                info["type"] = info["type"] or _infer_type(value)
                text = str(value)[:60]
                if text not in info["examples"] and len(info["examples"]) < 5:
                    info["examples"].append(text)
        try:
            meta = self.field_metadata(entity)
        except Exception:  # noqa: BLE001 - metadata is a bonus; sampled fields still work
            meta = {}
        for path, m in meta.items():
            info = fields.setdefault(path, {"name": path, "examples": [], "type": None, "free_text": is_banned(path)})
            for key in ("label", "description", "options"):
                if m.get(key):
                    info[key] = m[key][:20] if key == "options" else str(m[key])[:300]
            info["type"] = info["type"] or m.get("type")
        safe_samples = []
        from .guard import strip_banned_fields
        for raw in samples[:3]:
            safe_samples.append({k: (str(v)[:60] if v is not None else None) for k, v in _flatten(strip_banned_fields(raw)[0]).items()})
        return {"source": self.name, "entity": entity, "fields": sorted(fields.values(), key=lambda f: f["name"]),
                "samples": safe_samples, "sample_count": len(samples)}


def _flatten(record: dict, prefix: str = "", depth: int = 0) -> dict:
    out: dict = {}
    for key, value in (record or {}).items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and depth < 3:
            out.update(_flatten(value, f"{path}.", depth + 1))
        elif isinstance(value, list) and value and isinstance(value[0], dict) and depth < 3:
            out.update(_flatten(value[0], f"{path}[0].", depth + 1))
        else:
            out[path] = value
    return out


def _infer_type(value) -> str:
    from .mapping import to_date

    if isinstance(value, bool) or str(value).lower() in ("true", "false"):
        return "bool"
    if isinstance(value, (int, float)):
        return "num"
    if isinstance(value, list):
        return "list"
    text = str(value)
    if to_date(text) and len(text) >= 8 and not text.isdigit():
        return "date"
    try:
        float(text.replace(",", ""))
        return "num"
    except ValueError:
        return "str"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_registry: dict[str, type[Connector]] = {}
_registry_lock = threading.Lock()
_entry_points_loaded = False


def register(type_name: str):
    def decorator(cls):
        cls.type_name = type_name
        _registry[type_name] = cls
        return cls
    return decorator


def _load_builtin_and_plugins() -> None:
    global _entry_points_loaded
    if _entry_points_loaded:
        return
    # Built-ins register themselves on import.
    from ..connectors import csv_file, mcp_generic, rest, salesforce, salesforce_rest, sql  # noqa: F401
    try:
        from importlib.metadata import entry_points
        for ep in entry_points(group="renewal_risk.connectors"):
            try:
                _registry.setdefault(ep.name, ep.load())
            except Exception as exc:  # noqa: BLE001 - a broken plugin must not break the app
                print(f"warning: connector plugin '{ep.name}' failed to load: {exc}")
    except Exception:  # noqa: BLE001
        pass
    _entry_points_loaded = True


def connector_types() -> list[str]:
    with _registry_lock:
        _load_builtin_and_plugins()
        return sorted(_registry)


def create(name: str, config: dict) -> Connector:
    with _registry_lock:
        _load_builtin_and_plugins()
        cls = _registry.get(config.get("type"))
    if cls is None:
        raise ConnectorError(f"source '{name}': unknown connector type '{config.get('type')}' (known: {', '.join(connector_types())})")
    return cls(name, config)


class ConnectorPool:
    """One live connector per source for the process lifetime (some, like the
    MCP ones, hold a subprocess and are expensive to start)."""

    def __init__(self, sources: dict[str, dict]):
        self.sources = sources
        self._live: dict[str, Connector] = {}
        self._lock = threading.Lock()

    def get(self, name: str) -> Connector:
        with self._lock:
            conn = self._live.get(name)
            if conn is None:
                if name not in self.sources:
                    raise ConnectorError(f"unknown source '{name}'")
                conn = create(name, self.sources[name])
                self._live[name] = conn
            return conn

    def providers(self, entity: str) -> list[str]:
        """Source names whose config provides `entity` (no connection made)."""
        out = []
        for name, cfg in self.sources.items():
            if entity in (cfg.get("entities") or {}):
                out.append(name)
        return out

    def close(self) -> None:
        with self._lock:
            for conn in self._live.values():
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            self._live.clear()
