"""Accuracy (backtest on closed renewals), "not enough data" coverage, the setup checklist, auto-map, the demo workspace."""
import hashlib
import json
from datetime import date, timedelta

from renewal_risk.core import backtest
from renewal_risk.core.pipeline import coverage_verdict, rules_config, score_signals
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
    from renewal_risk.service.context import get_context
    old = {k: v for k, v in r.items() if k not in ("calibration", "thresholds", "mode")}  # saved by an older version
    (get_context().settings.state_dir / "backtest.json").write_text(json.dumps(old), encoding="utf-8")
    assert admin.get("/admin/api/backtest").json() == {}  # ignored, so the page never breaks on it

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
    good = {**bad, "thresholds": {"high": 60, "medium": 25}, "signal_factors": {"support_escalation": 1.5, "nope": 2, "poor_satisfaction": 9}}
    assert admin.put("/admin/api/rules", json=good).json()["ok"]
    rules = get_context().engine.rules
    assert rules["weights"]["medium"] == 25 and rules["thresholds"] == {"high": 60, "medium": 25} and rules["min_coverage"] == 0.4
    assert rules["signal_factors"] == {"support_escalation": 1.5}  # unknown signs and out-of-range factors are dropped


def test_overlapping_signs_in_one_section_count_less_and_factors_apply():
    rules = rules_config({})
    tickets = [{"name": n, "severity": s, "section": "support"} for n, s in
               (("high_priority_tickets", "high"), ("support_escalation", "medium"), ("open_ticket_backlog", "medium"), ("case_volume_spike", "high"))]
    assert score_signals(tickets, {**rules, "same_section_factor": 1}) == 100  # the old way: one bad support week = 100
    assert score_signals(tickets, rules) == 68  # strongest in full, the rest at half: 35 + 0.5 * (15 + 15 + 35)
    spread = [{**tickets[0], "section": "support"}, {"name": "usage_decline", "severity": "high", "section": "usage"}]
    assert score_signals(spread, rules) == 70  # independent sections still add up in full
    assert score_signals(spread, {**rules, "signal_factors": {"usage_decline": 0.5}}) == 52  # 35 + 0.5 x 35


def test_learned_factors_and_threshold_suggestions():
    # A sign that fired before 4 of 4 losses and 1 of 6 renewals earns more weight; one that never helps, less.
    def case(lost, fired, metric):
        return {"outcome": "churned" if lost else "renewed", "enough": True, "amount": 1,
                "fired": [{"name": n, "severity": "medium"} for n in fired], "metrics": {"tickets": metric}}
    cases = [case(True, ["good"], m) for m in (6, 7, 8, 9)] + [case(False, ["good"] if i == 0 else ["noise"], m) for i, m in enumerate((5, 1, 2, 2, 3, 0))]
    cases += [case(False, ["noise"], 1)] * 2
    factors = backtest.learn_factors(cases, 4 / len(cases))
    assert factors["good"] > 1.5 and factors["noise"] < 1

    sig = {"name": "tickets", "rule": "count", "thresholds": {"medium": 2, "high": 4}}
    [row] = backtest.suggest_thresholds(cases, [sig])
    assert row["status"] == "suggested" and row["suggested"]["medium"] == 6  # every loss had 6+; renewals at most 5
    assert row["then"] == {"caught": 4, "false_alarms": 0} and row["now"]["false_alarms"] > 0
    assert row["suggested"]["high"] == 6  # all flags at 6+ were losses
    thin = backtest.suggest_thresholds(cases[:5], [sig])
    assert thin[0]["status"] == "not_enough"  # never guesses from a handful of renewals
    below = {"name": "tickets", "rule": "ratio", "thresholds": {"medium": 0.5}}  # smaller is worse
    assert backtest.suggest_thresholds([{**c, "metrics": {"tickets": -c["metrics"]["tickets"]}} for c in cases], [below])[0]["suggested"]["medium"] == -6


def _closed(ctx):
    return ctx.engine.list_renewals(TODAY.isoformat(), (TODAY - timedelta(days=730)).isoformat(), closed=True)


def _predict(ctx, row, days_before, band, fired=()):
    predicted_on = (date.fromisoformat(row["close_date"]) - timedelta(days=days_before)).isoformat()
    signs = [{"name": n, "severity": "high", "section": n, "metric": 3} for n in fired]
    ctx.store.record_prediction(row, {"as_of": predicted_on, "score": 70 if band == "high" else 0, "band": band, "signals": signs,
                                      "metrics": {n: 3 for n in fired}, "coverage": {"enough": True}}, "test")


def test_predictions_are_saved_then_scored_against_what_happened(admin):
    from renewal_risk.service.context import get_context

    ctx = get_context()
    principal = ctx.policy.principal_for_email("admin@demo.example")
    ctx.settings.agent["llm"]["mode"] = "rules"
    ctx.service.ask(principal, "renewals next 120 days")  # the quick check of every open renewal is kept
    open_refs = [r["renewal_ref"] for r in ctx.engine.list_renewals((TODAY + timedelta(days=120)).isoformat(), TODAY.isoformat())]
    saved = ctx.store.predictions_for(open_refs)
    assert saved and all(p[0]["predicted_on"] == TODAY.isoformat() and "metrics" in p[0] for p in saved.values())

    # What the agent said 100 days before each closed renewal (too late for one of them).
    closed = {r["name"]: r for r in _closed(ctx)}
    _predict(ctx, closed["Umbrella Health"], 100, "high", ["support_escalation", "poor_satisfaction"])
    _predict(ctx, closed["Acme Corp"], 100, "high", ["support_escalation", "poor_satisfaction"])
    _predict(ctx, closed["Wayne Retail"], 100, "low")
    _predict(ctx, closed["Initech"], 20, "high", ["support_escalation", "poor_satisfaction"])  # only 20 days ahead: not counted at 90
    r = admin.post("/admin/api/backtest", json=WINDOW).json()
    assert r["mode"] == "live" and r["live_available"] == 3 and r["tested"] == 3
    by_name = {c["name"]: c for c in r["cases"]}
    assert by_name["Acme Corp"]["predicted"] and not by_name["Wayne Retail"]["predicted"]
    assert sum(b["renewals"] for b in r["calibration"]) == 3
    assert admin.post("/admin/api/backtest", json={**WINDOW, "mode": "replay"}).json()["mode"] == "replay"
    assert admin.post("/admin/api/backtest", json={**WINDOW, "mode": "guess"}).status_code == 400


def test_closed_renewals_are_followed_up_once(ctx):
    from renewal_risk.service.scheduler import outcome_followup

    closed = {r["name"]: r for r in _closed(ctx)}
    umbrella = closed["Umbrella Health"]
    _predict(ctx, umbrella, 63, "high", ["support_escalation", "poor_satisfaction"])
    posted = []

    class Notifier:
        def post_channel(self, text):
            posted.append(text)
            return True

    day = date.fromisoformat(umbrella["close_date"]) + timedelta(days=5)
    result = outcome_followup(ctx, Notifier(), day)
    assert result["caught"] == 1 and result["posted"] and "Umbrella Health: churned, flagged 63 days ahead" in posted[0]
    assert outcome_followup(ctx, Notifier(), day) is None and len(posted) == 1  # each renewal is reported once


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
    assert admin.put("/admin/api/assign/ticket", json={"source": "legacy", "object": "tickets.csv", "link": "AccountId"}).json()["ok"]
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
