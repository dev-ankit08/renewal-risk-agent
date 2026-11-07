"""
api.py — the admin UI's back end, mounted at /admin/api (UI at /admin/).

Every endpoint needs a signed-in admin (cookie) or an admin API key, except
status, first-run setup, login and the OAuth callback. Saves go through the
ConfigStore (same YAML the CLI uses) and then rebuild the running context, so
changes apply without a restart.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets as pysecrets
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from .. import __version__
from ..auth import oauth
from ..auth.secrets import default_store
from ..core import model
from ..core import playbooks as playbooks_mod
from ..core import backtest, objects, prompts
from ..core import sections as sections_mod
from ..core import signals as signals_mod
from ..core.connector import ConnectorError
from ..core.llm import LLMError
from ..core.settings import all_signals, load_packs, load_presets, signal_catalog
from ..core.validate import validate_settings
from ..service import workspace
from ..service.context import get_context, reload_context
from . import auth, packs
from .config_store import FileConfigStore
from .mapping_agent import MappingAgent, inactive_signals, missing_required

router = APIRouter(prefix="/admin/api", tags=["admin"])
callback_router = APIRouter(tags=["admin"])

_NAME = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
_last_tests: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def secret_store_for(ctx):
    return default_store(ctx.settings.state_dir, ctx.settings.tenant_id)


def config_store(ctx) -> FileConfigStore:
    return FileConfigStore(ctx.settings.dir, history=ctx.store)


def public_url(request: Request | None = None) -> str:
    env = os.environ.get("RENEWAL_RISK_PUBLIC_URL")
    if env:
        return env.rstrip("/")
    if request is not None:
        return str(request.base_url).rstrip("/")
    return "http://localhost:8080"


def callback_url(request: Request | None = None) -> str:
    return f"{public_url(request)}/admin/oauth/callback"


def _check_name(name: str) -> None:
    if not _NAME.match(name):
        raise HTTPException(400, "source names are lowercase letters, digits and _ (e.g. crm, billing)")


def _source(ctx, name: str) -> dict:
    src = ctx.settings.sources.get(name)
    if src is None:
        raise HTTPException(404, f"no source named '{name}'")
    return src


def _connection_state(ctx, name: str, src: dict) -> dict:
    missing = sorted(m for m in ctx.settings.missing_env if m.startswith(f"SECRET:sources/{name}/"))
    state = {"credentials_complete": not missing, "missing": [m.split("/")[-1] for m in missing]}
    if src.get("auth_method") == "oauth2":
        bundle = secret_store_for(ctx).get(oauth.token_key(name))
        state["oauth_connected"] = bool(bundle)
        state["oauth_connected_at"] = bundle.get("obtained_at") if bundle else None
    return state


def _sample_link_value(ctx, source: str):
    """An account (or customer) id to sample fetch-only entities with."""
    engine = ctx.engine
    if source == ctx.settings.primary or engine.linker.is_same_id(source):
        accounts = engine.primary().list("account")
        return accounts[0]["id"] if accounts else None
    report = engine.linker.report()
    if report and source in report.sources:
        for entries in report.sources[source].links.values():
            if entries:
                return entries[0]["value"]
    try:
        customers = engine.pool.get(source).list("customer")
        return customers[0]["id"] if customers else None
    except ConnectorError:
        return None


def _apply(ctx_changes) -> dict:
    """Run a config change, then rebuild the context and report validation."""
    ctx_changes()
    ctx = reload_context()
    errors, warnings = validate_settings(ctx.settings)
    return {"ok": True, "errors": errors, "warnings": warnings}


# ---------------------------------------------------------------------------
# status / first run / login
# ---------------------------------------------------------------------------

@router.get("/status")
def status(request: Request) -> dict:
    ctx = get_context()
    return {
        "version": __version__, "needs_setup": ctx.store.admin_count() == 0,
        "pack": ctx.settings.pack.get("name"), "packs": [{"name": n, "label": p.get("label", n), "description": p.get("description", "")}
                                                          for n, p in load_packs().items()],
        "callback_url": callback_url(request), "model_configured": ctx.engine.ai_enabled(),
        "workspace": workspace.status(ctx), "matching_needed": ctx.engine.linker.needs_matching(),
    }


class SetupBody(BaseModel):
    setup_token: str
    email: str
    password: str
    pack: str = "saas"


@router.post("/setup")
def first_run(body: SetupBody) -> JSONResponse:
    ctx = get_context()
    expected = auth.setup_token(secret_store_for(ctx), ctx.store)
    if expected is None:
        raise HTTPException(409, "already set up — sign in instead")
    if not pysecrets.compare_digest(body.setup_token.strip(), expected):
        raise HTTPException(403, "wrong setup token — it is printed in the server console")
    if body.pack not in load_packs():
        raise HTTPException(400, f"unknown business type '{body.pack}'")
    email = body.email.strip().lower()
    try:
        ctx.store.create_admin(email, body.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    store = config_store(ctx)
    agent_cfg = store.read("agent.yaml")
    agent_cfg["pack"] = body.pack
    store.write("agent.yaml", agent_cfg, email)
    auth.setup_token(secret_store_for(ctx), ctx.store)  # clears the token
    reload_context()
    return _login_response(email)


class LoginBody(BaseModel):
    email: str
    password: str


def _login_response(email: str) -> JSONResponse:
    ctx = get_context()
    resp = JSONResponse({"ok": True, "email": email})
    resp.set_cookie(auth.COOKIE, auth.issue_session(secret_store_for(ctx), email), max_age=auth.SESSION_SECONDS,
                    httponly=True, samesite="strict", secure=public_url().startswith("https"))
    return resp


@router.post("/login")
def login(body: LoginBody) -> JSONResponse:
    ctx = get_context()
    if not ctx.store.check_admin(body.email, body.password):
        time.sleep(0.5)  # blunt brute force
        raise HTTPException(401, "wrong email or password")
    return _login_response(body.email.strip().lower())


@router.post("/logout")
def logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE)
    return resp


@router.get("/me")
def me(admin: str = Depends(auth.current_admin)) -> dict:
    return {"email": admin}


class PackBody(BaseModel):
    pack: str


@router.put("/pack")
def set_pack(body: PackBody, admin: str = Depends(auth.current_admin)) -> dict:
    if body.pack not in load_packs():
        raise HTTPException(400, f"unknown business type '{body.pack}'")
    ctx = get_context()
    store = config_store(ctx)

    def change():
        cfg = store.read("agent.yaml")
        cfg["pack"] = body.pack
        store.write("agent.yaml", cfg, admin)
    return _apply(change)


# ---------------------------------------------------------------------------
# catalog + sources
# ---------------------------------------------------------------------------

@router.get("/catalog")
def get_catalog(request: Request, admin: str = Depends(auth.current_admin)) -> dict:
    return packs.catalog(get_context().settings, callback_url(request))


@router.get("/sources")
def list_sources(admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    out = []
    for name, src in ctx.settings.sources.items():
        out.append({
            "name": name, "preset": src.get("preset"), "type": src.get("type"), "auth_method": src.get("auth_method"),
            "entities": sorted((src.get("entities") or {}).keys()), "primary": name == ctx.settings.primary,
            "test_data": bool(src.get("test_data")), "connection": _connection_state(ctx, name, src),
            "last_test": _last_tests.get(name),
        })
    return {"sources": out, "primary": ctx.settings.primary}


class SourceBody(BaseModel):
    preset: str | None = None
    type: str | None = None
    auth_method: str | None = None
    params: dict = {}
    secrets: dict = {}
    test_data: bool = False
    make_primary: bool = False


@router.put("/sources/{name}")
def save_source(name: str, body: SourceBody, admin: str = Depends(auth.current_admin)) -> dict:
    _check_name(name)
    ctx = get_context()
    store, sstore = config_store(ctx), secret_store_for(ctx)
    presets = load_presets(ctx.settings.dir)
    if body.preset and body.preset not in presets:
        raise HTTPException(400, f"unknown system '{body.preset}'")
    if not body.preset and body.type not in ("csv", "rest", "sql", "mcp"):
        raise HTTPException(400, "choose a system (preset) or a generic type")
    if "@" in str((body.params or {}).get("client_id", "")):  # a login email typed where the app's key belongs
        raise HTTPException(400, "the client id / consumer key comes from the OAuth app you created in the system "
                                 "(e.g. Salesforce Connected App), not your login email — and never enter your password here")

    def change():
        doc = store.read("connectors.yaml")
        sources = doc.setdefault("sources", {})
        previous = sources.get(name) or {}
        entry: dict = {"preset": body.preset} if body.preset else {"type": body.type, "entities": previous.get("entities", {})}
        if body.preset:
            preset = presets[body.preset]
            method = next((m for m in preset.get("auth_methods") or [] if m["method"] == body.auth_method), None)
            if preset.get("auth_methods") and method is None:
                raise HTTPException(400, f"choose how to sign in to {preset.get('label', body.preset)}")
            declared = {**(preset.get("params") or {}), **((method or {}).get("params") or {})}
            if method:
                entry["auth"] = {"method": method["method"]}
            for param, spec in declared.items():
                spec = spec or {}
                if spec.get("secret"):
                    key = f"sources/{name}/{param}"
                    if body.secrets.get(param):
                        sstore.set(key, str(body.secrets[param]))
                    if body.secrets.get(param) or sstore.get(key):
                        entry[param] = f"${{SECRET:{key}}}"
                elif body.params.get(param) not in (None, ""):
                    entry[param] = body.params[param]
        elif body.type == "csv":
            entry["path"] = previous.get("path") or f"uploads/{name}"
        elif body.type == "sql":
            entry["dsn"] = _secret_ref(sstore, name, "dsn", body.secrets.get("dsn", "")) or previous.get("dsn")
        if body.test_data:
            entry["test_data"] = True
        sources[name] = entry
        if body.make_primary or not doc.get("primary"):
            doc["primary"] = name
        store.write("connectors.yaml", doc, admin)
    return _apply(change)


@router.delete("/sources/{name}")
def delete_source(name: str, admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    store = config_store(ctx)

    def change():
        doc = store.read("connectors.yaml")
        (doc.get("sources") or {}).pop(name, None)
        (doc.get("linking") or {}).pop(name, None)
        if doc.get("assign"):  # data types read from it go back to "pick an object"
            doc["assign"] = {e: a for e, a in doc["assign"].items() if (a or {}).get("source") != name}
        if doc.get("primary") == name:
            doc["primary"] = next(iter(doc.get("sources") or {}), None)
        store.write("connectors.yaml", doc, admin)
        secret_store_for(ctx).delete_prefix(f"sources/{name}/")
    return _apply(change)


@router.post("/sources/{name}/test")
def test_source(name: str, admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    _source(ctx, name)
    try:
        result = ctx.engine.pool.get(name).health()
        outcome = {"ok": result.ok, "detail": result.detail}
    except Exception as exc:  # noqa: BLE001
        outcome = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    outcome["at"] = time.time()
    _last_tests[name] = outcome
    return outcome


@router.post("/sources/{name}/oauth/start")
def oauth_start(name: str, request: Request, admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    src = _source(ctx, name)
    if src.get("auth_method") != "oauth2" or not src.get("oauth"):
        raise HTTPException(400, "this source is not set up for OAuth sign-in")
    if not src.get("client_id"):
        raise HTTPException(400, "save the client id and secret first")
    url = oauth.start_flow(secret_store_for(ctx), name, src["oauth"], src["client_id"], callback_url(request))
    return {"authorize_url": url, "callback_url": callback_url(request)}


@callback_router.get("/admin/oauth/callback")
def oauth_callback(state: str = "", code: str = "", error: str = "", error_description: str = "") -> RedirectResponse:
    ctx = get_context()
    sstore = secret_store_for(ctx)
    source = oauth.pending_source(sstore, state) if state else None
    if error or not code or source is None:
        message = error_description or error or "sign-in was cancelled or the link expired"
        return RedirectResponse(f"/admin/#/sources?oauth_error={_q(message)}")
    src = ctx.settings.sources.get(source) or {}
    try:
        oauth.finish_flow(sstore, state, code, src.get("oauth") or {}, src.get("client_id", ""), src.get("client_secret"))
    except oauth.OAuthError as exc:
        return RedirectResponse(f"/admin/#/sources?oauth_error={_q(str(exc))}")
    reload_context()
    return RedirectResponse(f"/admin/#/sources?connected={_q(source)}")


def _q(text: str) -> str:
    from urllib.parse import quote
    return quote(text[:300])


@router.post("/sources/{name}/upload")
async def upload_csv(name: str, file: UploadFile = File(...), admin: str = Depends(auth.current_admin)) -> dict:
    """Store an uploaded CSV / XLSX for a `csv` source; returns its columns."""
    _check_name(name)
    ctx = get_context()
    filename = Path(file.filename or "upload.csv").name
    if not re.fullmatch(r"[A-Za-z0-9_.\- ]{1,80}\.(csv|xlsx)", filename):
        raise HTTPException(400, "upload a .csv or .xlsx file with a simple name")
    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(413, "file is larger than 50 MB")
    folder = ctx.settings.dir / "uploads" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_bytes(data)
    columns: list[str] = []
    if filename.endswith(".csv"):
        columns = next(csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace"))), [])
    return {"file": filename, "columns": columns}


class PrimaryBody(BaseModel):
    source: str


@router.put("/primary")
def set_primary(body: PrimaryBody, admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    _source(ctx, body.source)
    store = config_store(ctx)

    def change():
        doc = store.read("connectors.yaml")
        doc["primary"] = body.source
        store.write("connectors.yaml", doc, admin)
    return _apply(change)


# ---------------------------------------------------------------------------
# custom sections (sections.yaml): a new data type + its own specialist
# ---------------------------------------------------------------------------

_OBJ_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.$ -]{0,119}$")  # objects, tables, uploaded file names
SYSTEM_TIMEOUT = 60


def _section(ctx, key: str) -> dict:
    found = next((x for x in ctx.settings.sections if x["key"] == key), None)
    if found is None:
        raise HTTPException(404, f"no custom section '{key}'")
    return found


def _built_in_names(ctx) -> set[str]:
    return sections_mod.reserved_names(ctx.settings.agent, ctx.settings.pack, ctx.settings.sections)


class SectionBody(BaseModel):
    label: str
    description: str = ""
    data_type: dict
    focus: str


@router.put("/sections/{key}")
def save_section(key: str, body: SectionBody, admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    store = config_store(ctx)
    current = next((x for x in ctx.settings.sections if x["key"] == key), None)
    if current and current["data_type"]["name"] != body.data_type.get("name"):
        raise HTTPException(400, "the data type name cannot change once created (connections and signals use it)")

    def change():
        doc = store.read("sections.yaml")
        items = [x for x in doc.get("sections") or [] if x.get("key") != key] + [{"key": key, **body.model_dump()}]
        try:
            doc["sections"] = sections_mod.normalize({"sections": items}, _built_in_names(ctx))
        except sections_mod.SectionError as exc:
            raise HTTPException(400, str(exc)) from exc
        store.write("sections.yaml", doc, admin)
    return _apply(change)


@router.delete("/sections/{key}")
def delete_section(key: str, admin: str = Depends(auth.current_admin)) -> dict:
    """Remove a section, its data type's connections, and the sources made only for it."""
    ctx = get_context()
    section = _section(ctx, key)
    entity = section["data_type"]["name"]
    users = [x["name"] for x in _catalog(ctx) if x.get("origin") == "custom" and entity in signals_mod.required_entities(x)]
    if users:
        raise HTTPException(409, f"delete the signals that use this section first: {', '.join(users)}")
    store, sstore = config_store(ctx), secret_store_for(ctx)

    def change():
        doc = store.read("sections.yaml")
        doc["sections"] = [x for x in doc.get("sections") or [] if x.get("key") != key]
        store.write("sections.yaml", doc, admin)
        conn = store.read("connectors.yaml")
        for name, src in list((conn.get("sources") or {}).items()):
            (src.get("entities") or {}).pop(entity, None)
            if src.get("type") and src.get("section") == key and not src.get("entities"):
                conn["sources"].pop(name)
                sstore.delete_prefix(f"sources/{name}/")
            overrides = store.read(f"mappings/{name}.overrides.yaml")
            if overrides.pop(entity, None) is not None:
                store.write(f"mappings/{name}.overrides.yaml", overrides, admin)
        if conn.get("primary") not in (conn.get("sources") or {}):
            conn["primary"] = next(iter(conn.get("sources") or {}), None)
        store.write("connectors.yaml", conn, admin)
    return _apply(change)


