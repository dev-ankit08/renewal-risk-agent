"""
mapping_agent.py — proposes how a source's fields map onto the standard model.

Runs at setup time only; what the admin approves is saved as ordinary mapping
YAML, so every assessment uses a fixed, reviewable mapping (no model call).

  1. deterministic candidates: field name / label / description vs each
     standard field's name, aliases and description, plus type compatibility
  2. one structured Claude call per entity picks fields, writes expressions
     and value maps, and gives a confidence + reason (skipped when no model
     client is available — the deterministic best guess is returned instead)
  3. every expression is validated, and a preview maps the sampled records
"""
from __future__ import annotations

import re

from ..core import mapping, model, prompts

# Minimum a usable entity needs, beyond `id`.
REQUIRED = {
    "account": ["name"], "customer": ["name"], "renewal": ["close_date", "is_open"], "contract": ["end_date"],
    "ticket": ["is_open"], "invoice": ["is_paid"], "usage_metric": ["metric", "period_start", "value"],
    "project": ["name"], "milestone": ["due_date"], "time_entry": ["hours", "work_date"], "escalation": ["is_open"],
}
AUTO_ACCEPT = 0.85

_STOP = {"the", "of", "a", "an", "to", "is", "in", "on", "at", "no", "for", "and", "or", "by", "id", "c", "r", "hs", "pse", "this", "that", "record"}
_COMPATIBLE = {
    "str": {"str", "string", "picklist", "text", "reference", "id", "email", "enumeration", "option"},
    "num": {"num", "number", "double", "currency", "int", "integer", "percent", "decimal"},
    "bool": {"bool", "boolean", "checkbox"},
    "date": {"date", "datetime", "timestamp", "date-time"},
    "list": {"list", "str", "string", "multipicklist", "array", "url", "email"},
}


def tokens(text) -> set[str]:
    text = re.sub(r"__c$|__r\b", "", str(text or ""))
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if t and t not in _STOP}


def _type_ok(std_type: str, src_type) -> bool:
    if not src_type:
        return True
    return str(src_type).lower() in _COMPATIBLE.get(std_type, {std_type})


def score(std_field: str, doc: dict, src: dict, entity: str = "") -> float:
    if src.get("free_text"):
        return 0.0
    name, label = src["name"], src.get("label") or ""
    leaf = name.split(".")[-1]
    names = {a.lower() for a in doc["aliases"]} | {std_field.lower()}
    if leaf.lower() in names or label.lower() in names or name.lower() in names:
        base = 0.95
    elif std_field == "id":
        # Record ids: TicketNo on tickets, InvoiceNumber on invoices, Key / Ref alone.
        raw = re.sub(r"[^a-z]", "", leaf.lower())
        ends_like_id = re.search(r"(id|no|num|number|key|code|ref)$", raw)
        base = 0.8 if ends_like_id and ((entity and entity.split("_")[0] in raw) or raw in ("key", "ref", "number", "code")) else 0.0
    else:
        src_tokens = tokens(leaf) | tokens(label)

        def cover(t: set[str]) -> float:  # share of a standard name's words the source field has
            return len(t & src_tokens) / len(t) if t else 0.0
        best = max([cover(tokens(std_field))] + [cover(tokens(a)) for a in doc["aliases"]])
        desc = len(tokens(doc["description"]) & (src_tokens | tokens(src.get("description")))) / max(1, len(tokens(doc["description"])))
        base = min(0.85, 0.6 * best + 0.25 * desc)
    return round(base * (1.0 if _type_ok(doc["type"], src.get("type")) else 0.5), 3)


def candidates(entity: str, source_fields: list[dict], top: int = 4) -> dict[str, list[dict]]:
    out = {}
    for std_field, doc in model.field_docs(entity).items():
        ranked = sorted(({"source_field": f["name"], "score": score(std_field, doc, f, entity)} for f in source_fields), key=lambda c: -c["score"])
        out[std_field] = [c for c in ranked[:top] if c["score"] >= 0.2]
    return out


def _default_expr(path: str, std_type: str) -> str:
    return f"{path} | date" if std_type == "date" else f"{path} | domain" if std_type == "list" else path


def _root(expr) -> str | None:
    if isinstance(expr, dict):
        expr = expr.get("expr")
    if not isinstance(expr, str):
        return None
    head = expr.split("|", 1)[0].strip()
    return None if head.startswith(("'", '"')) or re.fullmatch(r"-?\d+(\.\d+)?|true|false|null", head.lower()) else head


