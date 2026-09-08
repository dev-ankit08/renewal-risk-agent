"""
prescreen.py — free, instant heuristic risk screen. No Anthropic call, no
sqlite ledger write. Pulls real Salesforce records via salesforce_client and
scores them with heuristics.py only.

Use this to answer "which accounts renewing soon look risky" before spending
real model calls (agent.run_account, via run.py --account/--golden) on the
few that warrant a deep, explained pass.
"""
from __future__ import annotations

from datetime import date

import agent
import config
import heuristics
import salesforce_client


def list_renewing_accounts(before_date: str, client: salesforce_client.SalesforceClient | None = None) -> list[dict]:
    """Accounts with an open Opportunity renewing on or before `before_date`."""
    client = client or salesforce_client.get_client(config.SALESFORCE_ORG_ALIAS)
    today = date.today().isoformat()
    soql = (
        "SELECT AccountId, Account.Name, Id, CloseDate, Amount FROM Opportunity "
        f"WHERE CloseDate <= {before_date} AND IsClosed = false "
        "ORDER BY CloseDate ASC LIMIT 200"
    )
    rows = client.soql_query(soql, today)

    accounts: dict[str, dict] = {}
    skipped = 0
    for row in rows:
        account_id = row.get("AccountId")
        account = row.get("Account") or {}
        name = account.get("Name")
        if not account_id or not name:
            skipped += 1
            continue
        # Keep the earliest renewal per account if there are multiple open opps.
        if account_id not in accounts or row.get("CloseDate", "") < accounts[account_id]["close_date"]:
            accounts[account_id] = {"account_id": account_id, "name": name, "close_date": row.get("CloseDate"), "amount": row.get("Amount")}
    if skipped:
        print(f"note: skipped {skipped} Opportunity row(s) with no linked Account")
    return sorted(accounts.values(), key=lambda a: a["close_date"] or "")


def prescreen_account(account_id: str, as_of: str, client: salesforce_client.SalesforceClient | None = None) -> dict:
    """Runs both heuristic packets (no Anthropic call). Returns a lightweight
    combined summary, not a full RiskAssessment."""
    client = client or salesforce_client.get_client(config.SALESFORCE_ORG_ALIAS)
    commercial_records = agent.gather_records(client, "commercial", account_id, as_of)
    relationship_records = agent.gather_records(client, "relationship", account_id, as_of)
    commercial_packet = heuristics.commercial_signals(account_id, as_of, commercial_records)
    relationship_packet = heuristics.relationship_signals(account_id, as_of, relationship_records)

    all_signals = commercial_packet["signals"] + relationship_packet["signals"]
    high_count = sum(1 for s in all_signals if s["severity"] == "high")
    medium_count = sum(1 for s in all_signals if s["severity"] == "medium")

    return {
        "account_id": account_id,
        "signal_count": len(all_signals),
        "high_severity_count": high_count,
        "medium_severity_count": medium_count,
        "flag": high_count > 0 or medium_count >= 2,
        "signals": all_signals,
        "gaps": commercial_packet["gaps"] + relationship_packet["gaps"],
    }


def prescreen_bulk(before_date: str) -> list[dict]:
    client = salesforce_client.get_client(config.SALESFORCE_ORG_ALIAS)
    today = date.today().isoformat()
    accounts = list_renewing_accounts(before_date, client)
    results = []
    for acct in accounts:
        result = prescreen_account(acct["account_id"], today, client)
        result["name"] = acct["name"]
        result["close_date"] = acct["close_date"]
        result["amount"] = acct["amount"]
        results.append(result)
    return sorted(results, key=lambda r: (-r["high_severity_count"], -r["signal_count"]))
