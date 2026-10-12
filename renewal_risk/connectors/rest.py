"""
rest.py — any JSON HTTP API (Stripe, Zendesk, HubSpot, Freshdesk, Chargebee,
an in-house service...). Most presets in the catalog are built on this.

  type: rest
  connection:
    base_url: https://api.stripe.com
    auth: {type: bearer, token: "${ENV:STRIPE_API_KEY}"}
      # bearer {token} | basic {username, password} | header {name, value}
      # query {name, value} | oauth2_client_credentials {token_url, client_id, client_secret, scope}
      # oauth2 {}  — a user-delegated connection made with Connect in the admin UI (tokens in the secret store)
    headers: {}                 # extra headers
    pagination: {type: stripe}  # default for every request; see _paginate
    timeout: 30
    max_pages: 50
  entities:
    invoice:
      fetch:
        request:
          path: /v1/invoices
          params: {customer: "{link_value}", limit: 100, "created[lte]": "{as_of_epoch}"}
          records_path: data
      fields: {...}

Only GET and POST are allowed; POST exists for search endpoints that take a
JSON body (HubSpot CRM search). PUT / PATCH / DELETE are rejected outright.
"""
from __future__ import annotations

import threading
import time
from urllib.parse import quote

from ..core.connector import Connector, ConnectorError, HealthResult, register, render_template, safe_value
from ..core.mapping import get_path


def _http():
    try:
        import httpx2 as httpx  # the HTTP library the anthropic SDK ships with
    except ImportError:
        try:
            import httpx  # type: ignore[no-redef]
        except ImportError as exc:
            raise ConnectorError("the rest connector needs `pip install httpx2`") from exc
    return httpx


def _plain(value) -> str:
    """Escape for params / JSON bodies: the HTTP client encodes these itself."""
    return "" if value is None else str(value)


def _path_segment(value) -> str:
    return quote(safe_value(value), safe="@:+-._")


