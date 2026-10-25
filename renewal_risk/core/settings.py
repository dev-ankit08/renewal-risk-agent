"""
settings.py — loads everything an admin configures.

Config directory (default ./config, or RENEWAL_RISK_CONFIG_DIR):

  connectors.yaml       sources, primary source, linking rules        (required)
  mappings/<src>.overrides.yaml   per-source field-mapping overrides  (optional)
  agent.yaml            models, specialists, schedule, bulk limits    (optional, merged over defaults)
  signals.yaml          signal thresholds                             (optional, merged over defaults by name)
  playbooks.yaml        company-approved actions per signal           (optional)
  access.yaml           who can see which accounts                    (optional; no file = nobody)
  slack.yaml            Slack app + alert routing                     (optional)
  links.csv             manual / approved account links               (optional, written by `link`)
  presets/*.yaml        company-specific presets                      (optional, extend the built-in catalog)

Secrets are never written into YAML: use ${ENV:NAME} / ${ENV:NAME:-default},
or ${SECRET:key} for credentials entered in the admin UI (encrypted secret store).
Preset connection templates use {{param}} and are filled from the source's
own settings in connectors.yaml.

agent.yaml `pack:` (saas | it_services) picks a business-type pack
(renewal_risk/packs/), which supplies setup categories, specialists, the
enabled signals, default playbooks and UI wording.
"""
from __future__ import annotations

import copy
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .playbooks import normalize as normalize_playbooks

load_dotenv()  # .env in the working directory, never overrides real env vars

PACKAGE_DIR = Path(__file__).resolve().parent.parent
PRESETS_DIR = PACKAGE_DIR / "presets"
DEFAULTS_DIR = PACKAGE_DIR / "defaults"
PACKS_DIR = PACKAGE_DIR / "packs"


class ConfigError(Exception):
    pass


def config_dir() -> Path:
    return Path(os.environ.get("RENEWAL_RISK_CONFIG_DIR", "config")).resolve()


def state_dir(cfg_dir: Path | None = None) -> Path:
    """Runtime state (secrets, store, link cache). Defaults next to the config.
    Not created here: whatever writes into it creates it, so validating a
    config never leaves directories behind."""
    env = os.environ.get("RENEWAL_RISK_STATE_DIR")
    return Path(env).resolve() if env else (cfg_dir or config_dir()).parent / "state"


# ---------------------------------------------------------------------------
# YAML + interpolation helpers
# ---------------------------------------------------------------------------