def _adapter(ctx, name: str):
    adapter = objects.ADAPTERS.get(objects.kind_of(_source(ctx, name)) or "")
    if adapter is None:
        raise HTTPException(400, f"'{name}' has no objects to pick (REST and MCP systems use a request instead)")
    return adapter


def _from_system(ctx, name: str, read):
    """Ask the connected system, with errors and slow systems reported, never hung."""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(lambda: read(ctx.engine.pool.get(name))).result(timeout=SYSTEM_TIMEOUT)
    except FuturesTimeout as exc:
        raise HTTPException(504, f"'{name}' did not answer within {SYSTEM_TIMEOUT} seconds — test the connection and try again") from exc
    except (ConnectorError, objects.ObjectError) as exc:
        raise HTTPException(502, f"{name}: {exc}") from exc
    finally:
        pool.shutdown(wait=False)  # a call that timed out finishes in the background


@router.get("/sources/{name}/objects")
def list_objects(name: str, admin: str = Depends(auth.current_admin)) -> dict:
    """The objects a connected system offers (Salesforce objects, tables, files, HubSpot objects); cached per connection."""
    ctx = get_context()
    adapter = _adapter(ctx, name)

    def read(conn):
        if getattr(conn, "_objects", None) is None:
            conn._objects = adapter.list(conn)[:objects.MAX_OBJECTS]
        return conn._objects
    return {"source": name, "label": adapter.label, "links": adapter.links, "objects": _from_system(ctx, name, read)}


