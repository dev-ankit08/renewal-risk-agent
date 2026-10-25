"""Custom data sections: a new data type + its own specialist, connected by CSV, SQL, REST, MCP or a Salesforce object."""
import sqlite3

import pytest
import yaml

from renewal_risk.core import model, sections
from tests.conftest import TODAY

SURVEYS = {
    "label": "Customer surveys",
    "description": "NPS responses from our survey tool.",
    "data_type": {"name": "survey", "description": "One survey response from a customer contact.", "date_field": "responded_at",
                  "fields": [{"name": "score", "type": "num", "description": "Likelihood to recommend, 0-10"},
                             {"name": "responded_at", "type": "date", "description": "When the customer answered"}]},
    "focus": "Judge whether the customer is unhappy from their survey scores and trend.",
}
LOW_SCORES = {"name": "low_survey_scores", "description": "Detractor survey answers.", "specialist": "customer_surveys",
              "rule": "count", "entity": "survey", "where": {"score": {"lte": 6}}, "thresholds": {"medium": 1, "high": 2}}


def test_sections_validate_with_reasons():
    reserved = {"accounts", "commercial"}
    good = sections.normalize({"sections": [{"key": "customer_surveys", **SURVEYS}]}, reserved)
    assert good[0]["data_type"]["fields"][0] == {"name": "score", "type": "num", "description": "Likelihood to recommend, 0-10"}
    for bad, reason in [
        ({"key": "commercial"}, "already used"),
        ({"data_type": {**SURVEYS["data_type"], "name": "ticket"}}, "standard data type"),
        ({"data_type": {**SURVEYS["data_type"], "fields": [{"name": "comment", "type": "str"}]}}, "free text"),
        ({"data_type": {**SURVEYS["data_type"], "fields": [{"name": "id", "type": "str"}]}}, "added automatically"),
        ({"data_type": {**SURVEYS["data_type"], "date_field": "score"}}, "date fields"),
        ({"focus": ""}, "what to look for"),
    ]:
        with pytest.raises(sections.SectionError, match=reason):
            sections.normalize({"sections": [{"key": "customer_surveys", **SURVEYS, **bad}]}, reserved)
    problems: list[str] = []
    assert sections.normalize({"sections": [{"key": "x"}]}, reserved, problems) == [] and problems  # lenient on load


def test_csv_section_end_to_end(admin, demo_config, tmp_path):
    from renewal_risk.service.context import get_context

    assert admin.put("/admin/api/sections/customer_surveys", json=SURVEYS).json()["ok"]
    assert admin.put("/admin/api/sections/commercial", json=SURVEYS).status_code == 400  # a built-in specialist's name
    ctx = get_context()
    assert "survey" in model.ENTITIES and model.field_type("survey", "score") == "num"
    assert ctx.settings.agent["specialists"]["customer_surveys"]["entities"] == ["survey"]
    card = next(c for c in admin.get("/admin/api/catalog").json()["categories"] if c["key"] == "customer_surveys")
    assert card["custom"] and card["section"]["focus"] and card["configured_sources"] == []

    # Connect with the existing CSV upload flow, then map the fields.
    rows = "id,account_id,score,responded_at\nS1,A001,3,%s\nS2,A001,5,%s\nS3,A002,9,%s\n" % ((TODAY.isoformat(),) * 3)
    assert admin.put("/admin/api/sources/surveys", json={"type": "csv", "test_data": True}).json()["ok"]
    up = admin.post("/admin/api/sources/surveys/upload", files={"file": ("surveys.csv", rows.encode(), "text/csv")}).json()
    assert up["columns"] == ["id", "account_id", "score", "responded_at"]
    assert admin.put("/admin/api/sources/surveys/csv-entity", json={"entity": "survey", "file": "surveys.csv", "match": "account_id"}).json()["ok"]
    fields = {"id": "id", "score": "score | num", "responded_at": "responded_at | date", "created_at": "responded_at | date"}
    assert admin.put("/admin/api/mapping/surveys/survey", json={"fields": fields}).json()["ok"]

    # A warning sign for the section, tested on real accounts.
    tested = {r["account_id"]: r for r in admin.post("/admin/api/signals/test", json={"signal": LOW_SCORES}).json()["results"]}
    assert tested["A001"]["severity"] == "high" and not tested["A002"]["fired"]
    assert admin.post("/admin/api/signals/custom", json={"signal": LOW_SCORES}).json()["ok"]

    # Its own specialist reads the data and reports, in AI and in rules-only mode.
    ctx = get_context()
    result = ctx.engine.assess("A001", account_name="Acme Corp")
    packet = next(p for p in result.packets if p["specialist"] == "customer_surveys")
    assert {s["name"] for s in packet["signals"]} == {"low_survey_scores"} and result.record_counts["survey"] == 2
    ctx.settings.agent["llm"]["mode"] = "rules"
    view = ctx.service.ask(ctx.policy.principal_for_email("admin@demo.example"), "Acme Corp")
    assert any(s["specialist"] == "customer_surveys" and s["how_calculated"] for s in view["signals"])

    # Deleting is refused while a signal uses it; then removes the data type and the source made for it.
    assert admin.delete("/admin/api/sections/customer_surveys").status_code == 409
    assert admin.delete("/admin/api/signals/custom/low_survey_scores").json()["ok"]
    assert admin.delete("/admin/api/sections/customer_surveys").json()["ok"]
    assert "survey" not in model.ENTITIES and "customer_surveys" not in get_context().settings.agent["specialists"]
    assert "survey" not in (yaml.safe_load((demo_config / "connectors.yaml").read_text(encoding="utf-8"))["sources"]["surveys"].get("entities") or {})


