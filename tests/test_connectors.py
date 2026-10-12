import json
import sqlite3
import sys
from pathlib import Path

import pytest

from renewal_risk.core.connector import ConnectorError, create


def test_csv_fetch_maps_and_as_of_filters(tmp_path):
    (tmp_path / "t.csv").write_text("id,org,status,created\n1,A,open,2026-01-01\n2,A,open,2026-12-01\n3,B,open,2026-01-01\n", encoding="utf-8")
    conn = create("support", {"type": "csv", "path": str(tmp_path), "entities": {"ticket": {
        "fetch": {"file": "t.csv", "match": "org"}, "fields": {"id": "id", "status": "status", "created_at": "created | date"}}}})
    rows = conn.fetch("ticket", "A", "2026-06-30")
    assert [r["id"] for r in rows] == ["1"]  # record 2 is after as_of, record 3 another org
    assert rows[0]["ref"] == "support:ticket/1"


def test_csv_rejects_path_escape(tmp_path):
    conn = create("x", {"type": "csv", "path": str(tmp_path), "entities": {"ticket": {"list": {"file": "../secret.csv"}, "fields": {"id": "id"}}}})
    with pytest.raises(ConnectorError):
        conn.list("ticket")


def test_sql_binds_parameters(tmp_path):
    db = tmp_path / "u.sqlite"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE u (id TEXT, tenant TEXT, day TEXT, value REAL)")
    c.executemany("INSERT INTO u VALUES (?,?,?,?)", [("1", "A", "2026-01-01", 5), ("2", "A'; DROP TABLE u;--", "2026-01-01", 9)])
    c.commit()
    c.close()
    conn = create("usage", {"type": "sql", "dsn": f"sqlite:///{db.as_posix()}", "entities": {"usage_metric": {
        "fetch": {"query": "SELECT * FROM u WHERE tenant = :link_value AND day <= :as_of"},
        "fields": {"id": "id", "value": "value", "period_start": "day | date"}}}})
    assert [r["value"] for r in conn.fetch("usage_metric", "A", "2026-06-30")] == [5]
    assert conn.health().ok
    conn.close()


def test_rest_pagination_auth_and_read_only():
    import httpx2

    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/v1/customers":
            if "starting_after" not in request.url.params:
                return httpx2.Response(200, json={"data": [{"id": "c1", "email": "a@acme.com"}], "has_more": True})
            return httpx2.Response(200, json={"data": [{"id": "c2", "email": "b@globex.com"}], "has_more": False})
        return httpx2.Response(404, json={})

    from renewal_risk.connectors.rest import RestConnector

    conn = RestConnector("billing", {
        "type": "rest",
        "connection": {"base_url": "https://api.example.com", "auth": {"type": "bearer", "token": "sk_test"}, "pagination": {"type": "stripe"}},
        "entities": {"customer": {"list": {"request": {"path": "/v1/customers", "params": {"limit": 1}, "records_path": "data"}},
                                  "fields": {"id": "id", "domain": "email | domain"}},
                     "invoice": {"fetch": {"request": {"method": "DELETE", "path": "/v1/x"}}, "fields": {"id": "id"}}},
    }, transport=httpx2.MockTransport(handler))
    customers = conn.list("customer")
    assert [(c["id"], c["domain"]) for c in customers] == [("c1", ["acme.com"]), ("c2", ["globex.com"])]
    assert seen[0].headers["authorization"] == "Bearer sk_test"
    with pytest.raises(ConnectorError, match="not allowed"):
        conn.fetch("invoice", "c1", "2026-01-01")


def test_rest_path_values_are_escaped():
    import httpx2

    from renewal_risk.connectors.rest import RestConnector

    urls = []
    conn = RestConnector("s", {"type": "rest", "connection": {"base_url": "https://h"}, "entities": {"ticket": {
        "fetch": {"request": {"path": "/orgs/{link_value}/tickets", "records_path": "tickets"}}, "fields": {"id": "id"}}}},
        transport=httpx2.MockTransport(lambda r: urls.append(str(r.url)) or httpx2.Response(200, json={"tickets": []})))
    conn.fetch("ticket", "9001", "2026-01-01")
    assert urls == ["https://h/orgs/9001/tickets"]
    with pytest.raises(ConnectorError):
        conn.fetch("ticket", "../admin", "2026-01-01")


def test_mcp_generic_connector_against_stub_server():
    pytest.importorskip("mcp")
    server = Path(__file__).parent / "mcp_stub_server.py"
    conn = create("erp", {"type": "mcp", "server": {"command": sys.executable, "args": [str(server)]}, "startup_timeout": 60,
                          "entities": {"invoice": {"fetch": {"tool": "query_invoices", "args": {"customer": "{link_value}"}, "records_path": "items"},
                                                   "fields": {"id": "InvoiceNo", "amount": "Total", "is_paid": "Status | eq('PAID')",
                                                              "due_date": "DueDate | date", "created_at": "Created | date"}}}})
    try:
        rows = conn.fetch("invoice", "C-1", "2026-06-30")
        assert {r["id"]: r["is_paid"] for r in rows} == {"INV-1": False, "INV-2": True}
        assert conn.fetch("invoice", "C-1", "2025-10-01") == []  # both created after this as_of
    finally:
        conn.close()


def test_unknown_connector_type():
    with pytest.raises(ConnectorError, match="unknown connector type"):
        create("x", {"type": "nope"})


def test_salesforce_soql_building_without_a_server():
    from renewal_risk.connectors.salesforce import SalesforceConnector

    class FakeSession:
        def __init__(self):
            self.queries = []

        def call(self, tool, args):
            self.queries.append(args["query"])
            return {"records": [{"attributes": {"type": "Case"}, "Id": "500A", "IsClosed": False, "Owner": {"attributes": {}, "Email": "X@Y.com"}}]}

    session = FakeSession()
    conn = SalesforceConnector("crm", {"type": "salesforce", "org_alias": "sandbox", "entities": {
        "ticket": {"fetch": {"object": "Case", "account_field": "AccountId"}, "fields": {"id": "Id", "is_open": "IsClosed | not"}},
        "account": {"search": {"soql": "SELECT Id, Name FROM Account WHERE Name LIKE '%{text}%' LIMIT 20"}, "fields": {"id": "Id"}}}}, session=session)
    rows = conn.fetch("ticket", "001ABC", "2026-05-01")
    assert rows[0]["is_open"] is True and rows[0]["ref"] == "crm:ticket/500A"
    assert session.queries[0] == ("SELECT FIELDS(ALL) FROM Case WHERE CreatedDate <= 2026-05-01T23:59:59Z AND AccountId = '001ABC' LIMIT 200")
    conn.search("account", "O'Neil & Merge_Labs 100%")
    assert "LIKE '%O\\'Neil & Merge\\_Labs 100\\%%'" in session.queries[1]
    assert json.dumps(rows)  # attributes stripped, serialisable
