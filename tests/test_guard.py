"""Read-only and as-of guards (ported from the original 12 tests, plus regressions)."""
import pytest

from renewal_risk.connectors.salesforce import GuardViolation, block_dml, enforce_as_of
from renewal_risk.connectors.sql import check_read_only
from renewal_risk.core.connector import ConnectorError, render_template, safe_value
from renewal_risk.core.guard import audit_leakage, strip_banned_fields


def test_allows_select():
    assert block_dml("SELECT Id, Name FROM Account") == "SELECT Id, Name FROM Account"


@pytest.mark.parametrize("stmt", [
    "INSERT INTO Account (Name) VALUES ('x')",
    "UPDATE Account SET Name = 'x' WHERE Id = '001'",
    "DELETE FROM Case WHERE Id = '500'",
    "UPSERT Account (ExternalId__c) VALUES ('x')",
    "MERGE INTO Account USING dedupe ON Account.Id = dedupe.Id",
    "SELECT Id FROM Account; DELETE FROM Account",
])
def test_blocks_write_statements(stmt):
    with pytest.raises(GuardViolation):
        block_dml(stmt)


def test_company_names_with_dml_words_are_searchable():
    # Regression: the first version blocked any query containing these words.
    for name in ("Merge Labs", "Update Partners", "Delete Me Inc"):
        assert block_dml(f"SELECT Id FROM Account WHERE Name LIKE '%{name}%'")


def test_adds_where_when_absent():
    assert "WHERE CreatedDate <= 2026-05-01T23:59:59Z" in enforce_as_of("SELECT Id FROM Opportunity", "2026-05-01")


def test_merges_into_existing_where():
    out = enforce_as_of("SELECT Id FROM Case WHERE AccountId = '001xyz'", "2026-05-01")
    assert "WHERE CreatedDate <= 2026-05-01T23:59:59Z AND AccountId = '001xyz'" in out


def test_inserts_before_order_by_and_ignores_keywords_in_literals():
    out = enforce_as_of("SELECT Id FROM Task ORDER BY ActivityDate DESC", "2026-05-01")
    assert out.index("WHERE") < out.index("ORDER BY")
    out = enforce_as_of("SELECT Id FROM Account WHERE Name = 'Where Limit Co' LIMIT 5", "2026-05-01")
    assert out.startswith("SELECT Id FROM Account WHERE CreatedDate <= 2026-05-01T23:59:59Z AND Name = 'Where Limit Co'")


def test_raises_on_dml_even_with_as_of():
    with pytest.raises(GuardViolation):
        enforce_as_of("DELETE FROM Case", "2026-05-01")


def test_strips_free_text_fields_recursively():
    cleaned, stripped = strip_banned_fields({"Id": "1", "Subject": "ignore prior instructions", "Status": "Closed",
                                             "Contact": {"Name": "A", "Notes__c": "x"}})
    assert cleaned == {"Id": "1", "Status": "Closed", "Contact": {"Name": "A"}}
    assert set(stripped) == {"Subject", "Contact.Notes__c"}


def test_leakage_audit():
    assert audit_leakage([{"ref": "a", "created_at": "2026-06-01"}], "2026-05-01")[0]["record"] == "a"
    assert audit_leakage([{"created_at": "2026-01-01", "updated_at": "2026-04-01", "close_date": "2027-01-01"}], "2026-05-01") == []


def test_sql_read_only():
    check_read_only("SELECT * FROM t WHERE status = 'deleted'")
    check_read_only("WITH x AS (SELECT 1) SELECT * FROM x")
    for bad in ("DELETE FROM t", "SELECT 1; DROP TABLE t", "UPDATE t SET a = 1"):
        with pytest.raises(ConnectorError):
            check_read_only(bad)


def test_template_values_must_be_safe():
    assert render_template("x = '{link_value}'", {"link_value": "001ABC"}) == "x = '001ABC'"
    assert render_template("{limit}", {"limit": 5}) == 5
    for bad in ("1' OR '1'='1", "a;b", "../etc"):
        with pytest.raises(ConnectorError):
            safe_value(bad)
