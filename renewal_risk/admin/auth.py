"""
auth.py — who may use the admin UI.

Free implementation: local admin accounts (scrypt hashes in the store) and a
signed, HttpOnly, SameSite=Strict session cookie. An API key whose access.yaml
user has the `admin` permission also works (automation, CI). A hosted edition
swaps in OIDC SSO behind the same `current_admin` dependency.

First run: while no admin account exists, a one-time setup token is printed
to the server console; creating the first admin requires it, so nobody who
merely reaches the port can claim the install.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from fastapi import HTTPException, Request

COOKIE = "rr_admin"
SESSION_SECONDS = 12 * 3600


def _key(secret_store) -> bytes:
    key = secret_store.get("admin/session_key")
    if not key:
        key = secrets.token_hex(32)
        secret_store.set("admin/session_key", key)
    return key.encode()


def issue_session(secret_store, email: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"email": email, "exp": int(time.time()) + SESSION_SECONDS}).encode()).decode()
    sig = hmac.new(_key(secret_store), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session(secret_store, token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, hmac.new(_key(secret_store), payload.encode(), hashlib.sha256).hexdigest()):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except (ValueError, TypeError):
        return None
    return data["email"] if data.get("exp", 0) > time.time() else None


def setup_token(secret_store, store) -> str | None:
    """The one-time first-run token (None once an admin exists)."""
    if store.admin_count() > 0:
        secret_store.delete("admin/setup_token")
        return None
    token = secret_store.get("admin/setup_token")
    if not token:
        token = secrets.token_urlsafe(12)
        secret_store.set("admin/setup_token", token)
    return token


def current_admin(request: Request) -> str:
    """FastAPI dependency: the signed-in admin's email, or 401."""
    from ..service.context import get_context
    from .api import secret_store_for

    ctx = get_context()
    email = verify_session(secret_store_for(ctx), request.cookies.get(COOKIE))
    if email:
        return email
    key = request.headers.get("x-api-key")
    principal = ctx.policy.principal_for_api_key(key) if key else None
    if principal is not None and principal.can("admin"):
        return principal.email
    raise HTTPException(status_code=401, detail="Sign in to the admin UI")
