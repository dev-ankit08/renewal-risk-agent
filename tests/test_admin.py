"""Admin UI back end: first run, connecting sources, the mapping agent, matching, signals, access, packs."""
import shutil

import pytest
import yaml

from tests.conftest import ROOT, TODAY, FakeLLM

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from renewal_risk.admin.mapping_agent import MappingAgent, candidates, signal_needs  # noqa: E402
from renewal_risk.core.settings import all_signals, load_settings  # noqa: E402


def test_first_run_login_and_auth_required(admin, demo_config):
    from renewal_risk.service.api import app

    anonymous = TestClient(app)
    assert anonymous.get("/admin/api/sources").status_code == 401
    assert anonymous.post("/admin/api/login", json={"email": "admin@demo.example", "password": "nope-nope-nope"}).status_code == 401
    assert anonymous.post("/admin/api/login", json={"email": "admin@demo.example", "password": "long-enough-pw"}).status_code == 200
    assert anonymous.get("/admin/api/me").json()["email"] == "admin@demo.example"
    assert admin.get("/admin/api/status").json()["needs_setup"] is False
    assert admin.post("/admin/api/setup", json={"setup_token": "x", "email": "b@x.com", "password": "long-enough-pw"}).status_code == 409
    # The demo's API key maps to an admin user, so automation can use the admin API too.
    assert anonymous.get("/admin/api/sources", headers={"X-API-Key": "demo-key-change-me"}).status_code == 200


def test_catalog_by_category_for_each_pack(admin):
    cat = admin.get("/admin/api/catalog").json()
    assert cat["pack"]["name"] == "saas"
    by_key = {c["key"]: c for c in cat["categories"]}
    assert {"accounts", "billing", "support", "usage"} <= set(by_key)
    support_systems = {s["preset"] for s in by_key["support"]["systems"]}
    assert {"zendesk", "freshdesk", "salesforce", "hubspot"} <= support_systems
    sf = next(s for s in by_key["accounts"]["systems"] if s["preset"] == "salesforce")
    oauth_method = next(m for m in sf["auth_methods"] if m["method"] == "oauth2")
    assert oauth_method["oauth"] and "/admin/oauth/callback" in oauth_method["help"]
    assert any(p["name"] == "client_secret" and p["secret"] for p in oauth_method["params"])
    assert by_key["accounts"]["configured_sources"] == ["crm"]

    assert admin.put("/admin/api/pack", json={"pack": "it_services"}).json()["ok"]
    cat = admin.get("/admin/api/catalog").json()
    assert cat["pack"]["terminology"]["renewal"] == "SOW renewal"
    delivery = next(c for c in cat["categories"] if c["key"] == "delivery")
    assert "jira" in {s["preset"] for s in delivery["systems"]}


def test_saving_a_source_keeps_secrets_out_of_yaml(admin, demo_config):
    r = admin.put("/admin/api/sources/payments", json={"preset": "stripe", "auth_method": "token", "secrets": {"api_key": "rk_test_SECRET123"}})
    assert r.status_code == 200, r.text
    text = (demo_config / "connectors.yaml").read_text(encoding="utf-8")
    assert "rk_test_SECRET123" not in text and "${SECRET:sources/payments/api_key}" in text
    sources = {s["name"]: s for s in admin.get("/admin/api/sources").json()["sources"]}
    assert sources["payments"]["connection"]["credentials_complete"] is True
    assert sources["payments"]["auth_method"] == "token"
    # Editing without re-entering the secret keeps it.
    admin.put("/admin/api/sources/payments", json={"preset": "stripe", "auth_method": "token", "disabled_entities": ["line_item"]})
    from renewal_risk.service.context import get_context
    src = get_context().settings.sources["payments"]
    assert src["connection"]["auth"]["token"] == "rk_test_SECRET123" and "line_item" not in src["entities"]
    assert admin.delete("/admin/api/sources/payments").json()["ok"]
    assert "payments" not in yaml.safe_load((demo_config / "connectors.yaml").read_text(encoding="utf-8"))["sources"]
    assert admin.put("/admin/api/sources/Bad-Name", json={"preset": "stripe"}).status_code == 400


