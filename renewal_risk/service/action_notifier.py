"""
action_notifier.py — sends each recommended action to the person who owns it.

After an assessment at or above `slack.actions.min_band` (default high), every
recommended action is resolved to people through its owner role
(playbooks `owner_roles[].find`) and DM'd once on Slack with Done / Snooze /
Not relevant buttons. Status lives in the store's `action_items` table.

Rules that keep it safe and quiet:
- a person is only ever DM'd about an account access.yaml lets them see;
- an action that is already open, snoozed, or was closed recently is not re-sent;
- a play whose minimum severity was not reached is not sent;
- no Slack bot token, or `actions.dm_owners: false` -> nothing is sent.
"""
from __future__ import annotations

import hashlib
import sys

from ..access.rbac import can_view
from ..core.playbooks import SEVERITY_RANK
from .risk_service import BAND_ORDER


def owner_emails(ctx, role_name: str, account: dict) -> list[str]:
    """People to notify for one owner role on one account (access-checked)."""
    role = next((r for r in ctx.settings.playbooks.get("owner_roles", []) if r["name"] == role_name), None)
    find = (role or {}).get("find") or {}
    found: set[str] = set()
    if find.get("account_field") and account.get(find["account_field"]):
        found.add(str(account[find["account_field"]]).strip().lower())
    found.update(find.get("emails") or [])
    if find.get("access_role"):
        found.update(ctx.policy.emails_who_can_view(account, [find["access_role"]]))
    allowed = []
    for email in sorted(found):
        principal = ctx.policy.principal_for_email(email)
        if principal is not None and can_view(principal, account):
            allowed.append(email)
    return allowed


def action_key(action: dict) -> str:
    """Stable id for 'the same action' across re-assessments."""
    if action.get("playbook_id"):
        return f"play:{action['playbook_id']}"
    words = " ".join(str(action.get("action", "")).lower().split())
    return "ai:" + hashlib.sha1(words.encode()).hexdigest()[:12]


def _severity_reached(play: dict, view: dict) -> bool:
    fired = [SEVERITY_RANK.get(s["severity"], 0) for s in view.get("signals", []) if s["name"] in play.get("signals", [])]
    return max(fired, default=0) >= SEVERITY_RANK.get(play.get("min_severity", "any"), 0)


def notify(ctx, view: dict, account: dict, send=None) -> list[dict]:
    """DM the owners of `view`'s recommended actions. `send(email, text, blocks)`
    defaults to the Slack notifier; returns what was sent."""
    from ..slack import blocks as kit

    cfg = {"dm_owners": True, "min_band": "high", "cooldown_days": 30, **((ctx.settings.slack or {}).get("actions") or {})}
    if not cfg["dm_owners"] or BAND_ORDER.get(view.get("band"), 0) < BAND_ORDER.get(cfg["min_band"], 2):
        return []
    if send is None:
        if not (ctx.settings.slack or {}).get("bot_token"):
            return []
        from ..slack.app import SlackNotifier
        send = SlackNotifier(ctx)._dm
    plays = {p["id"]: p for p in ctx.settings.playbooks.get("plays", [])}
    sent = []
    for action in view.get("recommended_actions", []):
        play = plays.get(action.get("playbook_id") or "")
        if play and (not play.get("notify", True) or not _severity_reached(play, view)):
            continue
        key = action_key(action)
        for email in owner_emails(ctx, action.get("owner_role", ""), account):
            if not ctx.store.should_notify(view["account_id"], key, email, cfg["cooldown_days"]):
                continue
            item_id = ctx.store.add_action_item(view["account_id"], view.get("account_name"), view.get("assessment_id"),
                                                action.get("playbook_id") or "", key, action["action"], email, action.get("due_in_days", 14))
            text = f"Action for {view.get('account_name')}: {action['action']}"
            try:
                ok = send(email, text, kit.action_blocks(view, action, play, item_id))
            except Exception as exc:  # noqa: BLE001 - a failed DM never fails the assessment
                print(f"actions: DM to {email} failed: {exc}", file=sys.stderr)
                ok = False
            sent.append({"email": email, "item_id": item_id, "action": action["action"], "delivered": bool(ok)})
    return sent
