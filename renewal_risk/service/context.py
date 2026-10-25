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
            from . import workspace  # starts in the workspace that was active when the service stopped
            _ctx = build_context(workspace.startup_settings())
        return _ctx


def set_context(ctx: AppContext | None) -> None:
    global _ctx
    with _lock:
        _ctx = ctx


def reload_context(cfg_dir=None, state_root=None) -> AppContext:
    """Rebuild everything from config after the admin UI saved a change, or from
    another config folder (a workspace switch). The model client (and any
    injected test double) is carried over."""
    global _ctx
    from ..core.settings import load_settings

    with _lock:
        old = _ctx
        if cfg_dir is None and old is not None:
            cfg_dir, state_root = old.settings.dir, old.settings.state_root
        settings = load_settings(cfg_dir, old.settings.tenant_id if old else "default", state_root) if cfg_dir else None
        llm = old.engine.llm if old else None
        _ctx = build_context(settings, llm=llm)
    if old is not None:
        old.engine.close()
    return _ctx
