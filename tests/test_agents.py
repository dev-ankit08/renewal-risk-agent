"""The AI team: a coordinator, one specialist per data section with data, then synthesis — instructions editable, rules fixed."""
from tests.test_sections import SURVEYS


def _record_prompts(ctx):
    seen: list[tuple[str, str]] = []
    original = ctx.engine.llm.structured

    def structured(role, system, user, schema, usage=None, check=None):
        seen.append((role, system))
        return original(role, system, user, schema, usage, check)
    ctx.engine.llm.structured = structured
    return seen


def test_one_specialist_per_mapped_section(admin):
    from renewal_risk.service.context import get_context

    ctx = get_context()
    assert list(ctx.engine.specialists()) == ["accounts", "billing", "support", "usage"]  # one per section
    assert ctx.engine.specialists()["billing"]["entities"] == ["invoice", "payment"]  # contracts are read once, under accounts
    assert not any(r["entity"] == "contract" for c in admin.get("/admin/api/catalog").json()["categories"]
                   if c["key"] == "billing" for r in c["rows"])
    assert {s["name"]: s["specialist"] for s in ctx.settings.signals}["seat_utilisation"] == "usage"  # by its main data

    # Three sections with data -> three specialists, between one coordinator and one synthesis.
    assert admin.delete("/admin/api/sources/usage").json()["ok"]
    ctx = get_context()
    seen = _record_prompts(ctx)
    result = ctx.engine.assess("A001", account_name="Acme Corp")
    assert [role for role, _ in seen] == ["coordinator", "specialist", "specialist", "specialist", "synthesis"]
    assert {p["specialist"] for p in result.packets} == {"accounts", "billing", "support"}
    assert {p["label"] for p in result.packets} == {"Customers & renewals", "Billing & payments", "Support"}
    agents = admin.get("/admin/api/agents").json()
    usage = next(s for s in agents["specialists"] if s["key"] == "usage")
    assert not usage["has_data"] and usage["signals"] == ["seat_utilisation", "usage_decline"]


def test_instructions_reach_each_agent_and_rules_stay(admin):
    from renewal_risk.service.context import get_context

    for key, text in [("coordinator", "Always run the support specialist."), ("support", "Escalations from VPs matter most."),
                      ("synthesis", "Mention the renewal date in the rationale.")]:
        assert admin.put(f"/admin/api/agents/{key}", json={"instructions": text}).json()["ok"]
    assert admin.put("/admin/api/agents/nope", json={"instructions": "x"}).status_code == 404
    assert admin.put("/admin/api/agents/synthesis", json={"instructions": "x" * 2001}).status_code == 400

    ctx = get_context()
    seen = _record_prompts(ctx)
    ctx.engine.assess("A001", account_name="Acme Corp")
    prompt = {role if role != "specialist" else system.split(" specialist", 1)[0]: system for role, system in seen}
    assert "Always run the support specialist." in prompt["coordinator"]
    assert "Escalations from VPs matter most." in prompt["You are the Support"]
    assert "Commercial health of the renewal" in prompt["You are the Customers & renewals"]  # untouched default
    assert "Mention the renewal date" in prompt["synthesis"] and "copied exactly" in prompt["synthesis"]  # rules kept

    agents = admin.get("/admin/api/agents").json()
    support = next(s for s in agents["specialists"] if s["key"] == "support")
    assert support["instructions"] == "Escalations from VPs matter most." and "Every evidence_ref must be" in support["preview"]
    assert admin.put("/admin/api/agents/support", json={"instructions": ""}).json()["ok"]  # back to the default
    support = next(s for s in admin.get("/admin/api/agents").json()["specialists"] if s["key"] == "support")
    assert support["instructions"] == support["default"] and support["default"].startswith("Support experience")


def test_a_custom_section_is_one_more_specialist(admin):
    from renewal_risk.service.context import get_context

    assert admin.put("/admin/api/sections/customer_surveys", json=SURVEYS).json()["ok"]
    assert admin.put("/admin/api/sections/coordinator", json=SURVEYS).status_code == 400  # reserved for an agent
    specialists = {s["key"]: s for s in admin.get("/admin/api/agents").json()["specialists"]}
    assert specialists["customer_surveys"]["custom"] and specialists["customer_surveys"]["instructions"] == SURVEYS["focus"]
    assert admin.put("/admin/api/agents/customer_surveys", json={"instructions": "Low NPS from executives first."}).json()["ok"]
    assert get_context().settings.sections[0]["focus"] == "Low NPS from executives first."  # kept with the section
    assert admin.put("/admin/api/agents/customer_surveys", json={"instructions": ""}).status_code == 400
