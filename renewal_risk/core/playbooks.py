"""
playbooks.py — the company's approved actions, normalised in one place.

Used leniently when settings load (older files without ids still work) and
strictly when the admin UI saves (bad input is refused with a reason).

  play:        id, name, description (why it works), signals, min_severity,
               action, steps, owner_role, priority, due_in_days, link, notify
  owner role:  name + how to find the person to notify for an account:
               {account_field: owner_email} | {access_role: director} | {emails: [...]}
"""
from __future__ import annotations

import re

from . import model

SEVERITY_RANK = {"any": 0, "low": 1, "medium": 2, "high": 3}
PRIORITIES = ("low", "medium", "high")


class PlaybookError(ValueError):
    pass


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(text or "").lower()).strip("_")[:48] or "play"


def normalize_owner_role(role) -> dict | None:
    if isinstance(role, str):
        return {"name": role.strip(), "find": {}} if role.strip() else None
    name = str((role or {}).get("name") or "").strip()
    if not name:
        return None
    find, clean = role.get("find") or {}, {}
    if find.get("account_field") and model.field_type("account", find["account_field"]):
        clean["account_field"] = find["account_field"]
    if find.get("access_role"):
        clean["access_role"] = str(find["access_role"])
    emails = [str(e).strip().lower() for e in find.get("emails") or [] if "@" in str(e)]
    if emails:
        clean["emails"] = emails
    return {"name": name, "find": clean}


def normalize_play(play: dict, known_signals: set[str] | None = None, strict: bool = False) -> dict | None:
    action = str(play.get("action") or "").strip()
    sigs = [s for s in play.get("signals") or [] if known_signals is None or s in known_signals]
    link = str(play.get("link") or "").strip()
    problem = ("every play needs an action and at least one known signal" if not action or not sigs
               else f"play '{action[:40]}': the runbook link must start with https://" if link and not link.startswith(("https://", "http://"))
               else None)
    if problem:
        if strict:
            raise PlaybookError(problem)
        return None
    return {
        "id": slug(play.get("id") or play.get("name") or action), "name": str(play.get("name") or action)[:80],
        "description": str(play.get("description") or "")[:500], "signals": sigs,
        "min_severity": play.get("min_severity") if play.get("min_severity") in SEVERITY_RANK else "any",
        "action": action[:300], "steps": [str(s)[:200] for s in play.get("steps") or [] if str(s).strip()][:10],
        "owner_role": str(play.get("owner_role") or "Account owner"),
        "priority": play.get("priority") if play.get("priority") in PRIORITIES else "medium",
        "due_in_days": max(1, min(int(play.get("due_in_days") or 14), 365)), "link": link,
        "notify": bool(play.get("notify", True)),
    }


def normalize(playbooks: dict, known_signals: set[str] | None = None, strict: bool = False, default_roles: list | None = None) -> dict:
    """`default_roles` (the pack's) tell a role written as a plain name how to
    find its person, so older files keep their DMs; an explicit `find` wins."""
    plays = [p for p in (normalize_play(p, known_signals, strict) for p in playbooks.get("plays") or []) if p]
    seen: set[str] = set()
    for p in plays:  # ids must be unique; suffix rather than drop when loading leniently
        if p["id"] in seen:
            if strict:
                raise PlaybookError(f"two plays share the id '{p['id']}'")
            p["id"] = f"{p['id']}_{len(seen)}"
        seen.add(p["id"])
    inherit = {r["name"]: r["find"] for r in (normalize_owner_role(d) for d in default_roles or []) if r}
    roles = []
    for raw in playbooks.get("owner_roles") or []:
        role = normalize_owner_role(raw)
        if role:
            roles.append({**role, "find": inherit.get(role["name"], {})} if isinstance(raw, str) else role)
    return {"owner_roles": roles, "plays": plays}
