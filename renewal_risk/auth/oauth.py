"""
oauth.py — OAuth 2.0 authorization-code flow (with PKCE) for connecting
sources from the admin UI, and token refresh at request time.

Free route: the customer registers their own OAuth app in each system (a
Salesforce External Client App, a HubSpot / Zendesk OAuth client...) and pastes
its client id + secret into the UI, together with our callback URL. Later, a
hosted edition can ship its own registered apps — only `client_id` /
`client_secret` change, not this flow.

  start_flow()   -> authorize URL (state + PKCE verifier kept in the secret store)
  finish_flow()  -> exchanges the code, stores the token bundle under
                    `sources/<source>/oauth`
  TokenManager   -> a valid access token for a connector, refreshing it when
                    it is about to expire (or after a 401)
"""
from __future__ import annotations

import base64
import hashlib
import secrets as pysecrets
import threading
import time
from urllib.parse import urlencode

from .secrets import SecretStore

STATE_TTL_SECONDS = 600


class OAuthError(Exception):
    pass


def _http(transport=None):
    import httpx2

    return httpx2.Client(timeout=30, transport=transport) if transport else httpx2.Client(timeout=30)


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(pysecrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def token_key(source: str) -> str:
    return f"sources/{source}/oauth"


def start_flow(store: SecretStore, source: str, oauth: dict, client_id: str, redirect_uri: str) -> str:
    """oauth: {authorize_url, token_url, scopes?, pkce?, extra_authorize_params?}"""
    if not oauth.get("authorize_url") or not client_id:
        raise OAuthError("authorize_url and client_id are required")
    state = pysecrets.token_urlsafe(24)
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": state}
    if oauth.get("scopes"):
        params["scope"] = oauth["scopes"]
    verifier = None
    if oauth.get("pkce", True):
        verifier, challenge = pkce_pair()
        params.update({"code_challenge": challenge, "code_challenge_method": "S256"})
    params.update(oauth.get("extra_authorize_params") or {})
    store.set(f"oauth_state/{state}", {"source": source, "verifier": verifier, "redirect_uri": redirect_uri, "created": time.time()})
    sep = "&" if "?" in oauth["authorize_url"] else "?"
    return f"{oauth['authorize_url']}{sep}{urlencode(params)}"


def pending_source(store: SecretStore, state: str) -> str | None:
    data = store.get(f"oauth_state/{state}")
    return data["source"] if data else None


def _token_request(token_url: str, data: dict, client_id: str, client_secret: str | None, client_auth: str, transport=None) -> dict:
    data = {k: v for k, v in data.items() if v is not None}
    auth = None
    if client_auth == "basic" and client_secret:
        auth = (client_id, client_secret)
    else:
        data["client_id"] = client_id
        if client_secret:
            data["client_secret"] = client_secret
    with _http(transport) as client:
        resp = client.post(token_url, data=data, auth=auth, headers={"Accept": "application/json"})
    try:
        body = resp.json()
    except ValueError:
        body = {"error": resp.text[:200]}
    if resp.status_code >= 400 or "access_token" not in body:
        raise OAuthError(f"token endpoint returned {resp.status_code}: {body.get('error_description') or body.get('error') or body}")
    return body


def _bundle(body: dict, previous: dict | None = None) -> dict:
    bundle = dict(previous or {})
    bundle.update({k: v for k, v in body.items() if k not in ("expires_in",)})
    if "expires_in" in body:
        bundle["expires_at"] = time.time() + float(body["expires_in"])
    elif "expires_at" not in body:
        bundle.pop("expires_at", None)  # e.g. Salesforce: no expiry given; refresh on 401
    bundle["obtained_at"] = time.time()
    return bundle


def finish_flow(store: SecretStore, state: str, code: str, oauth: dict, client_id: str, client_secret: str | None, transport=None) -> tuple[str, dict]:
    pending = store.get(f"oauth_state/{state}")
    store.delete(f"oauth_state/{state}")
    if not pending:
        raise OAuthError("unknown or already-used OAuth state — start the connection again")
    if time.time() - pending["created"] > STATE_TTL_SECONDS:
        raise OAuthError("the sign-in took longer than 10 minutes — start the connection again")
    body = _token_request(oauth["token_url"], {
        "grant_type": "authorization_code", "code": code, "redirect_uri": pending["redirect_uri"],
        "code_verifier": pending.get("verifier"),
    }, client_id, client_secret, oauth.get("client_auth", "body"), transport)
    bundle = _bundle(body)
    store.set(token_key(pending["source"]), bundle)
    return pending["source"], bundle


class TokenManager:
    """Gives connectors a current access token for one source."""

    def __init__(self, store: SecretStore, source: str, token_url: str, client_id: str, client_secret: str | None,
                 client_auth: str = "body", transport=None):
        self.store = store
        self.key = token_key(source)
        self.source = source
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.client_auth = client_auth
        self.transport = transport
        self._lock = threading.Lock()

    def bundle(self, force_refresh: bool = False) -> dict:
        with self._lock:
            bundle = self.store.get(self.key)
            if not bundle:
                raise OAuthError(f"source '{self.source}' is not connected — use Connect in the admin UI")
            expiring = bundle.get("expires_at") and bundle["expires_at"] - 60 < time.time()
            if force_refresh or expiring:
                if not bundle.get("refresh_token"):
                    raise OAuthError(f"source '{self.source}': access token expired and no refresh token — reconnect it")
                body = _token_request(self.token_url, {"grant_type": "refresh_token", "refresh_token": bundle["refresh_token"]},
                                      self.client_id, self.client_secret, self.client_auth, self.transport)
                bundle = _bundle(body, previous=bundle)  # keeps the refresh token when the server doesn't rotate it
                self.store.set(self.key, bundle)
            return bundle

    def access_token(self, force_refresh: bool = False) -> str:
        return self.bundle(force_refresh)["access_token"]
