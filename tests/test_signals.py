from renewal_risk.core import signals
from renewal_risk.core.settings import DEFAULTS_DIR, read_yaml

AS_OF = "2026-10-10"
DEFAULTS = read_yaml(DEFAULTS_DIR / "signals.yaml")["signals"]


def rec(entity, rid, **fields):
    return {"_entity": entity, "id": rid, "ref": f"src:{entity}/{rid}", **fields}


def run(records, available=None):
    available = available or set(records)
    return signals.evaluate(DEFAULTS, records, AS_OF, available)


def names(result, specialist):
    return {s["name"]: s["severity"] for s in result.get(specialist, {}).get("signals", [])}


def test_defaults_are_valid():
    assert signals.validate(DEFAULTS) == []


def test_amount_decrease_uses_cross_entity_baseline():
    out = run({
        "renewal": [rec("renewal", "r1", amount=80, is_open=True, close_date="2026-12-01")],
        "contract": [rec("contract", "c1", arr=100, start_date="2025-12-01")],
    })
    assert names(out, "commercial")["amount_decrease"] == "high"
    sig = next(s for s in out["commercial"]["signals"] if s["name"] == "amount_decrease")
    assert sig["evidence_ref"] == "src:renewal/r1" and "20%" in sig["value"]


def test_thresholds_inclusive_and_below_direction():
    assert signals.severity_for(15, {"medium": 5, "high": 15}) == "high"
    assert signals.severity_for(4, {"medium": 5, "high": 15}) is None
    assert signals.severity_for(0.3, {"medium": 0.6, "high": 0.4}, "below") == "high"


def test_counts_windows_and_where_ops():
    out = run({"invoice": [
        rec("invoice", "i1", is_paid=False, due_date="2026-09-01"),
        rec("invoice", "i2", is_paid=False, due_date="2026-12-01"),  # not yet due
        rec("invoice", "i3", is_paid=True, due_date="2026-08-01"),
    ]})
    assert names(out, "financial")["invoice_overdue"] == "medium"


def test_days_since_gap_when_no_dates():
    out = run({"activity": []})
    assert any("no_recent_activity not evaluated" in g for g in out["relationship"]["gaps"])


def test_missing_entities_become_gaps_not_signals():
    out = run({}, available=set())
    assert "payment_disputes not evaluated — no source provides payment" in out["financial"]["gaps"]


def test_champion_departed_cases():
    out = run({"contact": [rec("contact", "p1", name="Dana", title="VP", is_primary=True, is_active=False),
                           rec("contact", "p2", name="Lee", is_active=True)]})
    assert names(out, "relationship")["champion_departed"] == "high"


def test_usage_window_change_and_ratio():
    usage = [rec("usage_metric", f"u{i}", metric="active_users", period_start=d, value=v)
             for i, (d, v) in enumerate([("2026-10-05", 50), ("2026-09-25", 50), ("2026-09-01", 100), ("2026-08-20", 100)])]
    out = run({"usage_metric": usage, "line_item": [rec("line_item", "l1", quantity=200, is_active=True)]})
    assert names(out, "usage") == {"usage_decline": "high", "seat_utilisation": "high"}


def test_bad_signal_config_does_not_crash():
    out = signals.evaluate([{"name": "x", "rule": "count", "entity": "ticket", "where": {"created_at": {"lt": None}}}],
                           {"ticket": [rec("ticket", "t", created_at="2026-01-01")]}, AS_OF, {"ticket"})
    assert out  # evaluated without raising
