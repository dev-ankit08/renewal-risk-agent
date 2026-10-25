"""
packs.py — the setup wizard's catalog: for the active business-type pack,
which data categories to ask for and which systems can fill each one.
"""
from __future__ import annotations

from ..core.settings import Settings, load_packs, load_presets

GENERIC = [
    {"type": "csv", "label": "CSV / Excel upload", "description": "Upload exports from any system; map columns on the next screen."},
    {"type": "sql", "label": "Database (SQL)", "description": "Postgres, MySQL, SQL Server, Snowflake… — a read-only SELECT per account."},
    {"type": "rest", "label": "REST API", "description": "Any JSON API — one GET per account."},
    {"type": "mcp", "label": "MCP server", "description": "Any system with an MCP server — one tool call per account."},
]
SALESFORCE_TYPES = ("salesforce", "salesforce_rest")


def preset_entities(preset: dict) -> list[str]:
    return [e for e, spec in (preset.get("entities") or {}).items() if spec]


def public_auth_methods(preset: dict, callback_url: str) -> list[dict]:
    """Auth methods as the UI shows them: forms, help text, never secrets."""
    common = preset.get("params") or {}
    out = []
    for m in preset.get("auth_methods") or [{"method": "none", "label": "No authentication"}]:
        params = []
        for name, spec in {**common, **(m.get("params") or {})}.items():
            spec = spec or {}
            params.append({"name": name, "label": spec.get("label", name), "description": spec.get("description", ""),
                           "required": bool(spec.get("required")), "secret": bool(spec.get("secret")), "default": spec.get("default")})
        out.append({"method": m["method"], "label": m.get("label", m["method"]), "oauth": bool(m.get("oauth")),
                    "help": str(m.get("help", "")).replace("{callback_url}", callback_url).strip(), "params": params})
    return out


def catalog(settings: Settings, callback_url: str) -> dict:
    presets = load_presets(settings.dir)
    pack = settings.pack
    configured = {name: src for name, src in settings.sources.items()}
    categories = []
    salesforce = [n for n, src in configured.items() if src.get("type") in SALESFORCE_TYPES]
    for cat in pack.get("categories") or []:
        filled_by = [n for n, src in configured.items() if cat["entities"][0] in (src.get("entities") or {})]
        if cat.get("custom"):  # an admin-defined section: generic connectors or an object in a Salesforce org
            categories.append({**{k: cat.get(k) for k in ("key", "label", "description", "entities", "section")}, "required": False,
                               "custom": True, "systems": [], "configured_sources": filled_by})
            continue
        # The first entity leads: a system belongs to a category only if it has that
        # (Stripe has contracts, but it is not where your customer list lives).
        lead, wanted = cat["entities"][0], set(cat["entities"])
        systems = []
        for name, preset in sorted(presets.items()):
            provides = [e for e in preset_entities(preset) if e in wanted]
            if lead in provides:
                systems.append({"preset": name, "label": preset.get("label", name), "description": preset.get("description", ""),
                                "provides": provides, "setup_notes": preset.get("setup_notes", ""),
                                "auth_methods": public_auth_methods(preset, callback_url)})
        categories.append({**{k: cat.get(k) for k in ("key", "label", "description", "entities", "required")},
                           "systems": systems, "configured_sources": filled_by})
    return {
        "pack": {k: pack.get(k) for k in ("name", "label", "description", "terminology")},
        "packs": [{"name": n, "label": p.get("label", n), "description": p.get("description", "")} for n, p in load_packs().items()],
        "categories": categories,
        "generic": GENERIC,
        "salesforce_sources": salesforce,  # connections a custom section can read an object from
        "callback_url": callback_url,
    }
