"""Access control, plain-English questions, Slack rendering, REST API, alerts."""
from datetime import date

import pytest

from renewal_risk import nlquery
from renewal_risk.access.rbac import AccessPolicy, can_view
from renewal_risk.slack import blocks as kit


def test_rbac_scopes():
    policy = AccessPolicy({"users": [
        {"email": "pd@x.com", "role": "director", "scope": {"region": ["EMEA"]}},
        {"email": "csm@x.com", "role": "csm"},
        {"email": "vp@x.com", "role": "director", "scope": "all"},
    ], "domains": [{"domain": "x.com", "role": "viewer", "scope": {"owner_email": "self"}}]})
    emea = {"id": "1", "region": "emea", "owner_email": "csm@x.com"}
    amer = {"id": "2", "region": "AMER", "owner_email": "other@x.com"}
    pd, csm, vp = (policy.principal_for_email(e) for e in ("PD@x.com", "csm@x.com", "vp@x.com"))
    assert can_view(pd, emea) and not can_view(pd, amer)
    assert can_view(csm, emea) and not can_view(csm, amer)  # no scope = own accounts
    assert can_view(vp, amer)
    assert policy.principal_for_email("someone@x.com").role == "viewer"
    assert policy.principal_for_email("stranger@y.com") is None
    assert not pd.can("admin") and csm.can("assess") and not csm.can("bulk")
    assert policy.emails_who_can_view(emea, ["director"]) == ["pd@x.com", "vp@x.com"]


@pytest.mark.parametrize("text,intent,detail", [
    ("Acme Corp", "single_account", "Acme Corp"),
    ("is Acme Corp at risk?", "single_account", "Acme Corp"),
    ("why is Globex high risk", "single_account", "Globex"),
    ("how risky is Initech", "single_account", "Initech"),
    ("accounts at risk this quater", "bulk_window", "this quarter"),
    ("high risk renewals next 90 days", "bulk_window", "next 90 days"),
    ("renewals in Q1 2027", "bulk_window", "Q1 2027"),
    ("at risk before 2026-12-31", "bulk_window", "before 2026-12-31"),
])
def test_nlquery(text, intent, detail):
    got, params = nlquery.parse_intent(text, today=date(2026, 10, 10))
    assert got == intent
    assert (params.get("company_name") or params.get("label")) == detail


def test_nlquery_band_and_help():
    _, params = nlquery.parse_intent("high risk renewals this quarter", today=date(2026, 10, 10))
    assert params["band_filter"] == "high" and params["start"] == date(2026, 10, 1) and params["end"] == date(2026, 12, 31)
    with pytest.raises(nlquery.IntentParseError):
        nlquery.parse_intent("help")


def test_service_enforces_scope(ctx):
    svc, policy = ctx.service, ctx.policy
    pd = policy.principal_for_email("pd.emea@demo.example")
    answer = svc.ask(pd, "is Acme Corp at risk")
    assert answer["type"] == "single_account" and answer["band"] == "high"
    assert svc.ask(pd, "Initech")["type"] == "not_found"  # AMER account: invisible, not "forbidden"
    csm = policy.principal_for_email("csm.one@demo.example")
    assert svc.ask(csm, "accounts at risk this quarter")["type"] == "denied"  # csm has no bulk permission
    assert svc.ask(policy.principal_for_email("admin@demo.example"), "Stark")["type"] == "single_account"


def test_second_question_reuses_stored_assessment(ctx):
    admin = ctx.policy.principal_for_email("admin@demo.example")
    first = ctx.service.ask(admin, "Acme Corp")
    calls = len(ctx.engine.llm.calls)
    second = ctx.service.ask(admin, "Acme Corp")
    assert first["from_cache"] is False and second["from_cache"] is True
    assert len(ctx.engine.llm.calls) == calls


def test_portfolio_screens_all_and_assesses_flagged(ctx):
    admin = ctx.policy.principal_for_email("admin@demo.example")
    result = ctx.service.ask(admin, "accounts at risk next 120 days")
    assert result["type"] == "bulk_window"
    assert result["renewals_in_window"] == 6
    assert result["assessed_now"] == 3  # demo agent.yaml caps paid assessments at 3
    assert result["rows"][0]["band"] == "high"
    text, body = kit.answer_blocks(result)
    assert body[0]["type"] == "section" and len(body) <= 50
    text, body = kit.answer_blocks(result, {"renewals": "SOW renewals"})
    assert text.startswith("SOW renewals")
    assert "renewals" in body[0]["text"]["text"].lower()


def test_slack_blocks_for_assessment(ctx):
    admin = ctx.policy.principal_for_email("admin@demo.example")
    view = ctx.service.ask(admin, "Acme Corp")
    text, body = kit.answer_blocks(view)
    assert "Acme Corp" in text
    assert body[-1]["type"] == "actions"
    assert {e["action_id"] for e in body[-1]["elements"]} == {"rr_view_evidence", "rr_rerun", "rr_feedback_agree", "rr_feedback_disagree"}
    modal = kit.evidence_modal(view)
    assert modal["type"] == "modal" and len(modal["blocks"]) >= 5
    assert all(len(b["text"]["text"]) <= 3000 for b in body if b["type"] == "section")


def test_api_auth_and_endpoints(ctx, monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from renewal_risk.service.api import app

    monkeypatch.delenv("RENEWAL_RISK_DEV_USER", raising=False)
    client = TestClient(app)
    assert client.get("/v1/health").json()["primary"] == "crm"
    assert client.post("/v1/ask", json={"question": "Acme"}).status_code == 401
    headers = {"X-API-Key": "demo-key-change-me"}
    r = client.post("/v1/ask", json={"question": "Acme Corp"}, headers=headers)
    assert r.status_code == 200 and r.json()["band"] == "high"
    assert client.get("/v1/accounts/search", params={"q": "glob"}, headers=headers).json()["results"][0]["account_id"] == "A002"
    assert client.get("/v1/accounts/NOPE/assessment", headers=headers).status_code == 403
    assert client.get("/v1/connectors/test", headers=headers).json()["crm"]["ok"] is True
    assert "Renewal Risk Agent" in client.get("/").text


def test_scan_alerts_once_per_rise(ctx):
    from renewal_risk.service.scheduler import scan

    class Notifier:
        def __init__(self):
            self.sent = []

        def send_alert(self, view, account, previous):
            self.sent.append(view["account_id"])
            return ["#test"]

    notifier = Notifier()
    first = scan(ctx, notifier)
    assert first["renewals"] == 6 and set(notifier.sent) >= {"A001"}
    count = len(notifier.sent)
    scan(ctx, notifier)
    assert len(notifier.sent) == count  # same band again: no duplicate alert