@router.get("/sources/{name}/objects/{obj}/fields")
def object_fields(name: str, obj: str, admin: str = Depends(auth.current_admin)) -> dict:
    """One object's fields — to pick the field that links a record to its customer."""
    ctx = get_context()
    adapter = _adapter(ctx, name)
    if not _OBJ_NAME.match(obj):
        raise HTTPException(400, f"'{obj}' is not a valid object name")
    fields = _from_system(ctx, name, lambda conn: adapter.fields(conn, obj))
    if adapter.links and not fields:
        raise HTTPException(404, f"'{name}' has no object '{obj}' (check the name, e.g. Survey__c)")
    return {"object": obj, "fields": fields}


class AssignBody(BaseModel):
    source: str
    object: str
    link: str | None = None


@router.put("/assign/{entity}")
def assign_object(entity: str, body: AssignBody, admin: str = Depends(auth.current_admin)) -> dict:
    """Lock which system and object a data type is read from (then map its fields)."""
    ctx = get_context()
    if entity not in model.ENTITIES:
        raise HTTPException(400, f"unknown data type '{entity}'")
    adapter = _adapter(ctx, body.source)
    if not _OBJ_NAME.match(body.object) or (body.link and not _OBJ_NAME.match(body.link)):
        raise HTTPException(400, "object and link field names are letters, digits, _ . or $")
    if adapter.links and not body.link and entity not in objects.CUSTOMER_LIST:
        raise HTTPException(400, "choose the field that links each record to its customer")
    store = config_store(ctx)

    def change():
        doc = store.read("connectors.yaml")
        doc.setdefault("assign", {})[entity] = {k: v for k, v in body.model_dump().items() if v}
        if entity == "account":
            doc["primary"] = body.source  # the customer list comes from the chosen system
        store.write("connectors.yaml", doc, admin)
    return _apply(change)


@router.delete("/assign/{entity}")
def unassign_object(entity: str, admin: str = Depends(auth.current_admin)) -> dict:
    store = config_store(get_context())

    def change():
        doc = store.read("connectors.yaml")
        (doc.get("assign") or {}).pop(entity, None)
        store.write("connectors.yaml", doc, admin)
    return _apply(change)


class ConnectBody(BaseModel):
    kind: str                       # salesforce | sql | rest | mcp
    source: str                     # salesforce: the existing connection; others: a new or existing source name
    test_data: bool = False
    object: str = ""                # salesforce
    account_field: str = "AccountId"
    dsn: str = ""                   # sql (secret)
    query: str = ""
    base_url: str = ""              # rest
    auth_type: str = "none"         # none | bearer | header | query | basic
    auth_name: str = ""             # header / query parameter name, or basic username
    secret: str = ""                # token / password (secret)
    path: str = ""
    params: dict = {}
    records_path: str = ""
    command: str = ""               # mcp, stdio
    args: list[str] = []
    url: str = ""                   # mcp, streamable HTTP
    env: dict = {}                  # mcp: {VAR: value} (secrets)
    tool: str = ""
    tool_args: dict = {}


def _secret_ref(sstore, source: str, key: str, value: str) -> str | None:
    """Store a credential encrypted; config files only ever hold the reference."""
    if value:
        sstore.set(f"sources/{source}/{key}", value.strip())
    return f"${{SECRET:sources/{source}/{key}}}" if value or sstore.get(f"sources/{source}/{key}") else None


