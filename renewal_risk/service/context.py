"""context.py — builds the shared objects once per process."""
from __future__ import annotations

import threading
from dataclasses import dataclass

from ..access.rbac import AccessPolicy
from ..core.pipeline import Engine
from ..core.settings import Settings, get_settings
from .risk_service import RiskService
from .store import Store


@dataclass
class AppContext:
    settings: Settings
    engine: Engine
    store: Store
    policy: AccessPolicy
    service: RiskService


_ctx: AppContext | None = None
_lock = threading.Lock()


def build_context(settings: Settings | None = None, llm=None) -> AppContext:
    settings = settings or get_settings()
    store_path = (settings.agent.get("store") or {}).get("path") or (settings.state_dir / "renewal_risk.sqlite")
    store = Store(store_path, settings.tenant_id)
    engine = Engine(settings, llm=llm, store=store)
    policy = AccessPolicy(settings.access)
    return AppContext(settings, engine, store, policy, RiskService(engine, store, policy))


def get_context() -> AppContext:
    global _ctx
    with _lock:
        if _ctx is None:
            _ctx = build_context()
        return _ctx


def set_context(ctx: AppContext | None) -> None:
    global _ctx
    with _lock:
        _ctx = ctx


def reload_context() -> AppContext:
    """Rebuild everything from config after the admin UI saved a change. The
    model client (and any injected test double) is carried over."""
    global _ctx
    from ..core.settings import load_settings

    with _lock:
        old = _ctx
        settings = load_settings(old.settings.dir, old.settings.tenant_id) if old else None
        llm = old.engine.llm if old else None
        _ctx = build_context(settings, llm=llm)
    if old is not None:
        old.engine.close()
    return _ctx
