"""
salesforce_rest.py — Salesforce over its REST API with an OAuth connection made
in the admin UI (no `sf` CLI or Node needed on the server).

The admin creates an External Client App / Connected App in their own org
(free), with our callback URL and scopes `api refresh_token`, then clicks
Connect. The token bundle (access token, refresh token, instance_url) lives in
the secret store; this connector reads it per request and refreshes on 401.

SOQL building, the read-only and as-of guards and FieldDefinition metadata are
shared with the MCP transport (salesforce.SoqlConnector). On top of that,
REST describe adds picklist values for the mapping agent.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from ..auth.oauth import OAuthError, TokenManager
from ..auth.secrets import default_store
from ..core.connector import ConnectorError, HealthResult, register
from .rest import _http
from .salesforce import SoqlConnector, block_dml

API_VERSION = "v62.0"


@register("salesforce_rest")
class SalesforceRestConnector(SoqlConnector):
    def __init__(self, name: str, config: dict, token_manager: TokenManager | None = None, transport=None):
        super().__init__(name, config)
        oauth = config.get("oauth") or {}
        if token_manager is None:
            if not oauth.get("token_url") or not config.get("client_id"):
                raise ConnectorError(f"source '{name}': OAuth client_id and token_url are required — connect it in the admin UI")
            store = default_store(Path(config["_state_dir"]), config.get("_tenant_id", "default")) if config.get("_state_dir") else None
            if store is None:
                raise ConnectorError(f"source '{name}': no secret store available for OAuth tokens")
            token_manager = TokenManager(store, name, oauth["token_url"], config["client_id"], config.get("client_secret"),
                                         oauth.get("client_auth", "body"))
        self.tokens = token_manager
        self.api_version = config.get("api_version", API_VERSION)
        httpx = _http()
        self.client = httpx.Client(timeout=float(config.get("timeout", 60)), transport=transport) if transport else httpx.Client(timeout=float(config.get("timeout", 60)))

    def _get(self, path_or_url: str):
        for attempt in (1, 2):
            try:
                bundle = self.tokens.bundle(force_refresh=attempt == 2)
            except OAuthError as exc:
                raise ConnectorError(str(exc)) from exc
            base = bundle.get("instance_url") or self.config.get("instance_url")
            if not base:
                raise ConnectorError(f"source '{self.name}': no instance_url in the OAuth token — reconnect it")
            url = path_or_url if path_or_url.startswith("http") else f"{base.rstrip('/')}{path_or_url}"
            resp = self.client.get(url, headers={"Authorization": f"Bearer {bundle['access_token']}", "Accept": "application/json"})
            if resp.status_code == 401 and attempt == 1:
                continue  # expired session: refresh once and retry
            if resp.status_code >= 400:
                raise ConnectorError(f"source '{self.name}': Salesforce returned HTTP {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        raise ConnectorError(f"source '{self.name}': Salesforce rejected the refreshed token")

    def run_soql(self, soql: str) -> list[dict]:
        body = self._get(f"/services/data/{self.api_version}/query?q={quote(block_dml(soql))}")
        records = list(body.get("records", []))
        pages = 0
        while not body.get("done", True) and body.get("nextRecordsUrl") and pages < 50:
            body = self._get(body["nextRecordsUrl"])
            records.extend(body.get("records", []))
            pages += 1
        return records

    def object_fields(self, obj: str) -> dict[str, dict]:
        out = super().object_fields(obj)  # admin-written descriptions from FieldDefinition
        described = self._get(f"/services/data/{self.api_version}/sobjects/{quote(obj)}/describe")
        for f in described.get("fields", []):
            info = out.setdefault(f["name"], {})
            info.setdefault("label", f.get("label"))
            info.setdefault("type", f.get("type"))
            if f.get("inlineHelpText") and not info.get("description"):
                info["description"] = f["inlineHelpText"]
            values = [p.get("value") for p in f.get("picklistValues") or [] if p.get("active", True)]
            if values:
                info["options"] = values[:20]
        return out

    def health(self) -> HealthResult:
        try:
            self.run_soql("SELECT Id FROM Account LIMIT 1")
            return HealthResult(True, "connected to Salesforce (OAuth)")
        except Exception as exc:  # noqa: BLE001
            return HealthResult(False, f"{type(exc).__name__}: {exc}")

    def close(self) -> None:
        self.client.close()