def _generic_source(body: ConnectBody, sstore) -> tuple[dict, dict]:
    """(connection settings, fetch request) for a new SQL / REST / MCP source."""
    if body.kind == "sql":
        from ..connectors.sql import check_read_only
        try:
            check_read_only(body.query)
        except ConnectorError as exc:
            raise HTTPException(400, str(exc)) from exc
        if ":link_value" not in body.query:
            raise HTTPException(400, "the query must filter by the account with :link_value (e.g. WHERE account_id = :link_value)")
        return {"dsn": _secret_ref(sstore, body.source, "dsn", body.dsn)}, {"query": body.query}
    if body.kind == "rest":
        if not body.base_url.startswith(("https://", "http://")) or not body.path.startswith("/"):
            raise HTTPException(400, "give the API's base URL (https://…) and a path starting with /")
        auth: dict = {"type": body.auth_type}
        token = _secret_ref(sstore, body.source, "token", body.secret)
        if body.auth_type == "bearer":
            auth["token"] = token
        elif body.auth_type in ("header", "query"):
            auth.update(name=body.auth_name, value=token)
        elif body.auth_type == "basic":
            auth.update(username=body.auth_name, password=token)
        elif body.auth_type != "none":
            raise HTTPException(400, "auth must be none, bearer, header, query or basic")
        request = {"path": body.path, "params": body.params or {}}
        if body.records_path:
            request["records_path"] = body.records_path
        return {"connection": {"base_url": body.base_url.rstrip("/"), "auth": auth}}, {"request": request}
    if body.kind == "mcp":
        if not body.tool or not (body.command or body.url.startswith(("https://", "http://"))):
            raise HTTPException(400, "give the MCP server (a command, or an https URL) and the tool to call")
        env = {k: _secret_ref(sstore, body.source, f"env_{k}", str(v)) for k, v in (body.env or {}).items() if re.fullmatch(r"[A-Z][A-Z0-9_]*", k)}
        server = {"url": body.url} if body.url else {"command": body.command, "args": body.args}
        if env:
            server["env"] = env
        request = {"tool": body.tool, "args": body.tool_args or {}}
        if body.records_path:
            request["records_path"] = body.records_path
        return {"server": server}, request
    raise HTTPException(400, "connect with salesforce, sql, rest or mcp (CSV uses the upload)")


@router.post("/sections/{key}/connect")
def connect_section(key: str, body: ConnectBody, admin: str = Depends(auth.current_admin)) -> dict:
    """Point a section's data type at a Salesforce object, a SQL query, a REST endpoint or an MCP tool."""
    _check_name(body.source)
    ctx = get_context()
    entity = _section(ctx, key)["data_type"]["name"]
    store, sstore = config_store(ctx), secret_store_for(ctx)

    if body.kind == "salesforce":
        if _source(ctx, body.source).get("type") not in packs.SALESFORCE_TYPES:
            raise HTTPException(400, f"'{body.source}' is not a Salesforce connection")
        return assign_object(entity, AssignBody(source=body.source, object=body.object, link=body.account_field), admin)

    connection, request = _generic_source(body, sstore)

    def change():
        doc = store.read("connectors.yaml")
        sources = doc.setdefault("sources", {})
        entry = sources.get(body.source) or {"type": body.kind, "section": key}
        if entry.get("type") != body.kind:
            raise HTTPException(400, f"'{body.source}' is already a {entry.get('preset') or entry.get('type')} source — choose another name")
        entry.update({k: v for k, v in connection.items() if v})
        entities = entry.setdefault("entities", {}) or {}
        entities[entity] = {**(entities.get(entity) or {}), "fetch": request, "fields": (entities.get(entity) or {}).get("fields") or {"id": "id"}}
        entry["entities"] = entities
        if body.test_data:
            entry["test_data"] = True
        sources[body.source] = entry
        doc.setdefault("primary", next(iter(sources)))
        store.write("connectors.yaml", doc, admin)
    return _apply(change)


# ---------------------------------------------------------------------------
# field mapping
# ---------------------------------------------------------------------------

def _current_fields(src: dict, entity: str) -> dict:
    spec = (src.get("entities") or {}).get(entity)
    if not spec:
        raise HTTPException(404, f"source does not provide '{entity}'")
    block = spec[0] if isinstance(spec, list) else spec
    return dict(block.get("fields") or {})


def _describe(ctx, name: str, entity: str) -> dict:
    link = _sample_link_value(ctx, name)
    return _from_system(ctx, name, lambda conn: conn.describe(entity, link_value=link))


