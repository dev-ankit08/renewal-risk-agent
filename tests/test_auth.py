"""Secret store, ${SECRET:} config references, OAuth flow + refresh, Salesforce REST connector."""
import json
import sqlite3
import time
from urllib.parse import parse_qs, urlparse

import pytest

from renewal_risk.auth import oauth
from renewal_risk.auth.secrets import LocalSecretStore, SecretStoreError


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("RENEWAL_RISK_MASTER_KEY", raising=False)
    return LocalSecretStore(tmp_path / "secrets.sqlite")


def test_secret_store_encrypts_and_round_trips(store, tmp_path):
    store.set("sources/billing/api_key", "rk_live_supersecret")
    store.set("sources/crm/oauth", {"access_token": "a", "refresh_token": "r"})
    assert store.get("sources/billing/api_key") == "rk_live_supersecret"
    assert store.get("sources/crm/oauth")["refresh_token"] == "r"
    raw = sqlite3.connect(tmp_path / "secrets.sqlite").execute("SELECT value FROM secrets").fetchall()
    assert all(b"supersecret" not in bytes(r[0]) for r in raw)
    assert store.keys("sources/") == ["sources/billing/api_key", "sources/crm/oauth"]
    store.delete_prefix("sources/crm/")
    assert store.get("sources/crm/oauth") is None
    assert (tmp_path / "master.key").exists()


def test_wrong_master_key_is_reported(store, tmp_path):
    from cryptography.fernet import Fernet

    store.set("k", "v")
    other = LocalSecretStore(tmp_path / "secrets.sqlite", key=Fernet.generate_key())
    with pytest.raises(SecretStoreError, match="MASTER_KEY"):
        other.get("k")


def test_secret_references_resolve_in_config(tmp_path, monkeypatch):
    from renewal_risk.auth.secrets import default_store
    from renewal_risk.core.settings import load_settings
    from renewal_risk.core.validate import validate_settings

    monkeypatch.setenv("RENEWAL_RISK_STATE_DIR", str(tmp_path / "state"))
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "connectors.yaml").write_text(
        "sources:\n  billing:\n    preset: stripe\n    api_key: ${SECRET:sources/billing/api_key}\n", encoding="utf-8")
    errors, _ = validate_settings(load_settings(cfg))
    assert any("credentials not entered yet" in e and "sources/billing/api_key" in e for e in errors)
    default_store(tmp_path / "state").set("sources/billing/api_key", "rk_test_123")
    settings = load_settings(cfg)
    assert settings.sources["billing"]["connection"]["auth"]["token"] == "rk_test_123"
    assert "rk_test_123" not in (cfg / "connectors.yaml").read_text(encoding="utf-8")


def _token_server(calls):
    import httpx2

    def handler(request):
        form = parse_qs(request.content.decode())
        calls.append(form)
        if form["grant_type"] == ["authorization_code"]:
            assert form["code_verifier"][0]  # PKCE verifier sent back
            return httpx2.Response(200, json={"access_token": "at-1", "refresh_token": "rt-1", "instance_url": "https://acme.my.salesforce.com", "expires_in": 3600})
        return httpx2.Response(200, json={"access_token": f"at-{len(calls)}", "instance_url": "https://acme.my.salesforce.com"})
    return httpx2.MockTransport(handler)


def test_oauth_flow_with_pkce_and_refresh(store):
    cfg = {"authorize_url": "https://login.salesforce.com/services/oauth2/authorize", "token_url": "https://login.salesforce.com/services/oauth2/token", "scopes": "api refresh_token"}
    url = oauth.start_flow(store, "crm", cfg, "client-123", "http://localhost:8080/admin/oauth/callback")
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == ["client-123"] and q["code_challenge_method"] == ["S256"] and q["scope"] == ["api refresh_token"]
    state = q["state"][0]
    assert oauth.pending_source(store, state) == "crm"

    calls = []
    transport = _token_server(calls)
    source, bundle = oauth.finish_flow(store, state, "the-code", cfg, "client-123", "secret", transport=transport)
    assert source == "crm" and bundle["access_token"] == "at-1"
    with pytest.raises(oauth.OAuthError, match="already-used"):
        oauth.finish_flow(store, state, "the-code", cfg, "client-123", "secret", transport=transport)

    tm = oauth.TokenManager(store, "crm", cfg["token_url"], "client-123", "secret", transport=transport)
    assert tm.access_token() == "at-1"  # still valid
    stored = store.get(oauth.token_key("crm"))
    stored["expires_at"] = time.time() - 10
    store.set(oauth.token_key("crm"), stored)
    assert tm.access_token() == "at-2"  # refreshed
    assert store.get(oauth.token_key("crm"))["refresh_token"] == "rt-1"  # kept when not rotated
    assert calls[-1]["grant_type"] == ["refresh_token"]


