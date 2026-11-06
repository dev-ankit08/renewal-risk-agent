"""Connect once, pick an object per data type, lock it, then map: object adapters, assignments, API."""
import time

import pytest
import yaml

from renewal_risk.core import objects

TICKETS = "TicketNo,OrgId,State,Severity,OpenedOn\nT1,A001,Open,P1,2026-01-02\nT2,A002,Closed,P3,2026-01-05\n"


class FakeSalesforce:
    def run_soql(self, soql):
        assert "EntityDefinition" in soql and "LIMIT" in soql  # Salesforce can't page EntityDefinition
        return [{"QualifiedApiName": "Survey__c", "Label": "Surveys"}, {"QualifiedApiName": "Account", "Label": "Account"},
                {"QualifiedApiName": "AccountHistory", "Label": "Account History"}]

    def object_fields(self, obj):
        return {"Id": {"type": "Id"}, "Account__c": {"type": "Lookup(Account)", "label": "Account"}, "Score__c": {"type": "Number"}}


def test_salesforce_and_hubspot_adapters():
    sf = objects.ADAPTERS["salesforce"]
    assert [o["name"] for o in sf.list(FakeSalesforce())] == ["Account", "Survey__c"]  # history objects hidden
    assert [f["name"] for f in sf.fields(FakeSalesforce(), "Survey__c") if f["link"]] == ["Account__c", "Id"]
    block = sf.block("ticket", "Survey__c", "Account__c", {})
    assert block == {"fetch": {"object": "Survey__c", "account_field": "Account__c"}} and sf.current(block) == "Survey__c"
    assert sf.block("account", "Account", None, {})["list"] == {"object": "Account"}
    assert sf.current({"list": {"soql": "SELECT Id FROM Opportunity WHERE IsClosed = false"}}) == "Opportunity"

    hs = objects.ADAPTERS["hubspot"]
    block = hs.block("ticket", "tickets", None, {"id": "id", "priority": "properties.hs_ticket_priority | lower"})
    assert block["fetch"]["request"]["json"]["properties"] == ["hs_ticket_priority"]
    assert block["fetch"]["request"]["json"]["filterGroups"][0]["filters"][0]["propertyName"] == "associations.company"
    assert hs.current(block) == "tickets" and not hs.links

    with pytest.raises(objects.ObjectError):
        objects.ADAPTERS["sql"].block("ticket", "tickets; DROP TABLE x", "org_id", {})
    assert objects.ADAPTERS["sql"].block("ticket", "support.tickets", "org_id", {})["fetch"]["query"] == \
        "SELECT * FROM support.tickets WHERE org_id = :link_value"


def test_lock_an_object_then_map(admin, demo_config):
    from renewal_risk.service.context import get_context

    # Connect a CSV system once, upload a file: its files are its objects.
    assert admin.put("/admin/api/sources/helpdesk", json={"type": "csv", "test_data": True}).json()["ok"]
    admin.post("/admin/api/sources/helpdesk/upload", files={"file": ("tickets.csv", TICKETS.encode(), "text/csv")})
    listed = admin.get("/admin/api/sources/helpdesk/objects").json()
    assert [o["name"] for o in listed["objects"]] == ["tickets.csv"] and listed["links"]
    fields = admin.get("/admin/api/sources/helpdesk/objects/tickets.csv/fields").json()["fields"]
    assert [f["name"] for f in fields] == ["TicketNo", "OrgId", "State", "Severity", "OpenedOn"]

    # Lock: tickets now come only from that file; the other support system no longer supplies them.
    assert admin.put("/admin/api/assign/ticket", json={"source": "helpdesk", "object": "tickets.csv"}).status_code == 400  # link needed
    assert admin.put("/admin/api/assign/nope", json={"source": "helpdesk", "object": "tickets.csv", "link": "OrgId"}).status_code == 400
    assert admin.put("/admin/api/assign/ticket", json={"source": "helpdesk", "object": "tickets.csv", "link": "OrgId"}).json()["ok"]
    ctx = get_context()
    assert ctx.engine.pool.providers("ticket") == ["helpdesk"]
    assert ctx.settings.sources["helpdesk"]["entities"]["ticket"]["fetch"] == {"file": "tickets.csv", "match": "OrgId"}
    support = next(c for c in admin.get("/admin/api/catalog").json()["categories"] if c["key"] == "support")
    assert support["rows"] == [{"entity": "ticket", "locked": True, "source": "helpdesk", "object": "tickets.csv", "link": "OrgId",
                                "missing": ["is_open"]}]  # locked, but its required fields are not all mapped yet: "not mapped"

    # Then map its fields, and read real records for an account.
    mapping = admin.get("/admin/api/mapping/helpdesk/ticket").json()
    assert mapping["current"]["account_id"] == "OrgId" and mapping["describe"]["sample_count"] >= 1
    assert admin.put("/admin/api/mapping/helpdesk/ticket", json={"fields": {"id": "TicketNo", "priority": "Severity",
                                                                             "created_at": "OpenedOn | date"}}).json()["ok"]
    records = get_context().engine.pool.get("helpdesk").fetch("ticket", "A001", "2026-12-31")
    assert [(r["id"], r["priority"]) for r in records] == [("T1", "P1")]

    # Locking the preset's own object keeps the preset's reading and fields; unlocking restores defaults.
    renewal = next(r for c in admin.get("/admin/api/catalog").json()["categories"] for r in c["rows"] if r["entity"] == "renewal")
    assert renewal == {"entity": "renewal", "locked": False, "source": "crm", "object": "renewals.csv"}
    before = get_context().settings.sources["crm"]["entities"]["renewal"]
    assert admin.put("/admin/api/assign/renewal", json={"source": "crm", "object": "renewals.csv", "link": "AccountId"}).json()["ok"]
    assert get_context().settings.sources["crm"]["entities"]["renewal"] == before
    assert admin.delete("/admin/api/assign/ticket").json()["ok"]
    assert "support" in get_context().engine.pool.providers("ticket")
    assert "ticket" not in (yaml.safe_load((demo_config / "connectors.yaml").read_text(encoding="utf-8"))["assign"])


def test_sql_tables_and_assigning_the_customer_list(admin, demo_config):
    from renewal_risk.service.context import get_context

    tables = [o["name"] for o in admin.get("/admin/api/sources/usage/objects").json()["objects"]]
    assert tables == ["usage"]
    assert "tenant_id" in [f["name"] for f in admin.get("/admin/api/sources/usage/objects/usage/fields").json()["fields"]]
    assert admin.put("/admin/api/assign/account", json={"source": "crm", "object": "accounts.csv"}).json()["ok"]
    assert yaml.safe_load((demo_config / "connectors.yaml").read_text(encoding="utf-8"))["primary"] == "crm"
    assert get_context().settings.primary == "crm"


def test_a_slow_system_never_hangs_the_mapping_screen(admin, monkeypatch):
    from renewal_risk.admin import api
    from renewal_risk.service.context import get_context

    monkeypatch.setattr(api, "SYSTEM_TIMEOUT", 0.3)
    monkeypatch.setattr(type(get_context().engine.pool.get("support")), "describe", lambda self, *a, **k: time.sleep(2))
    r = admin.get("/admin/api/mapping/support/ticket")
    assert r.status_code == 504 and "did not answer" in r.json()["detail"]