@router.get("/mapping/{name}/{entity}")
def get_mapping(name: str, entity: str, admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    src = _source(ctx, name)
    current = _current_fields(src, entity)
    multi = isinstance((src.get("entities") or {}).get(entity), list)
    return {
        "source": name, "entity": entity, "entity_description": model.ENTITIES[entity]["description"],
        "standard": model.field_docs(entity), "current": current, "read_only": multi,
        "describe": _describe(ctx, name, entity),
        "signals_affected": inactive_signals(entity, current, ctx.settings.signals),
    }


class SuggestBody(BaseModel):
    use_model: bool = True


@router.post("/mapping/{name}/{entity}/suggest")
def suggest_mapping(name: str, entity: str, body: SuggestBody, admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    src = _source(ctx, name)
    described = _describe(ctx, name, entity)
    llm = ctx.engine.llm if body.use_model else None
    note = None
    try:
        result = MappingAgent(llm, ctx.settings.signals).suggest(entity, described, _current_fields(src, entity))
    except LLMError as exc:
        note = f"The model was not available ({exc}); showing rule-based suggestions."
        result = MappingAgent(None, ctx.settings.signals).suggest(entity, described, _current_fields(src, entity))
    result["note"] = note
    return result


class FieldsBody(BaseModel):
    fields: dict


def _raw_samples(ctx, name: str, entity: str) -> list[dict]:
    try:
        return ctx.engine.pool.get(name).sample(entity, limit=5, link_value=_sample_link_value(ctx, name))
    except ConnectorError as exc:
        raise HTTPException(502, f"could not read sample records: {exc}") from exc


@router.post("/mapping/{name}/{entity}/preview")
def preview_mapping(name: str, entity: str, body: FieldsBody, admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    _source(ctx, name)
    records = MappingAgent.preview(entity, body.fields, _raw_samples(ctx, name, entity), name)
    return {"records": records, "signals_affected": inactive_signals(entity, body.fields, ctx.settings.signals)}


@router.put("/mapping/{name}/{entity}")
def save_mapping(name: str, entity: str, body: FieldsBody, admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    _source(ctx, name)
    unknown = [f for f in body.fields if model.field_type(entity, f) is None]
    if unknown:
        raise HTTPException(400, f"not standard {entity} fields: {', '.join(unknown)}")
    if "id" not in body.fields:
        raise HTTPException(400, "map at least the id field")
    return _apply(lambda: _write_mapping(ctx, name, entity, body.fields, admin))


def _write_mapping(ctx, name: str, entity: str, fields: dict, admin: str) -> None:
    """Write one data type's field mapping to the source's overrides file (versioned)."""
    preset_fields = _current_fields(_source(ctx, name), entity)
    store, fname = config_store(ctx), f"mappings/{name}.overrides.yaml"
    doc = store.read(fname)
    # Preset fields the admin removed are nulled so the override wins.
    doc[entity] = {**(doc.get(entity) or {}), "fields": {**{f: None for f in preset_fields if f not in fields}, **fields}}
    store.write(fname, doc, admin)
    ctx.store.save_mapping_version(name, entity, fields, admin)


@router.post("/mapping/auto")
def auto_map(admin: str = Depends(auth.current_admin)) -> dict:
    """Map every source's data types at once: confident suggestions are saved, only
    for fields not mapped yet; the rest is listed for review (Map fields on each Data sources row)."""
    ctx = get_context()
    llm = ctx.engine.llm if ctx.engine.ai_enabled() else None
    report, todo = [], []
    for name, src in ctx.settings.sources.items():
        for entity, spec in (src.get("entities") or {}).items():
            if not spec or isinstance(spec, list) or entity not in model.ENTITIES:
                continue
            current = {f: e for f, e in _current_fields(src, entity).items() if e is not None}
            try:
                described = _describe(ctx, name, entity)
                try:
                    result = MappingAgent(llm, ctx.settings.signals).suggest(entity, described, current)
                except LLMError:
                    result = MappingAgent(None, ctx.settings.signals).suggest(entity, described, current)
            except HTTPException as exc:
                report.append({"source": name, "entity": entity, "accepted": [], "needs_review": [], "error": exc.detail})
                continue
            accepted = {s["field"]: s["expr"] for s in result["suggestions"] if s["auto_accept"] and s["field"] not in current}
            if accepted:
                todo.append((name, entity, {**current, **accepted}))
            report.append({"source": name, "entity": entity, "accepted": sorted(accepted),
                           "needs_review": [s["field"] for s in result["suggestions"] if not s["auto_accept"] and s["field"] not in current]
                                           + result["required_missing"], "error": None})

    def change():
        for name, entity, fields in todo:
            _write_mapping(ctx, name, entity, fields, admin)
    return {**(_apply(change) if todo else {"ok": True, "errors": [], "warnings": []}), "mappings": report}


# ---------------------------------------------------------------------------
# account matching
# ---------------------------------------------------------------------------

def _report_json(report) -> dict:
    if report is None:
        return {"accounts": 0, "sources": {}, "note": "every source shares the primary account ids — nothing to match"}
    total = len(report.account_ids)
    out = {}
    for name, s in report.sources.items():
        linked = total if s.method == "same_id" else sum(1 for a in report.account_ids if s.links.get(a))
        out[name] = {"method": s.method, "error": s.error, "linked": linked, "coverage": round(linked / total, 3) if total else 0,
                     "customers": s.customer_count, "review": s.review[:200], "unmatched_customers": s.unmatched_customers[:200]}
    return {"accounts": total, "built_at": report.built_at, "sources": out}


@router.get("/linking")
def get_linking(admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    try:
        report = ctx.engine.linker.report()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"could not build account matches: {exc}") from exc
    return {**_report_json(report), "rules": ctx.settings.linking}


@router.post("/linking/rebuild")
def rebuild_linking(admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    try:
        return _report_json(ctx.engine.linker.build())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"could not build account matches: {exc}") from exc


class DecisionBody(BaseModel):
    source: str
    account_id: str
    link_value: str
    status: str  # approved | rejected


@router.post("/linking/decision")
def link_decision(body: DecisionBody, admin: str = Depends(auth.current_admin)) -> dict:
    from ..core.linking import read_manual_links, write_manual_links

    if body.status not in ("approved", "rejected"):
        raise HTTPException(400, "status must be approved or rejected")
    ctx = get_context()
    path = ctx.engine.linker.manual_path
    rows = [r for r in read_manual_links(path)
            if not (r["source"] == body.source and r["account_id"] == body.account_id and r.get("link_value") == body.link_value)]
    rows.append({"source": body.source, "account_id": body.account_id, "link_value": body.link_value, "status": body.status, "note": f"by {admin}"})
    write_manual_links(path, rows)
    return _report_json(ctx.engine.linker.build())


class RulesBody(BaseModel):
    rules: list[dict]


@router.put("/linking/{source}")
def set_link_rules(source: str, body: RulesBody, admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    _source(ctx, source)
    store = config_store(ctx)

    def change():
        doc = store.read("connectors.yaml")
        doc.setdefault("linking", {})[source] = body.rules
        store.write("connectors.yaml", doc, admin)
    return _apply(change)


# ---------------------------------------------------------------------------
# signals, playbooks
# ---------------------------------------------------------------------------

_SLUG = re.compile(r"^[a-z][a-z0-9_]{2,48}$")
_SIGNAL_META = ("name", "description", "specialist", "rule", "enabled", "custom", "how_calculated_note",
                "author", "created_at", "updated_at", "value_template", "origin")


def _catalog(ctx) -> list[dict]:
    return signal_catalog(ctx.settings.dir, ctx.settings.pack)


def _signal_row(sig: dict, provided: set[str], categories: list[dict]) -> dict:
    needed = sorted(signals_mod.required_entities(sig))
    section = sections_mod.section_of(sig, categories)
    row = {"name": sig["name"], "description": sig.get("description", ""), "specialist": section,
           "area": next((c["label"] for c in categories if c["key"] == section), section),
           "origin": sig.get("origin", "built-in"), "enabled": sig.get("enabled", True), "rule": sig.get("rule"),
           "thresholds": sig.get("thresholds", {}), "direction": sig.get("direction", "above"), "entities": needed,
           "has_data": bool(set(needed) & provided), "explanation": signals_mod.explain(sig),
           "how_calculated_note": sig.get("how_calculated_note", "")}
    if row["origin"] == "custom":
        row["spec"] = {k: v for k, v in sig.items() if k != "origin"}
    return row


def _signal_problems(sig: dict, ctx, editing: str | None = None) -> list[str]:
    """The one validation path for preview, test, draft and save."""
    problems = []
    name = sig.get("name", "")
    if not _SLUG.match(name or ""):
        problems.append("name: 3-49 lowercase letters, digits or _, starting with a letter (e.g. p1_ticket_spike)")
    builtin = {s["name"] for s in _catalog(ctx) if s.get("origin") == "built-in"}
    if name in builtin and name != editing:
        problems.append(f"name: '{name}' is a built-in signal — pick another name")
    return problems + signals_mod.validate([sig])


def _write_signal_overrides(ctx, admin: str, mutate) -> dict:
    """Read signals.yaml, let `mutate(entries_by_name)` change it, write it back."""
    store = config_store(ctx)

    def change():
        doc = store.read("signals.yaml")
        entries = {e["name"]: e for e in doc.get("signals") or [] if e.get("name")}
        mutate(entries)
        doc["signals"] = list(entries.values())
        store.write("signals.yaml", doc, admin)
    return _apply(change)


@router.get("/signals")
def get_signals(admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    provided = ctx.engine.available_entities()
    categories = ctx.settings.pack.get("categories") or []
    return {"signals": [_signal_row(s, provided, categories) for s in _catalog(ctx)], "pack": ctx.settings.pack.get("name")}


class SignalsBody(BaseModel):
    signals: list[dict]  # [{name, enabled, thresholds}]


@router.put("/signals")
def save_signals(body: SignalsBody, admin: str = Depends(auth.current_admin)) -> dict:
    """On/off and thresholds for every signal. Custom signals keep their full
    definition (the first version rewrote this file from built-ins only and
    silently dropped them)."""
    ctx = get_context()
    defaults = {s["name"]: s for s in all_signals(ctx.settings.pack)}

    def mutate(entries: dict) -> None:
        for item in body.signals:
            name = item.get("name")
            thresholds = {k: v for k, v in (item.get("thresholds") or {}).items() if k in signals_mod.SEVERITIES and v not in (None, "")}
            if name in entries and entries[name].get("custom"):
                entries[name].update(enabled=bool(item.get("enabled", True)), thresholds=thresholds or entries[name].get("thresholds", {}))
                continue
            base = defaults.get(name)
            if base is None:
                continue
            entry = {"name": name}
            if bool(item.get("enabled", True)) != base.get("enabled", True):
                entry["enabled"] = bool(item.get("enabled", True))
            if thresholds and thresholds != base.get("thresholds"):
                entry["thresholds"] = thresholds
            if len(entry) > 1:
                entries[name] = entry
            else:
                entries.pop(name, None)
    return _write_signal_overrides(ctx, admin, mutate)


@router.get("/signals/builder")
def signal_builder(admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    available = ctx.engine.available_entities()
    return {**signals_mod.builder_metadata(),
            "entities": {e: model.field_docs(e) for e in sorted(available)}}


class SignalBody(BaseModel):
    signal: dict
    original_name: str | None = None
    window_days: int = 120


@router.post("/signals/preview")
def preview_signal(body: SignalBody, admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    return {"explanation": signals_mod.explain(body.signal), "errors": _signal_problems(body.signal, ctx, body.original_name)}


@router.post("/signals/custom")
def save_custom_signal(body: SignalBody, admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    errors = _signal_problems(body.signal, ctx, body.original_name)
    if errors:
        raise HTTPException(400, "; ".join(errors))
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    sig = {k: v for k, v in body.signal.items() if k != "origin"}

    def mutate(entries: dict) -> None:
        previous = entries.pop(body.original_name or sig["name"], {})
        entries[sig["name"]] = {**sig, "custom": True, "author": previous.get("author", admin),
                                "created_at": previous.get("created_at", now), "updated_at": now}
    return _write_signal_overrides(ctx, admin, mutate)


@router.delete("/signals/custom/{name}")
def delete_custom_signal(name: str, admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()

    def mutate(entries: dict) -> None:
        if not (entries.get(name) or {}).get("custom"):
            raise HTTPException(404, f"no custom signal named '{name}'")
        entries.pop(name)
    return _write_signal_overrides(ctx, admin, mutate)


@router.post("/signals/test")
def test_signal(body: SignalBody, admin: str = Depends(auth.current_admin)) -> dict:
    """Run a draft signal on up to 20 accounts renewing soon — real data, no AI."""
    ctx = get_context()
    sig = {**body.signal, "name": body.signal.get("name") or "draft"}
    errors = signals_mod.validate([sig])
    if errors:
        raise HTTPException(400, "; ".join(errors))
    engine, today = ctx.engine, date.today()
    try:
        accounts = engine.list_renewals((today + timedelta(days=body.window_days)).isoformat(), today.isoformat())
    except ConnectorError:
        accounts = [{"account_id": a["id"], "name": a.get("name")} for a in engine.primary().list("account")]
    accounts = accounts[:20]
    needed, available = signals_mod.required_entities(sig), engine.available_entities()

    def run(acct: dict) -> dict:
        records, gaps, _ = engine.gather(acct["account_id"], today.isoformat(), entities=needed)
        found = next(iter(signals_mod.evaluate([sig], records, today.isoformat(), available).values()))
        hit = found["signals"][0] if found["signals"] else None
        return {"account_id": acct["account_id"], "name": acct.get("name"), "fired": bool(hit),
                "severity": hit["severity"] if hit else None, "value": hit["value"] if hit else "",
                "evidence_ref": hit["evidence_ref"] if hit else None, "gaps": found["gaps"] + gaps}

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, accounts))
    return {"results": results, "fired": sum(r["fired"] for r in results), "tested": len(results),
            "explanation": signals_mod.explain(sig)}


class DraftBody(BaseModel):
    text: str


@router.post("/signals/draft")
def draft_signal(body: DraftBody, admin: str = Depends(auth.current_admin)) -> dict:
    """Plain English -> a signal definition the admin reviews, tests and saves."""
    ctx = get_context()
    text = body.text.strip()
    if not 5 <= len(text) <= 1000:
        raise HTTPException(400, "describe the warning sign in 5 to 1000 characters")
    builder = signal_builder(admin)
    entities = {e: {f: d["description"] for f, d in fields.items()} for e, fields in builder["entities"].items()}
    examples = [{k: v for k, v in s.items() if k != "origin"} for s in _catalog(ctx) if s["name"] in ("open_ticket_backlog", "no_recent_activity", "project_red")]
    system, user = prompts.signal_draft_prompt(text, builder, entities, examples)
    try:
        data = ctx.engine.llm.structured("utility", system, user, prompts.SIGNAL_DRAFT_SCHEMA)
    except LLMError as exc:
        raise HTTPException(503, f"AI drafting is unavailable ({exc}); build the signal in the form instead") from exc
    try:
        spec = json.loads(data["spec_json"]) if data["spec_json"].strip() else {}
    except ValueError:
        spec = {}
    spec = {k: v for k, v in spec.items() if k not in _SIGNAL_META}
    sig = {"name": re.sub(r"[^a-z0-9_]", "_", data["name"].lower()).strip("_")[:48], "description": data["description"],
           "rule": data["rule"], **spec}
    return {"signal": sig, "reason": data["reason"], "explanation": signals_mod.explain(sig), "errors": _signal_problems(sig, ctx)}


@router.get("/playbooks")
def get_playbooks(admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    return {**ctx.settings.playbooks, "signals": [s["name"] for s in _catalog(ctx)]}


@router.put("/playbooks")
def save_playbooks(body: dict, admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    try:
        clean = playbooks_mod.normalize(body, {s["name"] for s in _catalog(ctx)}, strict=True)
    except playbooks_mod.PlaybookError as exc:
        raise HTTPException(400, str(exc)) from exc
    store = config_store(ctx)
    return _apply(lambda: store.write("playbooks.yaml", clean, admin))


@router.get("/actions")
def list_actions(status: str | None = None, admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    return {"actions": ctx.store.action_items(status=status), "summary": ctx.store.action_summary(30)}


# ---------------------------------------------------------------------------
# access + Slack
# ---------------------------------------------------------------------------

@router.get("/access")
def get_access(admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    raw = config_store(ctx).read("access.yaml")
    from ..access.rbac import DEFAULT_ROLES
    return {"users": raw.get("users") or [], "domains": raw.get("domains") or [],
            "roles": sorted({**DEFAULT_ROLES, **(raw.get("roles") or {})}),
            "api_keys": [{"name": k.get("name"), "as": k.get("as")} for k in raw.get("api_keys") or []]}


class AccessBody(BaseModel):
    users: list[dict] = []
    domains: list[dict] = []


@router.put("/access")
def save_access(body: AccessBody, admin: str = Depends(auth.current_admin)) -> dict:
    store = config_store(get_context())

    def clean(entry: dict, key: str) -> dict | None:
        if not entry.get(key) or not entry.get("role"):
            return None
        out = {key: str(entry[key]).strip().lower(), "role": entry["role"]}
        scope = entry.get("scope")
        if scope == "all" or (isinstance(scope, dict) and scope):
            out["scope"] = scope
        return out

    def change():
        doc = store.read("access.yaml")
        doc["users"] = [u for u in (clean(u, "email") for u in body.users) if u]
        doc["domains"] = [d for d in (clean(d, "domain") for d in body.domains) if d]
        store.write("access.yaml", doc, admin)
    return _apply(change)


class ApiKeyBody(BaseModel):
    name: str
    as_email: str


@router.post("/access/api-keys")
def create_api_key(body: ApiKeyBody, admin: str = Depends(auth.current_admin)) -> dict:
    _check_name(body.name)
    ctx = get_context()
    key = "rr_" + pysecrets.token_urlsafe(24)
    secret_store_for(ctx).set(f"api_keys/{body.name}", key)
    store = config_store(ctx)

    def change():
        doc = store.read("access.yaml")
        keys = [k for k in doc.get("api_keys") or [] if k.get("name") != body.name]
        keys.append({"name": body.name, "key": f"${{SECRET:api_keys/{body.name}}}", "as": body.as_email.strip().lower()})
        doc["api_keys"] = keys
        store.write("access.yaml", doc, admin)
    result = _apply(change)
    return {**result, "key": key, "note": "Copy it now — it is not shown again."}


@router.delete("/access/api-keys/{name}")
def delete_api_key(name: str, admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    store = config_store(ctx)

    def change():
        doc = store.read("access.yaml")
        doc["api_keys"] = [k for k in doc.get("api_keys") or [] if k.get("name") != name]
        store.write("access.yaml", doc, admin)
        secret_store_for(ctx).delete(f"api_keys/{name}")
    return _apply(change)


@router.get("/slack")
def get_slack(admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    raw = config_store(ctx).read("slack.yaml")
    sstore = secret_store_for(ctx)
    return {"mode": raw.get("mode", "socket"), "command": raw.get("command", "/renewal-risk"),
            "reply_visibility": raw.get("reply_visibility", "ephemeral"), "alerts": raw.get("alerts") or {},
            "actions": {"dm_owners": True, "min_band": "high", "snooze_days": 7, "cooldown_days": 30, **(raw.get("actions") or {})},
            "bot_token_set": bool(ctx.settings.slack.get("bot_token")), "app_token_set": bool(ctx.settings.slack.get("app_token")),
            "stored_in_ui": bool(sstore.get("slack/bot_token"))}


class SlackBody(BaseModel):
    bot_token: str | None = None
    app_token: str | None = None
    command: str = "/renewal-risk"
    reply_visibility: str = "ephemeral"
    alerts: dict = {}


@router.put("/slack")
def save_slack(body: SlackBody, admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    sstore, store = secret_store_for(ctx), config_store(ctx)

    def change():
        doc = store.read("slack.yaml")
        for field, key in (("bot_token", "slack/bot_token"), ("app_token", "slack/app_token")):
            value = getattr(body, field)
            if value:
                sstore.set(key, value.strip())
            if value or sstore.get(key):
                doc[field] = f"${{SECRET:{key}}}"
        doc.update({"mode": doc.get("mode", "socket"), "command": body.command, "reply_visibility": body.reply_visibility,
                    "alerts": {k: body.alerts.get(k) for k in ("channel", "dm_owner", "dm_roles", "min_band") if k in body.alerts}})
        store.write("slack.yaml", doc, admin)
    result = _apply(change)
    result["note"] = "Restart `renewal-risk serve` for the Slack connection to pick up new tokens."
    return result


class ActionsBody(BaseModel):
    dm_owners: bool = True
    min_band: str = "high"
    snooze_days: int = 7
    cooldown_days: int = 30


@router.put("/slack/actions")
def save_action_settings(body: ActionsBody, admin: str = Depends(auth.current_admin)) -> dict:
    """Action DM settings (Signals & playbooks > Owners & notifications)."""
    ctx = get_context()
    store = config_store(ctx)
    clean = {"dm_owners": body.dm_owners, "min_band": body.min_band if body.min_band in ("medium", "high") else "high",
             "snooze_days": max(1, min(body.snooze_days, 90)), "cooldown_days": max(1, min(body.cooldown_days, 365))}

    def change():
        doc = store.read("slack.yaml")
        doc["actions"] = clean
        store.write("slack.yaml", doc, admin)
    return _apply(change)


class SlackTestBody(BaseModel):
    channel: str


@router.post("/slack/test")
def slack_test(body: SlackTestBody, admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    token = ctx.settings.slack.get("bot_token")
    if not token:
        raise HTTPException(400, "save a bot token first")
    try:
        from slack_sdk import WebClient
        WebClient(token=token).chat_postMessage(channel=body.channel, text=":white_check_mark: Renewal Risk is connected to Slack.")
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Slack said: {exc}") from exc


# ---------------------------------------------------------------------------
# health + feedback
# ---------------------------------------------------------------------------

@router.get("/health")
def health(admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    errors, warnings = validate_settings(ctx.settings)
    report = _cached_link_report(ctx)
    return {
        "errors": errors, "warnings": warnings, "pack": ctx.settings.pack.get("name"),
        "sources": {n: {"last_test": _last_tests.get(n), **_connection_state(ctx, n, s)} for n, s in ctx.settings.sources.items()},
        "linking": _report_json(report) if report else None, "matching_needed": ctx.engine.linker.needs_matching(),
        "daily": ctx.store.assessment_stats(30), "recent": ctx.store.recent_assessments(20),
        "actions": ctx.store.action_summary(30),
        "model_configured": ctx.engine.ai_enabled(),
    }


def _cached_link_report(ctx):
    """The last account-matching report, without querying any source."""
    try:
        return ctx.engine.linker.report() if ctx.engine.linker.cache_path.exists() else None
    except Exception:  # noqa: BLE001 - health and the checklist never fail because one source is down
        return None


# ---------------------------------------------------------------------------
# setup checklist, demo workspace
# ---------------------------------------------------------------------------

def _checklist(ctx) -> list[dict]:
    """What is left to do, from what is already known — no source is queried."""
    cat = packs.catalog(ctx.settings, "")
    sources = ctx.settings.sources
    missing_sections = [c["label"] for c in cat["categories"] if c.get("required") and not c["configured_sources"]]
    broken = sorted(n for n, s in sources.items() if not _connection_state(ctx, n, s)["credentials_complete"]
                    or (_last_tests.get(n) or {"ok": True})["ok"] is False)
    unmapped = sorted(f"{n}: {signals_mod.label(e)}" for n, s in sources.items() for e, spec in (s.get("entities") or {}).items()
                      if spec and e in model.ENTITIES and missing_required(e, spec))
    report = _cached_link_report(ctx)
    low = sorted(n for n, s in (_report_json(report)["sources"] if report else {}).items() if s["method"] != "same_id" and s["coverage"] < 0.8)
    available = ctx.engine.available_entities()
    with_data = sum(1 for s in ctx.settings.signals if signals_mod.required_entities(s) & available)
    items = [
        ("connect", "Connect your systems", not missing_sections, "still to connect: " + ", ".join(missing_sections), "sources"),
        ("credentials", "Sign in and test every connection", bool(sources) and not broken, "check: " + ", ".join(broken), "sources"),
        ("mapping", "Map the required fields", bool(sources) and not unmapped, "missing required fields — " + "; ".join(unmapped[:4]), "sources"),
        # only when a second system keeps its own customer list
        *([("matching", "Match accounts across systems (80%+)", report is not None and not low, "below 80%: " + ", ".join(low) if low
            else "build the matches on Account matching", "matching")] if ctx.engine.linker.needs_matching() else []),
        ("signals", "At least 5 warning signs have data", with_data >= 5, f"{with_data} have data so far", "signals"),
        ("access", "Give people access", bool((ctx.settings.access or {}).get("users")), "add people on Access & Slack", "access"),
        ("slack", "Connect Slack", bool((ctx.settings.slack or {}).get("bot_token")), "add the Slack app tokens", "access"),
        ("first_answer", "Ask your first question", bool(ctx.store.recent_assessments(1)), "ask in Slack or on the Ask page", "health"),
    ]
    return [{"key": k, "label": label, "done": bool(done), "detail": "" if done else detail, "route": route}
            for k, label, done, detail, route in items]


@router.get("/setup/checklist")
def setup_checklist(admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    items = _checklist(ctx)
    return {"items": items, "done": sum(i["done"] for i in items), "total": len(items), "workspace": workspace.status(ctx)}


class WorkspaceBody(BaseModel):
    name: str


@router.put("/workspace")
def switch_workspace(body: WorkspaceBody, admin: str = Depends(auth.current_admin)) -> dict:
    """Switch between your real setup ("home") and the built-in sample data ("demo")."""
    try:
        return workspace.status(workspace.switch(body.name))
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc


# ---------------------------------------------------------------------------
# accuracy: backtest on closed renewals, rules weights
# ---------------------------------------------------------------------------

class BacktestBody(BaseModel):
    start: str
    end: str
    lead_days: int | None = None
    mode: str = "auto"  # live = what the agent said at the time, replay = rebuilt from history, auto = live when it can


@router.post("/backtest")
def run_backtest(body: BacktestBody, admin: str = Depends(auth.current_admin)) -> dict:
    if body.mode not in ("auto", "live", "replay"):
        raise HTTPException(400, "mode is auto, live or replay")
    ctx = get_context()
    store = config_store(ctx)
    result = backtest.run(ctx.engine, body.start, body.end, body.lead_days, backtest.read_outcomes(store.read_text("outcomes.csv") or ""),
                          mode=body.mode, predictions_for=ctx.store.predictions_for)
    if "error" in result:
        raise HTTPException(400, result["error"])
    result["run_at"] = time.time()
    (ctx.settings.state_dir / "backtest.json").write_text(json.dumps(result), encoding="utf-8")
    return result


@router.get("/backtest")
def last_backtest(admin: str = Depends(auth.current_admin)) -> dict:
    path = get_context().settings.state_dir / "backtest.json"
    last = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return last if "calibration" in last else {}  # a run saved by an older version: run it again


@router.post("/backtest/outcomes")
async def upload_outcomes(file: UploadFile = File(...), admin: str = Depends(auth.current_admin)) -> dict:
    """outcomes.csv: account_id, close_date, outcome (renewed | downgraded | churned) — wins over the CRM's stages."""
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    rows = backtest.read_outcomes(text)
    if not rows:
        raise HTTPException(400, "expected columns account_id, close_date, outcome (renewed, downgraded or churned)")
    store = config_store(get_context())
    result = _apply(lambda: store.write_text("outcomes.csv", text, admin))
    return {**result, "rows": len(rows)}


class ScoringBody(BaseModel):
    weights: dict
    thresholds: dict
    signal_factors: dict | None = None


@router.put("/rules")
def save_rules(body: ScoringBody, admin: str = Depends(auth.current_admin)) -> dict:
    """Apply scoring (e.g. the backtest's suggestion) — versioned in agent.yaml."""
    weights = {k: int(body.weights[k]) for k in ("high", "medium", "low") if k in body.weights}
    thresholds = {k: int(body.thresholds[k]) for k in ("high", "medium") if k in body.thresholds}
    if len(weights) != 3 or len(thresholds) != 2 or not 0 < thresholds["medium"] < thresholds["high"] <= 100:
        raise HTTPException(400, "give weights for high, medium and low, and thresholds 0 < medium < high <= 100")
    ctx = get_context()
    known = {s["name"] for s in ctx.settings.signals}
    factors = {k: round(float(v), 2) for k, v in (body.signal_factors or {}).items() if k in known and 0.25 <= float(v) <= 4}
    store = config_store(ctx)

    def change():
        doc = store.read("agent.yaml")
        rules = {**(doc.get("rules") or {}), "weights": weights, "thresholds": thresholds}
        if body.signal_factors is not None:
            rules["signal_factors"] = factors
        doc["rules"] = rules
        store.write("agent.yaml", doc, admin)
    return _apply(change)


# ---------------------------------------------------------------------------
# AI agents: coordinator -> one specialist per data section -> synthesis
# ---------------------------------------------------------------------------

def _prompt_preview(ctx, key: str, instructions: str) -> str:
    """The full system prompt the agent gets, with these instructions (read-only on the screen)."""
    if key == "coordinator":
        return prompts.coordinator_prompt("<account>", {}, instructions)[0]
    if key == "synthesis":
        return prompts.synthesis_prompt("<account>", None, "<as-of date>", [], ctx.settings.playbooks, instructions)[0]
    spec = ctx.engine.specialists()[key]
    catalog = [x for x in ctx.settings.signals if x.get("specialist") == key]
    return prompts.specialist_prompt(key, instructions, catalog, "<account>", "<as-of date>", {}, {}, label=spec.get("label"))[0]


@router.get("/agents")
def list_agents(admin: str = Depends(auth.current_admin)) -> dict:
    ctx = get_context()
    available = ctx.engine.available_entities()
    own = ctx.settings.agent.get("agents") or {}
    specialists = [{"key": k, "label": v["label"], "entities": v["entities"], "custom": v["custom"],
                    "has_data": bool(set(v["entities"]) & available), "instructions": v["focus"], "default": v["default"],
                    "signals": sorted(x["name"] for x in ctx.settings.signals if x.get("specialist") == k),
                    "preview": _prompt_preview(ctx, k, v["focus"])} for k, v in ctx.engine.specialists().items()]
    agents = {}
    for k in sections_mod.AGENTS:
        default = prompts.DEFAULT_INSTRUCTIONS[k]
        current = (own.get(k) or {}).get("instructions") or default
        agents[k] = {"key": k, "instructions": current, "default": default, "preview": _prompt_preview(ctx, k, current)}
    return {"coordinator": agents["coordinator"], "specialists": specialists, "synthesis": agents["synthesis"],
            "ai_enabled": ctx.engine.ai_enabled()}


class InstructionsBody(BaseModel):
    instructions: str = ""


@router.put("/agents/{key}")
def save_agent(key: str, body: InstructionsBody, admin: str = Depends(auth.current_admin)) -> dict:
    """An agent's instructions; empty = back to the default. The safety rules are not editable."""
    ctx = get_context()
    text = body.instructions.strip()
    if len(text) > 2000:
        raise HTTPException(400, "keep instructions under 2,000 characters")
    spec = ctx.engine.specialists().get(key)
    if key not in sections_mod.AGENTS and spec is None:
        raise HTTPException(404, f"no agent '{key}'")
    if spec and spec["custom"]:  # a custom section's specialist: its focus lives with the section
        section = _section(ctx, key)
        if not text:
            raise HTTPException(400, "a custom section's specialist needs instructions")
        return save_section(key, SectionBody(label=section["label"], description=section["description"],
                                             data_type=section["data_type"], focus=text), admin)
    store = config_store(ctx)

    def change():
        doc = store.read("agent.yaml")
        agents = doc.setdefault("agents", {})
        if text and text != prompts.DEFAULT_INSTRUCTIONS.get(key, (spec or {}).get("default")):
            agents[key] = {"instructions": text}
        else:  # empty or the default: back to the default
            agents.pop(key, None)
        store.write("agent.yaml", doc, admin)
    return _apply(change)


@router.get("/feedback")
def list_feedback(admin: str = Depends(auth.current_admin)) -> dict:
    return {"feedback": get_context().store.feedback()}


class FeedbackBody(BaseModel):
    account_id: str
    verdict: str | None = None   # agree | disagree
    outcome: str | None = None   # renewed | churned | downgraded
    note: str | None = None
    assessment_id: int | None = None


@router.post("/feedback")
def add_feedback(body: FeedbackBody, admin: str = Depends(auth.current_admin)) -> dict:
    if body.verdict not in (None, "agree", "disagree") or body.outcome not in (None, "renewed", "churned", "downgraded"):
        raise HTTPException(400, "verdict: agree|disagree; outcome: renewed|churned|downgraded")
    fid = get_context().store.add_feedback(body.account_id, admin, body.verdict, body.outcome, body.note, body.assessment_id)
    return {"ok": True, "id": fid}
