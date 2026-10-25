"""Playbook actions: owner resolution, Slack DMs (once, access-checked), Done / Snooze, and playbook ids."""
import pytest

from renewal_risk.core import playbooks
from renewal_risk.service import action_notifier
from renewal_risk.slack import blocks as kit
from renewal_risk.slack.app import update_action


def _acme(ctx):
    admin = ctx.policy.principal_for_email("admin@demo.example")
    return ctx.service.ask(admin, "Acme Corp"), ctx.service.account("A001")


class Outbox:
    def __init__(self):
        self.sent = []

    def __call__(self, email, text, blocks):
        self.sent.append((email, text, blocks))
        return True


def test_owner_roles_resolve_to_people_who_may_see_the_account(ctx):
    account = ctx.service.account("A001")  # EMEA, owned by csm.one
    ctx.settings.playbooks["owner_roles"] = [
        {"name": "Owner", "find": {"account_field": "owner_email"}},
        {"name": "Director", "find": {"access_role": "director"}},
        {"name": "Fixed", "find": {"emails": ["stranger@elsewhere.example", "csm.two@demo.example", "admin@demo.example"]}},
        {"name": "Label only", "find": {}},
    ]
    assert action_notifier.owner_emails(ctx, "Owner", account) == ["csm.one@demo.example"]
    assert action_notifier.owner_emails(ctx, "Director", account) == ["pd.emea@demo.example"]
    # not in access.yaml, or not allowed to see A001 -> never DM'd
    assert action_notifier.owner_emails(ctx, "Fixed", account) == ["admin@demo.example"]
    assert action_notifier.owner_emails(ctx, "Label only", account) == []
    assert action_notifier.owner_emails(ctx, "Unknown role", account) == []


def test_actions_are_dmd_once_and_snooze_or_done_is_respected(ctx):
    view, account = _acme(ctx)
    assert view["band"] == "high" and view["recommended_actions"]
    outbox = Outbox()
    sent = action_notifier.notify(ctx, view, account, send=outbox)
    assert sent and {s["email"] for s in sent} == {"csm.one@demo.example"} and all(s["delivered"] for s in sent)
    email, text, body = outbox.sent[0]
    assert "Acme Corp" in text and body[-1]["type"] == "actions"
    assert action_notifier.notify(ctx, view, account, send=outbox) == []  # still open: no repeat

    item = sent[0]["item_id"]
    outsider = ctx.policy.principal_for_email("pd.emea@demo.example")
    assert update_action(ctx, outsider, item, "done") is None  # not the owner, not an admin
    owner = ctx.policy.principal_for_email("csm.one@demo.example")
    assert update_action(ctx, owner, item, "snoozed")["status"] == "snoozed"
    assert action_notifier.notify(ctx, view, account, send=outbox) == []  # snoozed: quiet
    assert update_action(ctx, owner, item, "done")["status"] == "done"
    assert action_notifier.notify(ctx, view, account, send=outbox) == []  # closed recently: cooldown
    summary = ctx.store.action_summary(30)
    assert summary["done"] == 1 and summary["total"] == len(sent)


def test_notify_respects_settings_and_band(ctx):
    view, account = _acme(ctx)
    outbox = Outbox()
    ctx.settings.slack["actions"] = {"dm_owners": False}
    assert action_notifier.notify(ctx, view, account, send=outbox) == []
    ctx.settings.slack["actions"] = {"dm_owners": True}
    assert action_notifier.notify(ctx, {**view, "band": "medium"}, account, send=outbox) == []
    assert action_notifier.notify(ctx, view, account) == []  # no bot token: nothing sent, nothing breaks


def test_action_dm_blocks_carry_why_steps_and_explanations(ctx):
    view, _ = _acme(ctx)
    play = {"id": "exec_call", "name": "Executive call", "description": "Senior attention resets the relationship.",
            "steps": ["Book the call", "Share the plan"], "link": "https://wiki.example/exec", "signals": [], "priority": "high"}
    action = {**view["recommended_actions"][0], "playbook_id": "exec_call"}
    body = kit.action_blocks(view, action, play, item_id=7)
    text = str(body)
    assert "Executive call" in text and "Book the call" in text and "https://wiki.example/exec" in text
    assert view["signals"][0]["how_calculated"][:30] in text
    assert {e["action_id"] for e in body[-1]["elements"]} == {"rr_action_done", "rr_action_snooze", "rr_action_not_relevant"}
    assert all(len(b["text"]["text"]) <= 3000 for b in body if b["type"] == "section")


def test_assessments_explain_signals_and_keep_only_known_playbook_ids(ctx, monkeypatch):
    view, _ = _acme(ctx)
    assert all(s["how_calculated"] for s in view["signals"])
    known = {p["id"] for p in ctx.settings.playbooks["plays"]} | {""}
    assert {a["playbook_id"] for a in view["recommended_actions"]} <= known

    monkeypatch.setattr(type(ctx.engine.llm), "_play_for", staticmethod(lambda system, signal: "invented_play"))
    result = ctx.engine.assess("A001", account_name="Acme Corp")
    assert result.assessment["recommended_actions"] and all(a["playbook_id"] == "" for a in result.assessment["recommended_actions"])


def test_playbook_normalisation():
    assert playbooks.normalize_owner_role("Finance") == {"name": "Finance", "find": {}}
    role = playbooks.normalize_owner_role({"name": "Fin", "find": {"emails": ["A@X.com", "bad"], "account_field": "nope"}})
    assert role == {"name": "Fin", "find": {"emails": ["a@x.com"]}}
    play = playbooks.normalize_play({"action": "Call them", "signals": ["a", "zzz"], "due_in_days": 999}, {"a"})
    assert play["id"] == "call_them" and play["signals"] == ["a"] and play["due_in_days"] == 365 and play["notify"] is True
    with pytest.raises(playbooks.PlaybookError):
        playbooks.normalize({"plays": [{"action": "x", "signals": ["a"], "link": "javascript:alert(1)"}]}, {"a"}, strict=True)
    with pytest.raises(playbooks.PlaybookError):
        playbooks.normalize({"plays": [{"action": "x", "signals": ["a"]}, {"action": "x", "signals": ["a"]}]}, {"a"}, strict=True)
    assert len(playbooks.normalize({"plays": [{"action": "x", "signals": ["a"]}] * 2}, {"a"})["plays"]) == 2  # lenient load


def test_plain_role_names_inherit_the_packs_lookup():
    pack_roles = [{"name": "Account owner", "find": {"account_field": "owner_email"}}]
    roles = playbooks.normalize({"owner_roles": ["Account owner", {"name": "Account owner", "find": {}}, "Finance"]}, default_roles=pack_roles)["owner_roles"]
    assert [r["find"] for r in roles] == [{"account_field": "owner_email"}, {}, {}]  # an explicit find (even empty) wins