@register("rest")
class RestConnector(Connector):
    def __init__(self, name: str, config: dict, transport=None):
        super().__init__(name, config)
        httpx = _http()
        conn = config.get("connection") or {}
        self.base_url = (conn.get("base_url") or "").rstrip("/")
        if not self.base_url:
            raise ConnectorError(f"source '{name}': connection.base_url is required")
        self.auth = conn.get("auth") or {}
        self.default_pagination = conn.get("pagination") or {"type": "none"}
        self.max_pages = int(conn.get("max_pages", 50))
        self._token: tuple[str, float] | None = None
        self._token_lock = threading.Lock()
        self._delegated = None
        if self.auth.get("type") == "oauth2":
            from pathlib import Path

            from ..auth.oauth import TokenManager
            from ..auth.secrets import default_store

            oauth = config.get("oauth") or {}
            if not oauth.get("token_url") or not config.get("_state_dir"):
                raise ConnectorError(f"source '{name}': OAuth is not configured — connect it in the admin UI")
            self._delegated = TokenManager(default_store(Path(config["_state_dir"]), config.get("_tenant_id", "default")), name,
                                           oauth["token_url"], config.get("client_id", ""), config.get("client_secret"), oauth.get("client_auth", "body"))
        kwargs = {"timeout": float(conn.get("timeout", 30)), "headers": {"Accept": "application/json", **(conn.get("headers") or {})}}
        if transport is not None:
            kwargs["transport"] = transport
        if self.auth.get("type") == "basic":
            kwargs["auth"] = (str(self.auth.get("username", "")), str(self.auth.get("password", "")))
        self.client = httpx.Client(**kwargs)

    # -- auth ----------------------------------------------------------------

    def _auth_headers(self) -> dict:
        kind = self.auth.get("type")
        if kind == "bearer":
            return {"Authorization": f"Bearer {self.auth.get('token', '')}"}
        if kind == "header":
            return {self.auth["name"]: str(self.auth.get("value", ""))}
        if kind == "oauth2":
            from ..auth.oauth import OAuthError
            try:
                return {"Authorization": f"Bearer {self._delegated.access_token()}"}
            except OAuthError as exc:
                raise ConnectorError(str(exc)) from exc
        if kind == "oauth2_client_credentials":
            return {"Authorization": f"Bearer {self._oauth_token()}"}
        return {}

    def _auth_params(self) -> dict:
        if self.auth.get("type") == "query":
            return {self.auth["name"]: str(self.auth.get("value", ""))}
        return {}

    def _oauth_token(self) -> str:
        with self._token_lock:
            if self._token and self._token[1] > time.time() + 30:
                return self._token[0]
            data = {"grant_type": "client_credentials", "client_id": self.auth.get("client_id"), "client_secret": self.auth.get("client_secret")}
            if self.auth.get("scope"):
                data["scope"] = self.auth["scope"]
            resp = self.client.post(self.auth["token_url"], data=data)
            if resp.status_code >= 400:
                raise ConnectorError(f"source '{self.name}': OAuth token request failed ({resp.status_code})")
            body = resp.json()
            self._token = (body["access_token"], time.time() + float(body.get("expires_in", 3600)))
            return self._token[0]

    # -- requests ------------------------------------------------------------

    def _send(self, method: str, url: str, params: dict | None, body):
        for attempt in range(4):
            try:
                resp = self.client.request(
                    method, url, params={**self._auth_params(), **(params or {})},
                    json=body, headers=self._auth_headers(),
                )
            except Exception as exc:  # noqa: BLE001 - network errors
                if attempt == 3:
                    raise ConnectorError(f"source '{self.name}': request to {url} failed: {exc}") from exc
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code == 401 and self._delegated is not None and attempt == 0:
                self._delegated.access_token(force_refresh=True)  # expired access token: refresh once
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == 3:
                    break
                wait = resp.headers.get("retry-after")
                time.sleep(min(float(wait), 30) if wait and wait.replace(".", "").isdigit() else 2 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise ConnectorError(f"source '{self.name}': {method} {url} -> HTTP {resp.status_code}: {resp.text[:300]}")
            try:
                return resp.json()
            except ValueError as exc:
                raise ConnectorError(f"source '{self.name}': {url} did not return JSON") from exc
        raise ConnectorError(f"source '{self.name}': {method} {url} kept failing (HTTP {resp.status_code})")

    @staticmethod
    def _records(body, records_path: str | None) -> list[dict]:
        data = body if records_path in (None, "", "$") else get_path(body, records_path)
        if data is None:
            return []
        if isinstance(data, dict):
            return [data]
        return [r for r in data if isinstance(r, dict)]

    def execute(self, request: dict, ctx: dict, mode: str) -> list[dict]:
        spec = request.get("request", request)
        method = str(spec.get("method", "GET")).upper()
        if method not in ("GET", "POST"):
            raise ConnectorError(f"source '{self.name}': HTTP {method} is not allowed (read-only connector)")
        path = render_template(spec.get("path", ""), ctx, escape=_path_segment)
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        params = render_template(spec.get("params") or {}, ctx, escape=_plain)
        body = render_template(spec.get("json"), ctx, escape=_plain) if spec.get("json") is not None else None
        pagination = spec.get("pagination") or self.default_pagination
        limit = int(spec.get("max_records", 1000 if mode != "list" else 10000))
        return self._paginate(method, url, params, body, spec.get("records_path"), pagination, limit)

    def _paginate(self, method, url, params, body, records_path, pagination, limit) -> list[dict]:
        kind = pagination.get("type", "none")
        out: list[dict] = []
        page_number = int(pagination.get("start", 1))
        offset = 0
        for _ in range(self.max_pages):
            payload = self._send(method, url, params, body)
            batch = self._records(payload, records_path)
            out.extend(batch)
            if len(out) >= limit or not batch:
                break
            if kind == "stripe":
                if not get_path(payload, pagination.get("has_more_path", "has_more")):
                    break
                params = {**params, pagination.get("param", "starting_after"): batch[-1].get("id")}
            elif kind == "next_url":
                nxt = get_path(payload, pagination.get("next_path", "next"))
                if not nxt:
                    break
                url, params = nxt, {}
            elif kind == "cursor":
                cursor = get_path(payload, pagination["cursor_path"])
                if not cursor:
                    break
                if pagination.get("in", "query") == "body" and body is not None:
                    body = {**body, pagination["param"]: cursor}
                else:
                    params = {**params, pagination["param"]: cursor}
            elif kind == "page":
                size = int(pagination.get("page_size", len(batch)))
                if len(batch) < size:
                    break
                page_number += 1
                params = {**params, pagination.get("param", "page"): page_number}
            elif kind == "offset":
                offset += len(batch)
                params = {**params, pagination.get("param", "offset"): offset}
            else:
                break
        return out[:limit]

    def field_metadata(self, entity: str) -> dict[str, dict]:
        """From the entity's optional `describe:` block, e.g. HubSpot
        /crm/v3/properties/<object>, Zendesk /api/v2/ticket_fields, Jira /rest/api/3/field:
          describe: {request: {...}, name: name, label: label, description: description,
                     type: type, options: options, path_prefix: "properties."}"""
        out: dict[str, dict] = {}
        for block in self._blocks(entity):
            spec = block.get("describe")
            if not spec:
                continue
            for item in self.execute(spec, {}, "list"):
                name = get_path(item, spec.get("name", "name"))
                if not name:
                    continue
                options = get_path(item, spec.get("options", "options")) or []
                values = [str(get_path(o, spec.get("option_value", "label")) or get_path(o, "value") or o) for o in options if o][:20] if isinstance(options, list) else []
                out[f"{spec.get('path_prefix', '')}{name}"] = {
                    "label": get_path(item, spec.get("label", "label")), "description": get_path(item, spec.get("description", "description")),
                    "type": get_path(item, spec.get("type", "type")), "options": values,
                }
        return out

    def health(self) -> HealthResult:
        check = (self.config.get("connection") or {}).get("health_path")
        if check:
            try:
                self._send("GET", f"{self.base_url}{check}", {}, None)
                return HealthResult(True, f"GET {check} OK")
            except ConnectorError as exc:
                return HealthResult(False, str(exc))
        return super().health()

    def close(self) -> None:
        self.client.close()