def test_oauth_connect_flow_through_the_api(admin, monkeypatch):
    import httpx2

    from renewal_risk.auth import oauth as oauth_mod

    r = admin.put("/admin/api/sources/sfdc", json={"preset": "salesforce", "auth_method": "oauth2",
                                                   "params": {"client_id": "3MVG-demo", "login_host": "test.salesforce.com"},
                                                   "secrets": {"client_secret": "shh"}})
    assert r.status_code == 200, r.text
    start = admin.post("/admin/api/sources/sfdc/oauth/start").json()
    assert start["authorize_url"].startswith("https://test.salesforce.com/services/oauth2/authorize?")
    state = start["authorize_url"].split("state=")[1].split("&")[0]

    def token_endpoint(request):
        assert b"client_secret=shh" in request.content
        return httpx2.Response(200, json={"access_token": "00D-token", "refresh_token": "r", "instance_url": "https://acme.my.salesforce.com"})
    monkeypatch.setattr(oauth_mod, "_http", lambda transport=None: httpx2.Client(transport=httpx2.MockTransport(token_endpoint)))
    cb = admin.get(f"/admin/oauth/callback?state={state}&code=abc", follow_redirects=False)
    assert cb.status_code in (302, 307) and "connected=sfdc" in cb.headers["location"]
    sfdc = next(s for s in admin.get("/admin/api/sources").json()["sources"] if s["name"] == "sfdc")
    assert sfdc["connection"]["oauth_connected"] is True and sfdc["type"] == "salesforce_rest"
    bad = admin.get("/admin/oauth/callback?state=nope&code=abc", follow_redirects=False)
    assert "oauth_error" in bad.headers["location"]


def test_mapping_agent_on_custom_salesforce_names():
    described = {"fields": [
        {"name": "Id", "type": "id", "examples": ["006A"]},
        {"name": "Renewal_Date__c", "label": "Renewal Date", "type": "date", "description": "Date the renewal is due", "examples": ["2026-12-01"]},
        {"name": "Deal_Value__c", "label": "Deal value", "type": "currency", "examples": ["120000"]},
        {"name": "Is_Lost__c", "label": "Is lost", "type": "boolean"},
        {"name": "Description", "label": "Description", "type": "textarea", "free_text": True},
    ]}
    cands = candidates("renewal", described["fields"])
    assert cands["close_date"][0]["source_field"] == "Renewal_Date__c"
    assert cands["amount"][0]["source_field"] == "Deal_Value__c"
    assert all(c["source_field"] != "Description" for ranked in cands.values() for c in ranked)  # free text never offered
    rules = MappingAgent(None, all_signals({})).suggest("renewal", described)
    by_field = {s["field"]: s for s in rules["suggestions"]}
    assert by_field["close_date"]["expr"] == "Renewal_Date__c | date"
    assert "is_open" in rules["required_missing"]
    assert "opp_stalled" in rules["signals_affected"]  # needs stage_changed_at
    model_result = MappingAgent(FakeLLM(), []).suggest("ticket", {"fields": [{"name": "Id"}, {"name": "Priority", "type": "picklist", "options": ["P1", "P2"]}]})
    priority = next(s for s in model_result["suggestions"] if s["field"] == "priority")
    assert priority["by"] == "model" and priority["expr"] == {"expr": "Priority", "map": {"P1": "urgent"}}


def test_signal_needs_reads_fields():
    needs = signal_needs("renewal", all_signals({}))
    assert {"is_open", "stage_changed_at"} <= needs["opp_stalled"]


