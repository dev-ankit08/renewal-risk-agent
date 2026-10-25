"""End to end on the four-system CSV demo with a fake model client."""
import pytest

from renewal_risk.core.linking import read_manual_links, write_manual_links
from renewal_risk.core.pipeline import PolicyError
from renewal_risk.core.settings import load_settings
from renewal_risk.core.validate import validate_settings


def test_demo_config_is_valid(demo_config):
    errors, _warnings = validate_settings(load_settings(demo_config))
    assert errors == []


def test_linking_strategies(ctx):
    report = ctx.engine.linker.build()
    billing = report.sources["billing"]
    assert billing.links["A001"] == [{"value": "cus_acme", "method": "key:key_account_id"}]
    assert billing.links["A002"][0]["method"] == "domain"           # Globex: no CRM id, matched by domain
    assert "A006" not in billing.links                              # Wayne: no billing customer
    assert [r["customer_id"] for r in billing.review] == ["cus_stark"]  # gmail + similar name -> review only
    assert "A005" not in billing.links
    assert report.sources["usage"].method == "same_id"


def test_review_approval_links_account(ctx):
    linker = ctx.engine.linker
    write_manual_links(linker.manual_path, [{"source": "billing", "account_id": "A005", "link_value": "cus_stark", "status": "approved"}])
    assert linker.link_values("billing", "A005") == ["cus_stark"]
    assert read_manual_links(linker.manual_path)[0]["status"] == "approved"


def test_full_assessment_across_sources(ctx):
    result = ctx.engine.assess("A001", account_name="Acme Corp")
    a = result.assessment
    assert a["band"] == "high" and a["score"] == 100
    sources = {s["evidence_ref"].split(":")[0] for p in result.packets for s in p["signals"]}
    assert sources == {"crm", "support", "billing", "usage"}
    # Invented refs are dropped at both stages and recorded.
    assert "crm:renewal/NOPE" in result.fabricated_refs and "billing:invoice/FAKE" in result.fabricated_refs
    assert all(s["evidence_ref"] != "crm:renewal/NOPE" for p in result.packets for s in p["signals"])
    assert "billing:invoice/FAKE" not in a["citations"]
    assert a["recommended_actions"] and all(act["evidence_refs"] for act in a["recommended_actions"])
    assert result.leakage_violations == []
    assert {p["specialist"] for p in result.packets} == {"commercial", "relationship", "financial", "usage"}
    stored = ctx.store.latest_assessment("A001")
    assert stored["assessment"]["score"] == 100


def test_unlinked_source_is_a_gap_not_a_failure(ctx):
    result = ctx.engine.assess("A006")
    assert any("not linked to any billing customer" in g for g in result.source_gaps)
    assert result.assessment["band"] == "low"


def test_backtest_as_of_excludes_future_records(ctx):
    screen = ctx.engine.prescreen("A001", as_of="2026-01-01")
    assert screen["signals"] == [] or all(s["name"] != "invoice_overdue" for s in screen["signals"])


def test_policy_blocks_real_data_without_zdr(demo_config, monkeypatch):
    from renewal_risk.service.context import build_context
    from tests.conftest import FakeLLM

    settings = load_settings(demo_config)
    settings.sources["billing"]["test_data"] = False
    context = build_context(settings, llm=FakeLLM())
    with pytest.raises(PolicyError, match="billing"):
        context.engine.assess("A001")
    monkeypatch.setenv("RENEWAL_AGENT_ZDR_CONFIRMED", "true")
    assert context.engine.assess("A001").assessment["band"] == "high"
    context.engine.close()


def test_renewal_window_listing(ctx):
    from datetime import date, timedelta

    today = date.today()
    rows = ctx.engine.list_renewals((today + timedelta(days=120)).isoformat(), today.isoformat())
    assert [r["account_id"] for r in rows] == ["A006", "A003", "A001", "A002", "A004", "A005"]
    assert rows[0]["name"] == "Wayne Retail"


def test_rules_mode_answers_without_any_model_call(ctx, monkeypatch):
    from renewal_risk.core.llm import AnthropicLLM

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-...")  # the .env.example placeholder
    assert AnthropicLLM({}).configured is False
    ctx.settings.agent["llm"]["mode"] = "rules"
    ctx.settings.sources["crm"]["test_data"] = False  # nothing leaves the service, so no data-retention gate
    calls = len(ctx.engine.llm.calls)
    view = ctx.service.ask(ctx.policy.principal_for_email("admin@demo.example"), "Acme Corp")
    assert len(ctx.engine.llm.calls) == calls  # no model call at all, not even to parse the question
    assert view["type"] == "single_account" and view["mode"] == "rules" and view["band"] == "high"
    assert "warning signs found" in view["rationale"] and "_" not in view["rationale"] and view["drivers"]
    plays = {p["id"]: p for p in ctx.settings.playbooks["plays"]}
    assert view["recommended_actions"] and all(a["playbook_id"] in plays for a in view["recommended_actions"])
    assert all(s["how_calculated"] for s in view["signals"])
    assert set(view["citations"]) == {s["evidence_ref"] for s in view["signals"]}
