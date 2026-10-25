"""
app.py — the Slack app (Slack Bolt). Run with `renewal-risk serve --slack`
(with the API) or `renewal-risk slack` (Slack only).

Socket Mode is the default: the app opens an outbound websocket to Slack, so
a self-hosted install needs no public URL or inbound firewall rule.

slack.yaml:
  mode: socket                              # socket | http
  bot_token: ${ENV:SLACK_BOT_TOKEN}         # xoxb-...
  app_token: ${ENV:SLACK_APP_TOKEN}         # xapp-... (socket mode)
  signing_secret: ${ENV:SLACK_SIGNING_SECRET}  # (http mode)
  command: /renewal-risk
  reply_visibility: ephemeral               # ephemeral | in_channel for slash-command answers
  alerts:
    channel: "#renewal-risk"                # post every alert here ("" = no channel post)
    dm_owner: true                          # DM the account owner (account.owner_email)
    dm_roles: [director]                    # also DM these access.yaml roles whose scope covers the account
    min_band: high                          # alert when an account rises into this band or above

Who is asking is resolved from the Slack user's profile email, then
access.yaml decides what they may see. Users not in access.yaml get nothing.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time

from ..access.rbac import AccessDenied, Principal
from ..core.connector import ConnectorError
from ..core.llm import LLMError
from ..core.pipeline import PolicyError
from ..service.context import AppContext, get_context
from . import blocks as kit

_MENTION = re.compile(r"<@[A-Z0-9]+>")


class SlackIdentity:
    """Slack user id -> Principal, via the profile email (cached for an hour)."""

    def __init__(self, ctx: AppContext, client):
        self.ctx = ctx
        self.client = client
        self._cache: dict[str, tuple[Principal | None, float]] = {}
        self._lock = threading.Lock()

    def principal(self, user_id: str) -> Principal | None:
        with self._lock:
            hit = self._cache.get(user_id)
            if hit and time.time() - hit[1] < 3600:
                return hit[0]
        try:
            info = self.client.users_info(user=user_id)
            profile = (info.get("user") or {}).get("profile") or {}
            principal = self.ctx.policy.principal_for_email(profile.get("email"), profile.get("real_name"))
        except Exception as exc:  # noqa: BLE001 - missing users:read.email scope, network...
            print(f"slack: could not resolve user {user_id}: {exc}", file=sys.stderr)
            principal = None
        with self._lock:
            self._cache[user_id] = (principal, time.time())
        return principal


NO_ACCESS = ("You don't have access to renewal-risk data yet. Ask your admin to add your Slack email "
             "to access.yaml (and make sure the app has the users:read.email scope).")


def build_app(ctx: AppContext | None = None):
    try:
        from slack_bolt import App
    except ImportError as exc:
        raise RuntimeError("the Slack app needs `pip install slack_bolt`") from exc
    ctx = ctx or get_context()
    cfg = ctx.settings.slack or {}
    if not cfg.get("bot_token"):
        raise RuntimeError("slack.yaml: bot_token is not set (SLACK_BOT_TOKEN)")
    app = App(token=cfg["bot_token"], signing_secret=cfg.get("signing_secret") or None,
              token_verification_enabled=bool(cfg.get("verify_token", True)))
    identity = SlackIdentity(ctx, app.client)
    command = cfg.get("command", "/renewal-risk")
    visibility = cfg.get("reply_visibility", "ephemeral")

    terms = ctx.settings.pack.get("terminology") or {}

    def answer(principal: Principal, question: str) -> tuple[str, list]:
        """Always returns something to post: an error becomes a readable reply, never silence."""
        started = time.time()
        try:
            result = ctx.service.ask(principal, question)
        except (AccessDenied, ConnectorError, LLMError, PolicyError) as exc:
            result = {"type": "error", "message": str(exc)}
        except Exception as exc:  # noqa: BLE001 - the asker must hear back
            print(f"slack: question from {principal.email} failed: {exc!r}", file=sys.stderr)
            result = {"type": "error", "message": "Something went wrong while answering — the admin can see the details in the service log."}
        print(f"slack: {principal.email} asked {question!r} -> {result.get('type')} in {time.time() - started:.1f}s", file=sys.stderr)
        return kit.answer_blocks(result, terms)

    @app.command(command)
    def on_command(ack, command, respond):
        question = (command.get("text") or "").strip()
        if not question or question.lower() in ("help", "?"):
            ack()
            respond(response_type="ephemeral", text=f"Usage: `{command['command']} Acme` · `{command['command']} at risk this quarter` · "
                                                    f"`{command['command']} high risk renewals next 90 days`")
            return
        ack(response_type="ephemeral", text=f":hourglass_flowing_sand: Looking into _{kit._escape(question)}_…")
        principal = identity.principal(command["user_id"])
        if principal is None:
            print(f"slack: no access for user {command['user_id']}", file=sys.stderr)
            respond(response_type="ephemeral", text=NO_ACCESS)
            return
        text, body = answer(principal, question)
        respond(response_type=visibility, text=text, blocks=body, replace_original=False)

    def handle_conversation(event, say, client):
        question = _MENTION.sub("", event.get("text") or "").strip()
        thread = event.get("thread_ts") or event["ts"]
        principal = identity.principal(event["user"])
        if principal is None:
            say(text=NO_ACCESS, thread_ts=thread)
            return
        if not question:
            say(text="Ask me about a company (`is Acme at risk`) or a window (`at risk this quarter`).", thread_ts=thread)
            return
        placeholder = say(text=":hourglass_flowing_sand: Looking into it…", thread_ts=thread)
        text, body = answer(principal, question)
        try:
            client.chat_update(channel=event["channel"], ts=placeholder["ts"], text=text, blocks=body)
        except Exception:  # noqa: BLE001 - fall back to a new message
            say(text=text, blocks=body, thread_ts=thread)

    @app.event("app_mention")
    def on_mention(event, say, client):
        handle_conversation(event, say, client)

    @app.event("message")
    def on_dm(event, say, client):
        if event.get("channel_type") == "im" and not event.get("bot_id") and not event.get("subtype"):
            handle_conversation(event, say, client)

    def _account_from(body) -> str:
        return json.loads(body["actions"][0]["value"])["account_id"]

    @app.action("rr_view_evidence")
    def on_evidence(ack, body, client):
        ack()
        principal = identity.principal(body["user"]["id"])
        if principal is None:
            return
        try:
            view = ctx.service.account_assessment(principal, _account_from(body))
            client.views_open(trigger_id=body["trigger_id"], view=kit.evidence_modal(view))
        except AccessDenied as exc:
            client.chat_postEphemeral(channel=body["channel"]["id"], user=body["user"]["id"], text=str(exc))

    @app.action("rr_details")
    def on_details(ack, body, respond):
        ack()
        principal = identity.principal(body["user"]["id"])
        if principal is None:
            respond(response_type="ephemeral", text=NO_ACCESS, replace_original=False)
            return
        try:
            view = ctx.service.account_assessment(principal, _account_from(body))
            respond(response_type="ephemeral", text=view["account_name"], blocks=kit.assessment_blocks(view), replace_original=False)
        except (AccessDenied, ConnectorError, LLMError, PolicyError) as exc:
            respond(response_type="ephemeral", text=str(exc), replace_original=False)

    def _feedback(verdict: str):
        def handler(ack, body, respond):
            ack()
            principal = identity.principal(body["user"]["id"])
            if principal is None:
                return
            data = json.loads(body["actions"][0]["value"])
            try:
                ctx.service._visible(principal, data["account_id"])
            except AccessDenied:
                return
            ctx.store.add_feedback(data["account_id"], principal.email, verdict, assessment_id=data.get("assessment_id"))
            respond(response_type="ephemeral", replace_original=False,
                    text="Thanks — noted. Your feedback is used to check and tune the agent." if verdict == "agree"
                    else "Thanks — noted as wrong. An admin can review it on the Health & feedback page.")
        return handler

    app.action("rr_feedback_agree")(_feedback("agree"))
    app.action("rr_feedback_disagree")(_feedback("disagree"))

    def _action_status(status: str):
        """Done / Snooze / Not relevant on an action DM: only its owner (or an admin) may answer."""
        def handler(ack, body, respond):
            ack()
            principal = identity.principal(body["user"]["id"])
            item = update_action(ctx, principal, int(json.loads(body["actions"][0]["value"])["item_id"]), status)
            if item is None:
                respond(response_type="ephemeral", text="Only the owner of this action can update it.", replace_original=False)
                return
            kept = [b for b in (body.get("message") or {}).get("blocks", []) if b.get("type") != "actions"]
            respond(replace_original=True, text=f"{item['action']} — {status.replace('_', ' ')}",
                    blocks=kept + [kit.context(kit.ACTION_STATUS.get(status, status) + f" by {principal.email}")])
        return handler

    app.action("rr_action_done")(_action_status("done"))
    app.action("rr_action_snooze")(_action_status("snoozed"))
    app.action("rr_action_not_relevant")(_action_status("not_relevant"))

    @app.action("rr_rerun")
    def on_rerun(ack, body, respond):
        ack()
        principal = identity.principal(body["user"]["id"])
        if principal is None:
            respond(response_type="ephemeral", text=NO_ACCESS, replace_original=False)
            return
        respond(response_type="ephemeral", text=":hourglass_flowing_sand: Running a fresh assessment…", replace_original=False)
        try:
            view = ctx.service.account_assessment(principal, _account_from(body), fresh=True)
            respond(response_type="ephemeral", text=view["account_name"], blocks=kit.assessment_blocks(view), replace_original=False)
        except (AccessDenied, ConnectorError, LLMError, PolicyError) as exc:
            respond(response_type="ephemeral", text=str(exc), replace_original=False)

    return app


def update_action(ctx: AppContext, principal, item_id: int, status: str) -> dict | None:
    """Record Done / Snooze / Not relevant on an action item. Only its owner or an
    admin may; returns the updated item, or None when not allowed."""
    item = ctx.store.action_item(item_id)
    if principal is None or item is None or (principal.email != item["owner_email"] and not principal.can("admin")):
        return None
    snooze = float(((ctx.settings.slack or {}).get("actions") or {}).get("snooze_days", 7)) if status == "snoozed" else None
    ctx.store.add_feedback(item["account_id"], principal.email, note=f"action {status}: {item['action']}", assessment_id=item["assessment_id"])
    return ctx.store.set_action_status(item["id"], status, snooze)


def run(ctx: AppContext | None = None, block: bool = True):
    """Start the Slack app. Socket mode by default; http mode serves on SLACK_PORT (3000)."""
    ctx = ctx or get_context()
    cfg = ctx.settings.slack or {}
    app = build_app(ctx)
    if cfg.get("mode", "socket") == "socket":
        from slack_bolt.adapter.socket_mode import SocketModeHandler

        if not cfg.get("app_token"):
            raise RuntimeError("slack.yaml: app_token (SLACK_APP_TOKEN, xapp-...) is required for socket mode")
        handler = SocketModeHandler(app, cfg["app_token"])
        if block:
            handler.start()
        else:
            handler.connect()
        return handler
    import os
    app.start(port=int(os.environ.get("SLACK_PORT", "3000")))
    return app


class SlackNotifier:
    """Sends alerts. Used by the scheduler; independent of the Bolt app."""

    def __init__(self, ctx: AppContext):
        from slack_sdk import WebClient

        cfg = ctx.settings.slack or {}
        if not cfg.get("bot_token"):
            raise RuntimeError("slack.yaml: bot_token is not set — alerts cannot be sent")
        self.ctx = ctx
        self.cfg = cfg.get("alerts") or {}
        self.client = WebClient(token=cfg["bot_token"])
        self._channels: dict[str, str] = {}  # email -> DM channel, looked up once per notifier

    def _dm(self, email: str, text: str, body: list) -> bool:
        try:
            channel = self._channels.get(email)
            if channel is None:
                user = self.client.users_lookupByEmail(email=email)["user"]["id"]
                channel = self._channels[email] = self.client.conversations_open(users=user)["channel"]["id"]
            self.client.chat_postMessage(channel=channel, text=text, blocks=body)
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"slack: could not DM {email}: {exc}", file=sys.stderr)
            return False

    def send_alert(self, view: dict, account: dict, previous_band: str | None) -> list[str]:
        terms = self.ctx.settings.pack.get("terminology") or {}
        text = f"{kit.cap(terms.get('renewal', 'renewal'))} risk alert: {view['account_name']} is now {view['band']} risk ({view['score']}/100)"
        body = kit.alert_blocks(view, previous_band, terms)
        delivered = []
        if self.cfg.get("channel"):
            try:
                self.client.chat_postMessage(channel=self.cfg["channel"], text=text, blocks=body)
                delivered.append(self.cfg["channel"])
            except Exception as exc:  # noqa: BLE001
                print(f"slack: could not post to {self.cfg['channel']}: {exc}", file=sys.stderr)
        recipients = set()
        if self.cfg.get("dm_owner", True) and account.get("owner_email"):
            # Only DM owners who are themselves allowed to see the account.
            owner = self.ctx.policy.principal_for_email(account["owner_email"])
            from ..access.rbac import can_view
            if owner and can_view(owner, account):
                recipients.add(owner.email)
        if self.cfg.get("dm_roles"):
            recipients.update(self.ctx.policy.emails_who_can_view(account, self.cfg["dm_roles"]))
        for email in sorted(recipients):
            if self._dm(email, text, body):
                delivered.append(email)
        return delivered
