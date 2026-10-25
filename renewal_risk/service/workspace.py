"""
workspace.py — a demo workspace beside the real setup, one click away.

The demo is the CSV demo (examples/csv-demo) installed into
<state>/workspaces/demo/ with dates relative to today. Only the config folder
changes on a switch: state (admin sign-in, secrets, the store) is shared, so
nobody is logged out, and the real config is never touched. The people in the
real access.yaml are added to the demo's, and slack.yaml is copied, so the
same Slack app answers about the demo accounts. Leaving the demo forgets what
was recorded about its accounts.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import os
import shutil
from datetime import date
from pathlib import Path

import yaml

from ..core.settings import Settings, config_dir, load_settings, read_yaml, state_dir
from .context import AppContext, get_context, reload_context

# The sample data: the repository's CSV demo (the Docker image sets RENEWAL_RISK_DEMO_DIR).
DEMO_SOURCE = Path(os.environ.get("RENEWAL_RISK_DEMO_DIR") or Path(__file__).resolve().parents[2] / "examples" / "csv-demo")


def _file(state: Path) -> Path:
    return state / "workspace.json"


def _read(state: Path) -> dict:
    try:
        return json.loads(_file(state).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def demo_dir(state: Path) -> Path:
    return state / "workspaces" / "demo" / "config"


def startup_settings() -> Settings:
    """Settings for the workspace that was active when the service last ran."""
    home = config_dir()
    state = state_dir(home)
    return load_settings(demo_dir(state), state_root=state) if _read(state).get("active") == "demo" else load_settings(home)


def status(ctx: AppContext) -> dict:
    return {"active": "demo" if ctx.settings.state_root else "home", "demo_available": DEMO_SOURCE.exists()}


def _demo_accounts(cfg: Path) -> list[str]:
    with open(cfg.parent / "data" / "crm" / "accounts.csv", encoding="utf-8") as f:
        return [row["Id"] for row in csv.DictReader(f)]


def _install(home: Path, target: Path) -> None:
    """Copy the demo config, build its data for today, and give it the real people and Slack app."""
    if not DEMO_SOURCE.exists():
        raise FileNotFoundError("the sample data (examples/csv-demo) is not installed with this service")
    shutil.copytree(DEMO_SOURCE / "config", target, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("links.csv", "slack.yaml", "signals.yaml", "uploads", "mappings", "*.tmp"))
    spec = importlib.util.spec_from_file_location("csv_demo_generate", DEMO_SOURCE / "generate.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    generator.build(target.parent / "data", date.today())
    access = read_yaml(target / "access.yaml")
    known = {u.get("email") for u in access.get("users") or []}
    access["users"] = (access.get("users") or []) + [u for u in read_yaml(home / "access.yaml").get("users") or [] if u.get("email") not in known]
    (target / "access.yaml").write_text(yaml.safe_dump(access, sort_keys=False), encoding="utf-8")
    if (home / "slack.yaml").exists():
        shutil.copy2(home / "slack.yaml", target / "slack.yaml")


def switch(name: str) -> AppContext:
    """Make `name` ("demo" or "home") the active workspace."""
    ctx = get_context()
    state = ctx.settings.state_dir
    saved = _read(state)
    home = Path(saved.get("home") or ctx.settings.dir) if ctx.settings.state_root else ctx.settings.dir
    if name == "demo":
        target = demo_dir(state)
        _install(home, target)  # refreshed on every switch, so its dates stay current
        _file(state).write_text(json.dumps({"active": "demo", "home": str(home)}), encoding="utf-8")
        return reload_context(target, state)
    if name != "home":
        raise ValueError("workspace must be 'demo' or 'home'")
    if ctx.settings.state_root:
        ctx.store.purge_accounts(_demo_accounts(ctx.settings.dir))
    _file(state).write_text(json.dumps({"active": "home", "home": str(home)}), encoding="utf-8")
    return reload_context(home, None)