_ENV_PATTERN = re.compile(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_SECRET_PATTERN = re.compile(r"\$\{SECRET:([A-Za-z0-9_./-]+)\}")
_PARAM_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def _interpolate_env(value, missing: set[str], secrets=None):
    """Resolve ${ENV:...} and ${SECRET:...}. `secrets` is a key -> value lookup;
    unresolved names are collected in `missing` (secrets as "SECRET:<key>")."""
    if isinstance(value, str):
        def env_repl(match):
            name, default = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            missing.add(name)
            return ""

        def secret_repl(match):
            found = secrets(match.group(1)) if secrets else None
            if found in (None, ""):
                missing.add(f"SECRET:{match.group(1)}")
                return ""
            return str(found)
        return _SECRET_PATTERN.sub(secret_repl, _ENV_PATTERN.sub(env_repl, value))
    if isinstance(value, dict):
        return {k: _interpolate_env(v, missing, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(v, missing, secrets) for v in value]
    return value


def fill_params(value, params: dict, missing: set[str]):
    """Fill {{param}} placeholders in a preset from the source's settings."""
    if isinstance(value, str):
        whole = _PARAM_PATTERN.fullmatch(value.strip())
        if whole:  # keep non-string params (ints, lists) intact
            name = whole.group(1)
            if name not in params:
                missing.add(name)
                return ""
            return params[name]

        def repl(match):
            name = match.group(1)
            if name not in params:
                missing.add(name)
                return ""
            return str(params[name])
        return _PARAM_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: fill_params(v, params, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [fill_params(v, params, missing) for v in value]
    return value


def deep_merge(base, override):
    """Dicts merge recursively; anything else in `override` replaces `base`."""
    if isinstance(base, dict) and isinstance(override, dict):
        merged = copy.deepcopy(base)
        for key, value in override.items():
            merged[key] = deep_merge(merged.get(key), value) if key in merged else copy.deepcopy(value)
        return merged
    return copy.deepcopy(override)


def read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return data


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

def load_presets(cfg_dir: Path | None = None) -> dict[str, dict]:
    presets: dict[str, dict] = {}
    for directory in (PRESETS_DIR, (cfg_dir or config_dir()) / "presets"):
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.yaml")):
            presets[path.stem] = read_yaml(path)
    return presets


def choose_auth_method(preset: dict, raw: dict) -> dict | None:
    """The preset auth method this source uses: `auth.method` when set, else the
    first method whose required params are all present (so older configs that
    only set e.g. `org_alias` keep working), else the first method."""
    methods = preset.get("auth_methods") or []
    if not methods:
        return None
    wanted = (raw.get("auth") or {}).get("method") if isinstance(raw.get("auth"), dict) else None
    if wanted:
        return next((m for m in methods if m.get("method") == wanted), None)
    for m in methods:
        required = [p for p, spec in (m.get("params") or {}).items() if (spec or {}).get("required")]
        if required and all(raw.get(p) not in (None, "") for p in required):
            return m
    return methods[0]


def resolve_source(name: str, raw: dict, presets: dict[str, dict], overrides: dict, missing_env: set[str], secrets=None) -> tuple[dict, list[str]]:
    """Preset + chosen auth method + the admin's settings + mapping overrides
    -> one resolved source. Returns (source_config, problems)."""
    problems: list[str] = []
    raw = _interpolate_env(raw or {}, missing_env, secrets)
    preset_name = raw.get("preset")
    base: dict = {}
    method = None
    if preset_name:
        if preset_name not in presets:
            return {}, [f"source '{name}': unknown preset '{preset_name}' (see `renewal-risk presets`)"]
        base = copy.deepcopy(presets[preset_name])
        declared = dict(base.pop("params", {}) or {})
        method = choose_auth_method(base, raw)
        if base.get("auth_methods") and method is None:
            problems.append(f"source '{name}': preset '{preset_name}' has no auth method '{raw['auth'].get('method')}'")
        if method:
            declared.update(method.get("params") or {})
            base = deep_merge(base, method.get("set") or {})
            if method.get("oauth"):
                base["oauth"] = method["oauth"]
        for param, spec in declared.items():
            spec = spec or {}
            if param not in raw and "default" in spec:
                raw[param] = spec["default"]
            if spec.get("required") and raw.get(param) in (None, ""):
                problems.append(f"source '{name}': preset '{preset_name}' needs '{param}'"
                                + (f" ({spec['description']})" if spec.get("description") else ""))
        base.pop("auth_methods", None)
        missing_params: set[str] = set()
        base = fill_params(base, raw, missing_params)
        for param in sorted(missing_params - set(declared)):
            problems.append(f"source '{name}': preset '{preset_name}' references undeclared param '{param}'")
        for key in ("description", "setup_notes", "label", "categories", "describe_notes"):
            base.pop(key, None)
    user = {k: v for k, v in raw.items() if k not in ("preset", "auth")}
    resolved = deep_merge(base, user)
    if overrides:
        resolved["entities"] = deep_merge(resolved.get("entities", {}), overrides)
    # `entity: null` (set when an admin turns an entity off for this source) removes it.
    resolved["entities"] = {e: spec for e, spec in (resolved.get("entities") or {}).items() if spec}
    resolved["name"] = name
    resolved["preset"] = preset_name
    resolved["auth_method"] = method.get("method") if method else None
    if not resolved.get("type"):
        problems.append(f"source '{name}': no connector type (set `preset:` or `type:`)")
    return resolved, problems


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    dir: Path
    sources: dict[str, dict]
    primary: str | None
    linking: dict[str, list]
    agent: dict
    signals: list[dict]
    playbooks: dict
    access: dict
    slack: dict
    pack: dict = field(default_factory=dict)
    tenant_id: str = "default"
    problems: list[str] = field(default_factory=list)
    missing_env: set[str] = field(default_factory=set)

    @property
    def state_dir(self) -> Path:
        return state_dir(self.dir)


def load_packs() -> dict[str, dict]:
    return {p.stem: read_yaml(p) for p in sorted(PACKS_DIR.glob("*.yaml"))}


def all_signals(pack: dict) -> list[dict]:
    """Every built-in signal; those outside the pack start disabled (a company can enable them)."""
    in_pack = set(pack.get("signals") or [])
    out = []
    for sig in read_yaml(DEFAULTS_DIR / "signals.yaml").get("signals", []):
        if in_pack and sig["name"] not in in_pack:
            sig = {**sig, "enabled": False}
        out.append(sig)
    return out


def signal_catalog(cfg_dir: Path, pack: dict) -> list[dict]:
    """Every signal a company has, enabled or not: built-ins (with the company's
    overrides merged in) followed by its custom signals, each tagged `origin`."""
    user = read_yaml(cfg_dir / "signals.yaml")
    if user.get("replace_defaults"):
        return [{**s, "origin": "custom"} for s in user.get("signals", [])]
    by_name = {s["name"]: {**s, "origin": "built-in"} for s in all_signals(pack)}
    for sig in user.get("signals", []) or []:
        base = by_name.get(sig["name"])
        by_name[sig["name"]] = deep_merge(base, sig) if base else {**sig, "origin": "custom"}
    return list(by_name.values())


def _load_signals(cfg_dir: Path, pack: dict) -> list[dict]:
    return [s for s in signal_catalog(cfg_dir, pack) if s.get("enabled", True)]


def _secret_lookup(cfg_dir: Path, tenant_id: str):
    """Lazily opens the secret store only when a ${SECRET:...} is actually used."""
    cache: dict = {}

    def lookup(key: str):
        if "store" not in cache:
            from ..auth.secrets import default_store
            cache["store"] = default_store(state_dir(cfg_dir), tenant_id)
        return cache["store"].get(key)
    return lookup


def load_settings(cfg_dir: Path | str | None = None, tenant_id: str = "default") -> Settings:
    cfg_dir = Path(cfg_dir).resolve() if cfg_dir else config_dir()
    problems: list[str] = []
    missing_env: set[str] = set()
    secrets = _secret_lookup(cfg_dir, tenant_id)

    user_agent = _interpolate_env(read_yaml(cfg_dir / "agent.yaml"), missing_env, secrets)
    defaults_agent = read_yaml(DEFAULTS_DIR / "agent.yaml")
    pack_name = user_agent.get("pack") or defaults_agent.get("pack", "saas")
    packs = load_packs()
    pack = packs.get(pack_name, {})
    if not pack:
        problems.append(f"agent.yaml: unknown pack '{pack_name}' (known: {', '.join(packs)})")
    pack = {"name": pack_name, **pack}
    agent = deep_merge(deep_merge(defaults_agent, pack.get("agent") or {}), user_agent)
    if pack.get("specialists"):  # a pack replaces the specialist set; the company's agent.yaml can still override
        agent["specialists"] = deep_merge(pack["specialists"], user_agent.get("specialists") or {})

    connectors = read_yaml(cfg_dir / "connectors.yaml")
    if not connectors:
        problems.append(f"{cfg_dir / 'connectors.yaml'} is missing or empty — run `renewal-risk setup` or the admin UI")
    presets = load_presets(cfg_dir)
    raw_sources = connectors.get("sources") or {}
    sources: dict[str, dict] = {}
    for name, raw in raw_sources.items():
        overrides = read_yaml(cfg_dir / "mappings" / f"{name}.overrides.yaml")
        resolved, source_problems = resolve_source(name, raw, presets, overrides, missing_env, secrets)
        problems.extend(source_problems)
        if resolved:
            resolved["_config_dir"] = str(cfg_dir)
            resolved["_state_dir"] = str(state_dir(cfg_dir))
            resolved["_tenant_id"] = tenant_id
            sources[name] = resolved

    primary = connectors.get("primary")
    if sources and not primary:
        primary = next(iter(sources))
    if primary and primary not in sources:
        problems.append(f"primary source '{primary}' is not defined under sources")

    access = _interpolate_env(read_yaml(cfg_dir / "access.yaml"), missing_env, secrets)
    slack = _interpolate_env(read_yaml(cfg_dir / "slack.yaml"), missing_env, secrets)
    pack_playbooks = pack.get("playbooks") or {}
    playbooks = normalize_playbooks(read_yaml(cfg_dir / "playbooks.yaml") or pack_playbooks, default_roles=pack_playbooks.get("owner_roles"))

    return Settings(
        dir=cfg_dir,
        sources=sources,
        primary=primary,
        linking=connectors.get("linking") or {},
        agent=agent,
        signals=_load_signals(cfg_dir, pack),
        playbooks=playbooks,
        access=access,
        slack=slack,
        pack=pack,
        tenant_id=tenant_id,
        problems=problems,
        missing_env=missing_env,
    )


_cached: Settings | None = None
_cache_lock = threading.Lock()


def get_settings(reload: bool = False) -> Settings:
    global _cached
    with _cache_lock:
        if _cached is None or reload:
            _cached = load_settings()
        return _cached