def test_csv_upload_map_preview_save(admin, demo_config, tmp_path):
    assert admin.put("/admin/api/sources/legacy", json={"type": "csv"}).status_code == 200
    csv_bytes = b"TicketNo,Org,State,Sev,Opened\nT1,A001,Open,P1,2026-01-05\nT2,A002,Closed,P3,2026-01-06\n"
    up = admin.post("/admin/api/sources/legacy/upload", files={"file": ("tickets.csv", csv_bytes, "text/csv")}).json()
    assert up["columns"] == ["TicketNo", "Org", "State", "Sev", "Opened"]
    assert admin.put("/admin/api/sources/legacy/csv-entity", json={"entity": "ticket", "file": "tickets.csv", "match": "Org"}).json()["ok"]
    mapping = admin.get("/admin/api/mapping/legacy/ticket").json()
    assert {f["name"] for f in mapping["describe"]["fields"]} >= {"TicketNo", "State", "Sev"}
    sugg = admin.post("/admin/api/mapping/legacy/ticket/suggest", json={"use_model": True}).json()
    assert sugg["suggestions"]
    fields = {"id": "TicketNo", "status": "State", "is_open": "State | eq('Open')", "priority": {"expr": "Sev", "map": {"P1": "urgent", "P3": "low"}},
              "created_at": "Opened | date"}
    preview = admin.post("/admin/api/mapping/legacy/ticket/preview", json={"fields": fields}).json()
    assert preview["records"][0]["priority"] == "urgent" and preview["records"][0]["is_open"] is True
    assert admin.put("/admin/api/mapping/legacy/ticket", json={"fields": fields}).json()["ok"]
    saved = yaml.safe_load((demo_config / "mappings" / "legacy.overrides.yaml").read_text(encoding="utf-8"))
    assert saved["ticket"]["fields"]["priority"]["map"]["P1"] == "urgent"
    assert admin.put("/admin/api/mapping/legacy/ticket", json={"fields": {"status": "State"}}).status_code == 400  # id required


def test_linking_review_from_the_ui(admin):
    report = admin.get("/admin/api/linking").json()
    billing = report["sources"]["billing"]
    assert billing["linked"] == 4 and billing["review"][0]["customer_id"] == "cus_stark"
    after = admin.post("/admin/api/linking/decision", json={"source": "billing", "account_id": "A005", "link_value": "cus_stark", "status": "approved"}).json()
    assert after["sources"]["billing"]["linked"] == 5 and after["sources"]["billing"]["review"] == []


def test_signals_playbooks_access_slack_keys_feedback(admin, demo_config):
    sig = admin.get("/admin/api/signals").json()["signals"]
    assert any(s["name"] == "budget_overrun" and not s["enabled"] for s in sig)  # services signal, off in the saas pack
    r = admin.put("/admin/api/signals", json={"signals": [{"name": "no_recent_activity", "enabled": True, "thresholds": {"medium": 45, "high": 75}},
                                                          {"name": "poor_satisfaction", "enabled": False}]})
    assert r.json()["ok"]
    saved = yaml.safe_load((demo_config / "signals.yaml").read_text(encoding="utf-8"))["signals"]
    assert {"name": "poor_satisfaction", "enabled": False} in saved
    from renewal_risk.service.context import get_context
    names = {s["name"]: s for s in get_context().settings.signals}
    assert "poor_satisfaction" not in names and names["no_recent_activity"]["thresholds"]["medium"] == 45

    assert admin.put("/admin/api/access", json={"users": [{"email": "PD@Demo.example", "role": "director", "scope": {"region": ["EMEA"]}},
                                                         {"email": "", "role": "csm"}]}).json()["ok"]
    assert admin.get("/admin/api/access").json()["users"] == [{"email": "pd@demo.example", "role": "director", "scope": {"region": ["EMEA"]}}]

    key = admin.post("/admin/api/access/api-keys", json={"name": "dashboard", "as_email": "pd@demo.example"}).json()["key"]
    assert key.startswith("rr_") and key not in (demo_config / "access.yaml").read_text(encoding="utf-8")
    from renewal_risk.service.api import app
    assert TestClient(app).post("/v1/ask", json={"question": "Acme Corp"}, headers={"X-API-Key": key}).json()["band"] == "high"

    assert admin.put("/admin/api/slack", json={"bot_token": "xoxb-1", "app_token": "xapp-1", "alerts": {"channel": "#risk", "min_band": "high"}}).json()["ok"]
    assert "xoxb-1" not in (demo_config / "slack.yaml").read_text(encoding="utf-8")
    assert admin.get("/admin/api/slack").json()["bot_token_set"] is True

    assert admin.post("/admin/api/feedback", json={"account_id": "A001", "verdict": "agree", "outcome": "churned"}).json()["ok"]
    assert admin.get("/admin/api/feedback").json()["feedback"][0]["verdict"] == "agree"
    health = admin.get("/admin/api/health").json()
    assert health["errors"] == [] and "crm" in health["sources"]