def signal_needs(entity: str, signal_list: list[dict]) -> dict[str, set[str]]:
    """Which standard fields of `entity` each signal reads: {signal: {fields}}."""
    out: dict[str, set[str]] = {}
    for sig in signal_list:
        specs = [sig] + [sig.get(k) for k in ("current", "baseline", "numerator", "denominator", "trigger", "expected") if isinstance(sig.get(k), dict)]
        specs += [dict(c, entity=sig.get("entity")) for c in sig.get("cases", [])]
        for spec in specs:
            ents = spec.get("entity") or spec.get("entities") or (sig.get("entity") if spec is not sig else None)
            ents = ents if isinstance(ents, list) else [ents]
            if entity not in ents and not (spec.get("same_record") and sig.get("current", {}).get("entity") == entity):
                continue
            used = set((spec.get("where") or {}).keys())
            for key in ("field", "date_field", "start_field", "end_field"):
                if spec.get(key):
                    used.add(spec[key])
            if used:
                out.setdefault(sig["name"], set()).update(used)
    return out


class MappingAgent:
    def __init__(self, llm=None, signal_list: list[dict] | None = None):
        self.llm = llm
        self.signal_list = signal_list or []

    def suggest(self, entity: str, described: dict, current: dict | None = None) -> dict:
        current = current or {}
        source_fields = [f for f in described["fields"] if not f.get("free_text")]
        docs = model.field_docs(entity)
        cands = candidates(entity, source_fields)
        known_paths = {f["name"] for f in described["fields"]}
        suggestions: dict[str, dict] = {}

        if self.llm is not None:
            standard = {f: {"type": d["type"], "meaning": d["description"], "common_names": d["aliases"]} for f, d in docs.items()}
            compact = [{k: f[k] for k in ("name", "label", "description", "type", "options", "examples") if f.get(k)} for f in source_fields]
            system, user = prompts.mapping_prompt(entity, model.ENTITIES[entity]["description"], standard, compact,
                                                  {k: v[:3] for k, v in cands.items() if v}, current)
            data = self.llm.structured("utility", system, user, prompts.MAPPING_SCHEMA)
            for item in data["fields"]:
                if item["field"] not in docs or item["confidence"] < 0.5:
                    continue
                spec = item["expr"]
                if item["value_map"]:
                    spec = {"expr": item["expr"], "map": {v["source_value"]: v["standard_value"] for v in item["value_map"]}}
                suggestions[item["field"]] = {"field": item["field"], "expr": spec, "source_field": item["source_field"],
                                              "confidence": round(float(item["confidence"]), 2), "reason": item["reason"], "by": "model"}
        # Precedence: the model's choice, then what is already configured (a preset's
        # expressions carry value logic like `| cents` that a name match cannot), then rules.
        for std_field, expr in current.items():
            root = _root(expr)
            if known_paths and root and root not in known_paths:
                continue  # e.g. an upload's placeholder `id: id`: let a real field win
            if std_field in docs and std_field not in suggestions and expr is not None:
                suggestions[std_field] = {"field": std_field, "expr": expr, "source_field": _root(expr), "confidence": 1.0,
                                          "reason": "current mapping", "by": "current"}
        for std_field, ranked in cands.items():
            if std_field in suggestions or not ranked or ranked[0]["score"] < 0.5:
                continue
            best = ranked[0]
            suggestions[std_field] = {"field": std_field, "expr": _default_expr(best["source_field"], docs[std_field]["type"]),
                                      "source_field": best["source_field"], "confidence": best["score"],
                                      "reason": "name / label / type match", "by": "rules"}

        for s in suggestions.values():
            s["warnings"] = list(mapping.validate_expr(s["expr"]))
            root = _root(s["expr"])
            if root and known_paths and root not in known_paths and root.split("|")[0] not in known_paths:
                s["warnings"].append(f"'{root}' was not seen in the sampled records")
            s["auto_accept"] = s["confidence"] >= AUTO_ACCEPT and not s["warnings"]

        mapped = set(suggestions)
        required = ["id"] + REQUIRED.get(entity, [])
        needs = signal_needs(entity, self.signal_list)
        return {
            "entity": entity,
            "suggestions": [suggestions[f] for f in docs if f in suggestions],
            "unmapped": [f for f in docs if f not in mapped],
            "required_missing": [f for f in required if f not in mapped],
            "signals_affected": {sig: sorted(fields - mapped) for sig, fields in needs.items() if fields - mapped},
            "candidates": cands,
        }

    @staticmethod
    def preview(entity: str, fields: dict, raw_samples: list[dict], source: str, limit: int = 5) -> list[dict]:
        out = []
        for raw in raw_samples[:limit]:
            rec = mapping.map_record(raw, entity, fields, source)
            if rec is not None:
                out.append({k: v for k, v in rec.items() if not k.startswith("_")})
        return out

def inactive_signals(entity: str, fields: dict, signal_list: list[dict]) -> dict[str, list[str]]:
    """Signals that read fields this mapping leaves out."""
    return {sig: sorted(f - set(fields)) for sig, f in signal_needs(entity, signal_list).items() if f - set(fields)}


__all__ = ["MappingAgent", "candidates", "score", "signal_needs", "inactive_signals"]
