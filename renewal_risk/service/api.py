"""
api.py — REST API (FastAPI). Run with `renewal-risk serve`.

Auth: `X-API-Key: <key>` (or `Authorization: Bearer <key>`), keys mapped to a
user in access.yaml `api_keys`. For a single-user local setup only, set
RENEWAL_RISK_DEV_USER=<email from access.yaml> to skip the key.

  GET  /v1/health                          liveness (no auth)
  POST /v1/ask            {question}       plain-English question
  GET  /v1/accounts/search?q=              find accounts you can see
  GET  /v1/accounts/{id}/assessment        stored (fresh) or new assessment; ?fresh=true forces a new one
  GET  /v1/renewals?start=&end=&band=      renewals in a window, screened + riskiest assessed
  GET  /v1/connectors/test                 health of every source (admin)
  GET  /                                   minimal web page for asking questions
  /admin/ + /admin/api/*                   admin UI and its API (see renewal_risk/admin/api.py)
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__
from ..access.rbac import AccessDenied, Principal, can_view
from ..core.connector import ConnectorError
from ..core.llm import LLMError
from ..core.pipeline import AccountLookupError, PolicyError
from .context import get_context

app = FastAPI(title="Renewal Risk Agent", version=__version__)
_STATIC = Path(__file__).parent / "static"


def current_principal(x_api_key: str | None = Header(default=None), authorization: str | None = Header(default=None)) -> Principal:
    ctx = get_context()
    key = x_api_key or (authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None)
    principal = ctx.policy.principal_for_api_key(key)
    if principal is None and not key and os.environ.get("RENEWAL_RISK_DEV_USER"):
        principal = ctx.policy.principal_for_email(os.environ["RENEWAL_RISK_DEV_USER"])
    if principal is None:
        raise HTTPException(status_code=401, detail="Missing or unknown API key")
    return principal


def _errors(fn):
    try:
        return fn()
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except AccountLookupError as exc:
        raise HTTPException(status_code=404, detail={"message": str(exc), "candidates": exc.candidates}) from exc
    except PolicyError as exc:
        raise HTTPException(status_code=412, detail=str(exc)) from exc
    except (ConnectorError, LLMError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class AskBody(BaseModel):
    question: str


@app.get("/v1/health")
def health() -> dict:
    ctx = get_context()
    return {"status": "ok", "version": __version__, "sources": list(ctx.settings.sources), "primary": ctx.settings.primary,
            "config_problems": len(ctx.settings.problems)}


@app.post("/v1/ask")
def ask(body: AskBody, principal: Principal = Depends(current_principal)) -> dict:
    return get_context().service.ask(principal, body.question)


@app.get("/v1/accounts/search")
def search(q: str = Query(min_length=1), principal: Principal = Depends(current_principal)) -> dict:
    def run():
        principal.require("view")
        svc = get_context().service
        hits = svc.engine.primary().search("account", q)
        visible = [h for h in hits if can_view(principal, svc.account(h["id"]) or h)]
        return {"results": [{"account_id": h["id"], "name": h.get("name")} for h in visible[:25]]}
    return _errors(run)


@app.get("/v1/accounts/{account_id}/assessment")
def assessment(account_id: str, fresh: bool = False, as_of: str | None = None, principal: Principal = Depends(current_principal)) -> dict:
    return _errors(lambda: get_context().service.account_assessment(principal, account_id, fresh=fresh, as_of=as_of))


@app.get("/v1/renewals")
def renewals(start: date | None = None, end: date | None = None, band: str = "any", deep: bool = True,
             principal: Principal = Depends(current_principal)) -> dict:
    start = start or date.today()
    end = end or start + timedelta(days=90)
    return _errors(lambda: get_context().service.portfolio(principal, start, end, band, deep=deep))


@app.get("/v1/connectors/test")
def connectors_test(principal: Principal = Depends(current_principal)) -> dict:
    def run():
        principal.require("admin")
        ctx = get_context()
        out = {}
        for name in ctx.settings.sources:
            try:
                result = ctx.engine.pool.get(name).health()
                out[name] = {"ok": result.ok, "detail": result.detail}
            except Exception as exc:  # noqa: BLE001
                out[name] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        return out
    return _errors(run)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (_STATIC / "index.html").read_text(encoding="utf-8")


# --- admin UI ---------------------------------------------------------------
from ..admin.api import callback_router, router as admin_router  # noqa: E402

app.include_router(admin_router)
app.include_router(callback_router)
_ADMIN_UI = Path(__file__).resolve().parent.parent / "admin" / "static"


@app.get("/admin", include_in_schema=False)
def admin_redirect() -> RedirectResponse:
    return RedirectResponse("/admin/")


if (_ADMIN_UI / "index.html").exists():
    app.mount("/admin/", StaticFiles(directory=_ADMIN_UI, html=True), name="admin-ui")
else:
    @app.get("/admin/", response_class=HTMLResponse, include_in_schema=False)
    def admin_not_built() -> str:
        return ("<p>The admin UI has not been built. Run <code>npm install &amp;&amp; npm run build</code> in <code>web/</code> "
                "(the Docker image does this automatically).</p>")
