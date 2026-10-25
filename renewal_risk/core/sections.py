"""
sections.py — admin-defined data sections (config/sections.yaml).

A section is the custom counterpart of a pack category ("Customers & renewals"):
it brings a new kind of data into the agent and has its own specialist.

  sections:
    - key: customer_surveys                     # also the specialist's name
      label: Customer surveys
      description: NPS and CSAT responses from our survey tool.
      data_type:
        name: survey
        description: One survey response from a customer contact.
        date_field: responded_at                # when it happened (as-of filtering, leakage audit)
        fields:
          - {name: score, type: num, description: "Likelihood to recommend, 0-10"}
          - {name: responded_at, type: date, description: When the customer answered}
      focus: Judge whether the customer is unhappy, from their survey answers and trend.

Applying sections registers each data type with the standard model, adds the
setup category and adds the specialist — so connectors, field mapping,
signals, the pipeline and Slack all handle it with no special cases.
"""
from __future__ import annotations

import re

from . import guard, model

TYPES = ("str", "num", "date", "bool", "list")
STANDARD_FIELDS = ("id", "account_id", "created_at", "updated_at")
_SLUG = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class SectionError(ValueError):
    pass


def _field(raw: dict, where: str) -> dict:
    name = str(raw.get("name") or "").strip()
    if not _SLUG.match(name):
        raise SectionError(f"{where}: field names are lowercase letters, digits and _ (got '{name}')")
    if name in STANDARD_FIELDS:
        raise SectionError(f"{where}: '{name}' is added automatically — remove it")
    if guard.is_banned(name):
        raise SectionError(f"{where}: '{name}' looks like free text (comments, notes…), which never reaches the AI — leave it out")
    kind = raw.get("type") if raw.get("type") in TYPES else None
    if kind is None:
        raise SectionError(f"{where}: field '{name}' needs a type ({', '.join(TYPES)})")
    return {"name": name, "type": kind, "description": str(raw.get("description") or "").strip()[:300]}


def normalize_section(raw: dict, reserved: set[str]) -> dict:
    """One section, cleaned; raises SectionError with a reason the admin can act on."""
    key = str(raw.get("key") or "").strip()
    if not _SLUG.match(key):
        raise SectionError(f"section key '{key}': use lowercase letters, digits and _")
    if key in reserved:
        raise SectionError(f"section '{key}': that name is already used by a built-in section or specialist")
    dt = raw.get("data_type") or {}
    entity = str(dt.get("name") or "").strip()
    if not _SLUG.match(entity):
        raise SectionError(f"section '{key}': the data type needs a name like 'survey'")
    if entity in model.BUILT_IN:
        raise SectionError(f"section '{key}': '{entity}' is a standard data type — pick another name")
    fields = [_field(f, f"section '{key}'") for f in dt.get("fields") or []]
    if not fields:
        raise SectionError(f"section '{key}': add at least one field to the data type")
    names = [f["name"] for f in fields]
    if len(set(names)) != len(names):
        raise SectionError(f"section '{key}': two fields share a name")
    date_field = dt.get("date_field") or "created_at"
    if date_field != "created_at" and date_field not in [f["name"] for f in fields if f["type"] == "date"]:
        raise SectionError(f"section '{key}': the 'when it happened' field must be one of its date fields")
    focus = str(raw.get("focus") or "").strip()
    if not focus:
        raise SectionError(f"section '{key}': tell its specialist what to look for")
    return {
        "key": key, "label": str(raw.get("label") or key).strip()[:60], "description": str(raw.get("description") or "").strip()[:300],
        "data_type": {"name": entity, "description": str(dt.get("description") or raw.get("description") or "").strip()[:300],
                      "date_field": date_field, "fields": fields},
        "focus": focus[:1000],
    }


def normalize(doc: dict, reserved: set[str], problems: list[str] | None = None) -> list[dict]:
    """All sections. With `problems`, bad ones are reported and skipped (loading);
    without, the first problem raises (saving from the admin UI)."""
    out: list[dict] = []
    seen: set[str] = set()
    for raw in (doc or {}).get("sections") or []:
        try:
            section = normalize_section(raw, reserved)
            if {section["key"], section["data_type"]["name"]} & seen:
                raise SectionError(f"section '{section['key']}': its key or data type name is used twice")
        except SectionError as exc:
            if problems is None:
                raise
            problems.append(f"sections.yaml: {exc}")
            continue
        seen |= {section["key"], section["data_type"]["name"]}
        out.append(section)
    return out


def apply(sections: list[dict], agent: dict, pack: dict) -> None:
    """Register the data types and add each section's category and specialist."""
    model.register_entities({
        s["data_type"]["name"]: {
            "description": s["data_type"]["description"] or s["label"],
            "fields": {f["name"]: f"{f['type']} | {f['description']}" for f in s["data_type"]["fields"]},
            "date_field": s["data_type"]["date_field"],
        } for s in sections})
    agent["specialists"] = {**(agent.get("specialists") or {}),
                            **{s["key"]: {"entities": [s["data_type"]["name"]], "focus": s["focus"], "label": s["label"]} for s in sections}}
    pack["categories"] = list(pack.get("categories") or []) + [
        {"key": s["key"], "label": s["label"], "description": s["description"], "entities": [s["data_type"]["name"]],
         "custom": True, "section": s} for s in sections]


def reserved_names(agent: dict, pack: dict, sections: list[dict] = ()) -> set[str]:
    """Keys a section may not take: built-in categories and specialists."""
    own = {s["key"] for s in sections}
    return {c["key"] for c in pack.get("categories") or [] if not c.get("custom")} | (set(agent.get("specialists") or {}) - own)