def test_sql_rest_mcp_and_salesforce_connections(admin, demo_config, tmp_path):
    from renewal_risk.service.context import get_context

    assert admin.put("/admin/api/sections/customer_surveys", json=SURVEYS).json()["ok"]
    db = tmp_path / "surveys.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE surveys (id TEXT, account_id TEXT, score REAL, responded_at TEXT)")
        conn.execute("INSERT INTO surveys VALUES ('S1', 'A001', 2, ?)", (TODAY.isoformat(),))
    sql = {"kind": "sql", "source": "survey_db", "dsn": f"sqlite:///{db.as_posix()}",
           "query": "SELECT id, account_id, score, responded_at FROM surveys WHERE account_id = :link_value"}
    assert admin.post("/admin/api/sections/customer_surveys/connect", json={**sql, "query": "DELETE FROM surveys"}).status_code == 400
    assert admin.post("/admin/api/sections/customer_surveys/connect", json={**sql, "query": "SELECT * FROM surveys"}).status_code == 400
    assert admin.post("/admin/api/sections/customer_surveys/connect", json=sql).json()["ok"]
    saved = yaml.safe_load((demo_config / "connectors.yaml").read_text(encoding="utf-8"))["sources"]["survey_db"]
    assert saved["dsn"] == "${SECRET:sources/survey_db/dsn}" and saved["section"] == "customer_surveys"  # never the DSN itself
    records = get_context().engine.pool.get("survey_db").fetch("survey", "A001", TODAY.isoformat())
    assert [r["id"] for r in records] == ["S1"]

    rest = {"kind": "rest", "source": "survey_api", "base_url": "https://surveys.example.com", "auth_type": "bearer", "secret": "tok-123",
            "path": "/v1/responses", "params": {"account": "{link_value}"}, "records_path": "data"}
    assert admin.post("/admin/api/sections/customer_surveys/connect", json={**rest, "path": "v1"}).status_code == 400
    assert admin.post("/admin/api/sections/customer_surveys/connect", json=rest).json()["ok"]
    mcp = {"kind": "mcp", "source": "survey_mcp", "command": "npx", "args": ["-y", "survey-mcp"], "env": {"SURVEY_TOKEN": "s3cret"},
           "tool": "list_responses", "tool_args": {"account": "{link_value}"}, "records_path": "items"}
    assert admin.post("/admin/api/sections/customer_surveys/connect", json=mcp).json()["ok"]
    conf = yaml.safe_load((demo_config / "connectors.yaml").read_text(encoding="utf-8"))["sources"]
    assert conf["survey_api"]["connection"]["auth"] == {"type": "bearer", "token": "${SECRET:sources/survey_api/token}"}
    assert conf["survey_api"]["entities"]["survey"]["fetch"]["request"]["path"] == "/v1/responses"
    assert conf["survey_mcp"]["server"]["env"] == {"SURVEY_TOKEN": "${SECRET:sources/survey_mcp/env_SURVEY_TOKEN}"}
    assert "tok-123" not in str(conf) and "s3cret" not in str(conf)
    assert admin.post("/admin/api/sections/customer_surveys/connect", json={**rest, "source": "survey_db"}).status_code == 400  # wrong type

    # Salesforce objects need a Salesforce connection; the demo has none.
    sf = {"kind": "salesforce", "source": "crm", "object": "Survey__c", "account_field": "Account__c"}
    assert admin.post("/admin/api/sections/customer_surveys/connect", json=sf).status_code == 400
    assert admin.get("/admin/api/sources/crm/objects/Survey__c/fields").status_code == 400


def test_salesforce_object_goes_to_the_overrides_file(admin, demo_config, monkeypatch):
    from renewal_risk.service.context import get_context

    assert admin.put("/admin/api/sections/customer_surveys", json=SURVEYS).json()["ok"]
    monkeypatch.setitem(get_context().settings.sources["crm"], "type", "salesforce")  # pretend the CRM is a Salesforce org
    sf = {"kind": "salesforce", "source": "crm", "object": "Survey__c", "account_field": "Account__c"}
    assert admin.post("/admin/api/sections/customer_surveys/connect", json={**sf, "object": "Survey__c; DROP"}).status_code == 400
    assert admin.post("/admin/api/sections/customer_surveys/connect", json=sf).json()["ok"]
    overrides = yaml.safe_load((demo_config / "mappings" / "crm.overrides.yaml").read_text(encoding="utf-8"))
    assert overrides["survey"]["fetch"] == {"object": "Survey__c", "account_field": "Account__c"}
    assert overrides["survey"]["fields"]["id"] == "Id"
