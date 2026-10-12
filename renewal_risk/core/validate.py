"""
validate.py — static checks behind `renewal-risk validate-config` and
`setup check`. No connection is made here (see `test-connector` for that).
"""
from __future__ import annotations

from . import mapping, model, signals
from .settings import Settings



def validate_settings(settings: Settings) -> tuple[list[str], list[str]]:
    """-> (errors, warnings)"""
    errors = list(settings.problems)
    warnings: list[str] = []
    env_missing = sorted(n for n in settings.missing_env if not n.startswith("SECRET:"))
    secret_missing = sorted(n[len("SECRET:"):] for n in settings.missing_env if n.startswith("SECRET:"))
    if env_missing:
        errors.append("environment variables referenced but not set: " + ", ".join(env_missing))
    if secret_missing:
        errors.append("credentials not entered yet (admin UI > Data sources): " + ", ".join(secret_missing))
    from .settings import DEFAULTS_DIR, read_yaml
    known_signals = {s["name"] for s in read_yaml(DEFAULTS_DIR / "signals.yaml").get("signals", [])}
    unknown = [n for n in (settings.pack.get("signals") or []) if n not in known_signals]
    if unknown:
        errors.append(f"pack '{settings.pack.get('name')}' enables unknown signals: {', '.join(unknown)}")

    for name, src in settings.sources.items():
        ents = src.get("entities") or {}
        if not ents:
            errors.append(f"source '{name}': provides no entities")
        for entity, spec in ents.items():
            if entity not in model.ENTITIES:
                errors.append(f"source '{name}': unknown entity '{entity}' (known: {', '.join(model.ENTITIES)})")
                continue
            blocks = spec if isinstance(spec, list) else [spec]
            for block in blocks:
                if not isinstance(block, dict):
                    errors.append(f"source '{name}' entity '{entity}': expected a mapping")
                    continue
                if not any(block.get(m) for m in ("fetch", "list", "search")):
                    errors.append(f"source '{name}' entity '{entity}': needs at least one of fetch / list / search")
                fields = block.get("fields") or {}
                if "id" not in fields:
                    errors.append(f"source '{name}' entity '{entity}': fields.id is required")
                for bad in mapping.unknown_fields(entity, fields):
                    errors.append(f"source '{name}' entity '{entity}': '{bad}' is not a standard {entity} field")
                for fld, expr in fields.items():
                    for problem in mapping.validate_expr(expr):
                        errors.append(f"source '{name}' {entity}.{fld}: {problem}")

    primary = settings.sources.get(settings.primary or "")
    if primary:
        pents = primary.get("entities") or {}
        if "account" not in pents:
            errors.append(f"primary source '{settings.primary}' must provide 'account' (with a list request)")
        elif not any((b or {}).get("list") for b in (pents["account"] if isinstance(pents["account"], list) else [pents["account"]])):
            errors.append(f"primary source '{settings.primary}': account needs a `list` request")
    if not any("renewal" in (s.get("entities") or {}) for s in settings.sources.values()):
        warnings.append("no source provides 'renewal' — renewal-window questions, scans and alerts will not work")

    for src_name, rules in (settings.linking or {}).items():
        if src_name not in settings.sources:
            errors.append(f"linking: unknown source '{src_name}'")
            continue
        for rule in rules or []:
            kind = (rule.get("match") if isinstance(rule, dict) else rule) or ""
            if kind not in ("same_id", "domain", "fuzzy_name") and "=" not in kind:
                errors.append(f"linking.{src_name}: unknown rule '{kind}'")
            if kind not in ("same_id",) and "customer" not in (settings.sources[src_name].get("entities") or {}):
                errors.append(f"linking.{src_name}: rule '{kind}' needs the source to provide a 'customer' entity")
    for name, src in settings.sources.items():
        if name != settings.primary and name not in (settings.linking or {}) and "customer" in (src.get("entities") or {}):
            warnings.append(f"source '{name}': no linking rules — defaulting to domain + fuzzy-name review")

    errors.extend(signals.validate(settings.signals))
    provided = {e for s in settings.sources.values() for e in (s.get("entities") or {})}
    inactive = [s["name"] for s in settings.signals if not (signals.required_entities(s) & provided)]
    if inactive:
        warnings.append("signals off (no source provides their data): " + ", ".join(inactive))
    for role, spec in (settings.agent.get("specialists") or {}).items():
        if not set(spec.get("entities", [])) & provided:
            warnings.append(f"specialist '{role}' will never run (none of {spec.get('entities')} is provided)")

    users = (settings.access or {}).get("users") or []
    if not users and not (settings.access or {}).get("domains"):
        warnings.append("access.yaml has no users — nobody can use the API or Slack yet")
    slack = settings.slack or {}
    if slack and not slack.get("bot_token"):
        warnings.append("slack.yaml present but bot_token is empty")
    if not (settings.agent.get("policy") or {}).get("zdr_confirmed"):
        real = [n for n, s in settings.sources.items() if not s.get("test_data")]
        if real:
            warnings.append("policy.zdr_confirmed is false: full assessments are refused for " + ", ".join(real)
                            + " (prescreen and linking still work)")
    return errors, warnings
