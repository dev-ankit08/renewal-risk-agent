"""Accuracy (backtest on closed renewals), "not enough data" coverage, the setup checklist, auto-map, the demo workspace."""
import hashlib
from datetime import timedelta

from renewal_risk.core import backtest
from renewal_risk.core.pipeline import coverage_verdict, rules_config
from renewal_risk.service import action_notifier
from renewal_risk.slack import blocks as kit
from tests.conftest import TODAY

WINDOW = {"start": (TODAY - timedelta(days=730)).isoformat(), "end": TODAY.isoformat()}


def test_backtest_replays_closed_renewals_without_leaking_the_answer(admin):
    r = admin.post("/admin/api/backtest", json=WINDOW).json()
    assert (r["tested"], r["at_risk"]) == (6, 3)
    assert {k: r["metrics"][k] for k in ("caught", "missed", "false_alarms")} == {"caught": 2, "missed": 1, "false_alarms": 1}
    assert r["baseline_caught"] == 1  # "biggest deals first" catches one
    by_name = {c["name"]: c for c in r["cases"]}
    assert by_name["Acme Corp"]["outcome"] == "downgraded" and by_name["Umbrella Health"]["outcome"] == "churned"
    assert by_name["Acme Corp"]["as_of"] == (TODAY - timedelta(days=410 + 90)).isoformat()
    fired = {s["name"] for c in r["cases"] for s in c["fired"]}
    assert not fired & {"amount_decrease", "opp_stalled", "renewal_below_arr"}  # the final stage and amount never leak in
    assert {s["name"] for s in r["signals"]} == {"support_escalation", "poor_satisfaction"}
    assert admin.get("/admin/api/backtest").json()["metrics"] == r["metrics"]  # the last run is kept

    # An uploaded outcome wins over the CRM's stage.
    wayne = by_name["Wayne Retail"]
    csv = f"account_id,close_date,outcome\n{wayne['account_id']},{wayne['close_date']},renewed\nbad,row,maybe\n"
    assert admin.post("/admin/api/backtest/outcomes", files={"file": ("outcomes.csv", csv.encode(), "text/csv")}).json()["rows"] == 1
    again = admin.post("/admin/api/backtest", json=WINDOW).json()
    assert again["at_risk"] == 2 and again["metrics"]["missed"] == 0


def test_outcomes_signals_and_weight_search():
    cfg = backtest.DEFAULTS
    case = {"account_id": "A", "close_date": "2025-01-01", "amount": 90, "prior_amount": 100}
    assert backtest.outcome_of({**case, "stage": "Closed Lost"}, None, cfg, {}) == "churned"
    assert backtest.outcome_of({**case, "stage": "Closed Won"}, None, cfg, {}) == "downgraded"
    assert backtest.outcome_of({**case, "stage": "Closed Won", "prior_amount": None}, 80, cfg, {}) == "renewed"
    assert backtest.outcome_of({**case, "stage": "Negotiation"}, None, cfg, {}) is None
    assert backtest.outcome_of({**case, "stage": "Closed Lost"}, None, cfg, {("A", "2025-01-01"): "renewed"}) == "renewed"
    assert backtest.approximate({"rule": "count", "entity": "ticket", "where": {"is_open": True}})
    assert not backtest.approximate({"rule": "count", "entity": "ticket", "where": {"is_escalated": True}})

    # Two medium signs before every loss, one before every renewal: the current weights miss the losses.
    medium = lambda n: [{"name": f"s{i}", "severity": "medium"} for i in range(n)]  # noqa: E731
    cases = [{"fired": medium(2), "enough": True, "outcome": "churned", "amount": 1}] * 3 + \
            [{"fired": medium(1), "enough": True, "outcome": "renewed", "amount": 1}] * 3
    current = rules_config({})
    assert backtest.metrics(cases, current)["caught"] == 0
    better = backtest.suggest(cases, current)  # pure: works on the signals already found, reads no data
    assert backtest.metrics(cases, better) == {**backtest.metrics(cases, better), "caught": 3, "false_alarms": 0}


def test_apply_suggested_weights(admin):
    from renewal_risk.service.context import get_context

    bad = {"weights": {"high": 50, "medium": 25, "low": 5}, "thresholds": {"high": 30, "medium": 40}}
    assert admin.put("/admin/api/rules", json=bad).status_code == 400
    assert admin.put("/admin/api/rules", json={**bad, "thresholds": {"high": 60, "medium": 25}}).json()["ok"]
    rules = get_context().engine.rules
    assert rules["weights"]["medium"] == 25 and rules["thresholds"] == {"high": 60, "medium": 25} and rules["min_coverage"] == 0.4