def test_oauth_state_expires(store, monkeypatch):
    cfg = {"authorize_url": "https://x/auth", "token_url": "https://x/token"}
    state = parse_qs(urlparse(oauth.start_flow(store, "s", cfg, "c", "http://cb")).query)["state"][0]
    pending = store.get(f"oauth_state/{state}")
    pending["created"] -= 3600
    store.set(f"oauth_state/{state}", pending)
    with pytest.raises(oauth.OAuthError, match="10 minutes"):
        oauth.finish_flow(store, state, "code", cfg, "c", None)


def test_salesforce_rest_connector_paginates_refreshes_and_describes(store):
    import httpx2

    from renewal_risk.connectors.salesforce_rest import SalesforceRestConnector

    store.set(oauth.token_key("crm"), {"access_token": "expired", "refresh_token": "rt", "instance_url": "https://acme.my.salesforce.com"})
    token_calls = []
    tm = oauth.TokenManager(store, "crm", "https://login/token", "cid", "sec", transport=_token_server(token_calls))
    seen = []

    def api(request):
        seen.append(request)
        if request.headers["authorization"] == "Bearer expired":
            return httpx2.Response(401, json=[{"errorCode": "INVALID_SESSION_ID"}])
        path = request.url.path
        if path.endswith("/query"):
            q = request.url.params["q"]
            if "FieldDefinition" in q:
                return httpx2.Response(200, json={"done": True, "records": [
                    {"QualifiedApiName": "Renewal_Date__c", "Label": "Renewal Date", "DataType": "Date", "Description": "Date the renewal is due"}]})
            return httpx2.Response(200, json={"done": False, "nextRecordsUrl": "/services/data/v62.0/query/01g-2000",
                                              "records": [{"attributes": {"type": "Opportunity"}, "Id": "006A", "Amount": 10}]})
        if path.endswith("/query/01g-2000"):
            return httpx2.Response(200, json={"done": True, "records": [{"Id": "006B", "Amount": 20}]})
        if path.endswith("/describe"):
            return httpx2.Response(200, json={"fields": [{"name": "StageName", "label": "Stage", "type": "picklist",
                                                          "picklistValues": [{"value": "Prospecting", "active": True}, {"value": "Closed Won", "active": True}]}]})
        return httpx2.Response(404, json={})

    conn = SalesforceRestConnector("crm", {"type": "salesforce_rest", "entities": {"renewal": {
        "fetch": {"object": "Opportunity", "account_field": "AccountId"}, "fields": {"id": "Id", "amount": "Amount"}}}},
        token_manager=tm, transport=httpx2.MockTransport(api))
    rows = conn.fetch("renewal", "001X", "2026-05-01")
    assert [r["amount"] for r in rows] == [10, 20]
    assert token_calls and token_calls[0]["grant_type"] == ["refresh_token"]  # 401 -> refresh -> retry
    soql = seen[1].url.params["q"]
    assert "CreatedDate <= 2026-05-01T23:59:59Z" in soql and "AccountId = '001X'" in soql
    meta = conn.field_metadata("renewal")
    assert meta["Renewal_Date__c"]["description"] == "Date the renewal is due"
    assert meta["StageName"]["options"] == ["Prospecting", "Closed Won"]
    assert json.dumps(meta)


def test_preset_auth_method_selection(tmp_path, monkeypatch):
    from renewal_risk.core.settings import load_settings

    monkeypatch.setenv("RENEWAL_RISK_STATE_DIR", str(tmp_path / "state"))
    cfg = tmp_path / "c"
    cfg.mkdir()
    (cfg / "connectors.yaml").write_text(
        "sources:\n"
        "  old: {preset: salesforce, org_alias: sandbox}\n"                     # v1 config: picks the sf CLI method
        "  new: {preset: salesforce, auth: {method: oauth2}, client_id: abc, login_host: test.salesforce.com}\n", encoding="utf-8")
    s = load_settings(cfg)
    assert s.sources["old"]["type"] == "salesforce" and s.sources["old"]["auth_method"] == "cli"
    assert s.sources["new"]["type"] == "salesforce_rest"
    assert s.sources["new"]["oauth"]["token_url"] == "https://test.salesforce.com/services/oauth2/token"