@pytest.fixture()
def services_ctx(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("services_generate", ROOT / "examples" / "services-demo" / "generate.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    demo = tmp_path / "services"
    shutil.copytree(ROOT / "examples" / "services-demo" / "config", demo / "config")
    gen.build(demo / "data", TODAY)
    monkeypatch.setenv("RENEWAL_RISK_STATE_DIR", str(tmp_path / "state"))
    from renewal_risk.service.context import build_context
    context = build_context(load_settings(demo / "config"), llm=FakeLLM(fabricate=False))
    yield context
    context.engine.close()


def test_it_services_assessment(services_ctx):
    settings = services_ctx.settings
    assert settings.pack["name"] == "it_services" and "delivery" in settings.agent["specialists"]
    result = services_ctx.engine.assess("C001", account_name="Northwind Bank")
    signals = {s["name"] for p in result.packets for s in p["signals"]}
    assert {"budget_overrun", "project_red", "missed_milestones", "sow_ending_no_renewal", "open_escalations"} <= signals
    assert result.assessment["band"] == "high"
    assert any(p["specialist"] == "delivery" for p in result.packets)
    contoso = services_ctx.engine.prescreen("C002")
    assert "billable_hours_drop" in {s["name"] for s in contoso["signals"]}
    assert settings.playbooks["owner_roles"][0] == {"name": "Project director", "find": {"access_role": "director"}}


URGENT = {"name": "urgent_ticket_pileup", "description": "Several urgent tickets open at once.", "specialist": "relationship",
          "rule": "count", "entity": "ticket", "where": {"is_open": True}, "thresholds": {"medium": 2, "high": 3},
          "how_calculated_note": "Agreed with the support team in Q3."}


def test_custom_signals_preview_test_save_and_survive_threshold_saves(admin, demo_config):
    builder = admin.get("/admin/api/signals/builder").json()
    assert "count" in builder["rules"] and "ticket" in builder["entities"] and "relationship" in builder["specialists"]

    preview = admin.post("/admin/api/signals/preview", json={"signal": URGENT}).json()
    assert preview == {"explanation": "Counts tickets that are open. Medium at 2 or more, high at 3 or more.", "errors": []}
    clash = admin.post("/admin/api/signals/preview", json={"signal": {**URGENT, "name": "open_ticket_backlog"}}).json()
    assert any("built-in" in e for e in clash["errors"])
    assert admin.post("/admin/api/signals/custom", json={"signal": {**URGENT, "where": {"colour": "red"}}}).status_code == 400

    tested = admin.post("/admin/api/signals/test", json={"signal": URGENT}).json()
    by_id = {r["account_id"]: r for r in tested["results"]}
    assert tested["tested"] >= 1 and by_id["A001"]["fired"] and by_id["A001"]["severity"] == "high"

    assert admin.post("/admin/api/signals/custom", json={"signal": URGENT}).json()["ok"]
    rows = {s["name"]: s for s in admin.get("/admin/api/signals").json()["signals"]}
    custom = rows["urgent_ticket_pileup"]
    assert custom["origin"] == "custom" and custom["spec"]["author"] == "admin@demo.example"
    assert custom["how_calculated_note"] == URGENT["how_calculated_note"] and custom["explanation"].startswith("Counts tickets")
    assert rows["open_ticket_backlog"]["origin"] == "built-in"

    # Regression: saving thresholds must keep custom signals (it used to drop them).
    edits = [{**s, "thresholds": {"medium": 3, "high": 5}} if s["name"] == "urgent_ticket_pileup" else s for s in rows.values()]
    assert admin.put("/admin/api/signals", json={"signals": edits}).json()["ok"]
    saved = {s["name"]: s for s in yaml.safe_load((demo_config / "signals.yaml").read_text(encoding="utf-8"))["signals"]}
    assert saved["urgent_ticket_pileup"]["custom"] and saved["urgent_ticket_pileup"]["thresholds"] == {"medium": 3, "high": 5}

    from renewal_risk.service.context import get_context
    assert "urgent_ticket_pileup" in {s["name"] for s in get_context().settings.signals}  # live after reload

    # Rename keeps authorship; delete removes it.
    assert admin.post("/admin/api/signals/custom", json={"signal": {**URGENT, "name": "urgent_pileup"}, "original_name": "urgent_ticket_pileup"}).json()["ok"]
    names = {s["name"] for s in admin.get("/admin/api/signals").json()["signals"]}
    assert "urgent_pileup" in names and "urgent_ticket_pileup" not in names
    assert admin.delete("/admin/api/signals/custom/urgent_pileup").json()["ok"]
    assert admin.delete("/admin/api/signals/custom/open_ticket_backlog").status_code == 404


def test_ai_drafts_a_signal_for_review(admin):
    draft = admin.post("/admin/api/signals/draft", json={"text": "flag accounts with 3 or more open urgent tickets"}).json()
    assert draft["signal"]["name"] == "urgent_ticket_pileup" and draft["signal"]["rule"] == "count"
    assert draft["errors"] == [] and "tickets that are open" in draft["explanation"]
    assert admin.post("/admin/api/signals/draft", json={"text": "hi"}).status_code == 400
    assert "urgent_ticket_pileup" not in {s["name"] for s in admin.get("/admin/api/signals").json()["signals"]}  # never auto-saved


def test_playbook_owner_roles_and_action_settings(admin, demo_config):
    assert admin.post("/admin/api/signals/custom", json={"signal": URGENT}).json()["ok"]
    book = admin.get("/admin/api/playbooks").json()
    assert "urgent_ticket_pileup" in book["signals"]
    play = {"name": "Ticket swarm", "signals": ["urgent_ticket_pileup"], "action": "Swarm the open tickets", "steps": ["Assign owners"],
            "owner_role": "Finance team", "min_severity": "high", "link": "https://wiki.example/swarm"}
    roles = book["owner_roles"] + [{"name": "Finance team", "find": {"emails": ["admin@demo.example"]}}]
    assert admin.put("/admin/api/playbooks", json={"owner_roles": roles, "plays": book["plays"] + [play]}).json()["ok"]
    saved = yaml.safe_load((demo_config / "playbooks.yaml").read_text(encoding="utf-8"))
    assert saved["plays"][-1]["id"] == "ticket_swarm" and saved["owner_roles"][-1]["find"] == {"emails": ["admin@demo.example"]}
    assert admin.put("/admin/api/playbooks", json={"plays": [{**play, "signals": ["nope"]}]}).status_code == 400
    assert admin.put("/admin/api/playbooks", json={"plays": [{**play, "link": "ftp://x"}]}).status_code == 400

    assert admin.get("/admin/api/slack").json()["actions"] == {"dm_owners": True, "min_band": "high", "snooze_days": 7, "cooldown_days": 30}
    assert admin.put("/admin/api/slack/actions", json={"dm_owners": False, "min_band": "low", "snooze_days": 500}).json()["ok"]
    assert admin.get("/admin/api/slack").json()["actions"] == {"dm_owners": False, "min_band": "high", "snooze_days": 90, "cooldown_days": 30}
    assert admin.get("/admin/api/actions").json() == {"actions": [], "summary": {"total": 0, "open": 0, "done": 0, "snoozed": 0, "not_relevant": 0, "overdue": 0, "completion_rate": None}}
    assert admin.get("/admin/api/health").json()["actions"]["total"] == 0