def test_not_enough_data_is_never_low_risk(ctx):
    assert coverage_verdict({"x": {"signals": [], "checked": ["a"]}}, 10, 0.4) == {"checked": 1, "total": 10, "missing": 9, "enough": False}
    assert coverage_verdict({"x": {"signals": [{"name": "a"}], "checked": ["a"]}}, 10, 0.4)["enough"]  # a fired sign is always enough

    admin = ctx.policy.principal_for_email("admin@demo.example")
    ctx.settings.agent["llm"]["mode"] = "rules"
    full = ctx.service.ask(admin, "Acme Corp")
    assert full["band"] == "high" and not full["abstained"] and full["coverage"]["enough"]
    ctx.settings.agent["rules"] = {"min_coverage": 0.6}  # Wayne: nothing fired, only 9 of 17 signs had data to check
    quiet = ctx.service.ask(admin, "Wayne Retail")
    assert quiet["abstained"] and quiet["coverage"]["checked"] < quiet["coverage"]["total"]
    assert kit.verdict(quiet) == "Not enough data" and "Low risk" not in str(kit.answer_blocks(quiet)[1])
    assert "Warning signs checked" in str(kit.assessment_blocks(quiet))
    assert action_notifier.notify(ctx, {**full, "abstained": True}, ctx.service.account("A001"), send=lambda *a: True) == []
    window = ctx.service.ask(admin, "renewals next 120 days")
    assert any(r["band_source"] == "not_enough_data" for r in window["rows"])


def test_setup_checklist_and_auto_map(admin, demo_config):
    items = {i["key"]: i for i in admin.get("/admin/api/setup/checklist").json()["items"]}
    assert items["connect"]["done"] and items["signals"]["done"] and items["access"]["done"]
    assert not items["slack"]["done"] and items["slack"]["route"] == "access"
    assert not items["first_answer"]["done"]

    # A new CSV source with only its id mapped: auto-map fills the confident fields, leaves the rest for review.
    assert admin.put("/admin/api/sources/legacy", json={"type": "csv", "test_data": True}).json()["ok"]
    rows = "TicketNo,AccountId,State,Severity,OpenedOn\nT1,A001,Open,P1,2026-01-02\n"
    admin.post("/admin/api/sources/legacy/upload", files={"file": ("tickets.csv", rows.encode(), "text/csv")})
    assert admin.put("/admin/api/sources/legacy/csv-entity", json={"entity": "ticket", "file": "tickets.csv", "match": "AccountId"}).json()["ok"]
    assert not {i["key"]: i for i in admin.get("/admin/api/setup/checklist").json()["items"]}["mapping"]["done"]
    result = admin.post("/admin/api/mapping/auto").json()
    legacy = next(m for m in result["mappings"] if m["source"] == "legacy")
    assert legacy["accepted"] and "id" not in legacy["accepted"]  # the existing id mapping is kept
    saved = (demo_config / "mappings" / "legacy.overrides.yaml").read_text(encoding="utf-8")
    assert all(f"{f}:" in saved for f in legacy["accepted"])


def test_demo_workspace_switch_keeps_the_real_config(admin, demo_config):
    from renewal_risk.service.context import get_context
    from renewal_risk.slack.app import SlackIdentity

    digest = lambda: {p.name: hashlib.sha1(p.read_bytes()).hexdigest() for p in demo_config.glob("*.yaml")}  # noqa: E731
    before = digest()

    class Client:
        calls = 0

        def users_info(self, user):
            Client.calls += 1
            return {"user": {"profile": {"email": "pd.emea@demo.example"}}}

    identity = SlackIdentity(Client())
    assert identity.principal("U1") is not None and identity.principal("U1") is not None and Client.calls == 1

    assert admin.put("/admin/api/workspace", json={"name": "demo"}).json()["active"] == "demo"
    ctx = get_context()
    assert ctx.settings.state_root is not None and ctx.settings.dir.name == "config" and "workspaces" in str(ctx.settings.dir)
    assert admin.get("/admin/api/me").json()["email"] == "admin@demo.example"  # still signed in
    assert admin.get("/admin/api/status").json()["workspace"]["active"] == "demo"
    assert identity.principal("U1") is not None and Client.calls == 2  # Slack follows the switch without a restart
    ctx.service.ask(ctx.policy.principal_for_email("admin@demo.example"), "Acme Corp")
    assert ctx.store.recent_assessments(5)

    assert admin.put("/admin/api/workspace", json={"name": "home"}).json()["active"] == "home"
    home = get_context()
    assert home.settings.dir == demo_config and home.settings.state_root is None
    assert not home.store.recent_assessments(5)  # what the demo recorded is forgotten
    assert digest() == before  # the real config was never touched
    assert admin.put("/admin/api/workspace", json={"name": "nope"}).status_code == 400
