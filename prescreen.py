"""
prescreen.py — free, instant heuristic risk screen. No Anthropic call, no
sqlite ledger write. Pulls real Salesforce records via salesforce_client and
scores them with heuristics.py only.

Use this to answer "which accounts renewing soon look risky" before spending
real model calls (agent.run_account, via run.py --account/--golden) on the
few that warrant a deep, explained pass.
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import agent
import config
import heuristics
import salesforce_client


class AccountLookupError(Exception):
    """Zero or ambiguous (>1) name matches. .candidates holds
    [{"account_id", "name"}, ...] (empty for a zero-match lookup)."""

    def __init__(self, message: str, candidates: list[dict] | None = None):
        super().__init__(message)
        self.candidates = candidates or []


def _escape_soql_string(value: str) -> str:
    """Company names typed by a user are the first place in this codebase
    that free text reaches a SOQL string literal — everywhere else only
    interpolates Salesforce Ids. Escape backslash first, then the quote."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def resolve_account_by_name(name: str, client: salesforce_client.SalesforceClient | None = None) -> dict:
    """Looks up an Account Id by company name. Exact match first, falling
    back to a LIKE search. Never selects Renewal_Risk_Profile__c/
    Expected_Outcome__c — those are ground-truth test labels, not something
    account resolution (or anything upstream of it) should ever read."""
    client = client or salesforce_client.get_client(config.SALESFORCE_ORG_ALIAS)
    today = date.today().isoformat()
    escaped = _escape_soql_string(name)

    exact = client.soql_query(f"SELECT Id, Name FROM Account WHERE Name = '{escaped}' LIMIT 5", today)
    if len(exact) == 1:
        return {"account_id": exact[0]["Id"], "name": exact[0]["Name"]}
    if len(exact) > 1:
        raise AccountLookupError(
            f"Multiple accounts are named exactly '{name}' — did you mean one of these?",
            candidates=[{"account_id": r["Id"], "name": r["Name"]} for r in exact],
        )

    fuzzy = client.soql_query(f"SELECT Id, Name FROM Account WHERE Name LIKE '%{escaped}%' LIMIT 10", today)
    if len(fuzzy) == 1:
        return {"account_id": fuzzy[0]["Id"], "name": fuzzy[0]["Name"]}
    if not fuzzy:
        raise AccountLookupError(f"No account found matching '{name}'.")
    raise AccountLookupError(
        f"Multiple accounts match '{name}' — did you mean one of these?",
        candidates=[{"account_id": r["Id"], "name": r["Name"]} for r in fuzzy],
    )


def list_renewing_accounts(
    before_date: str,
    after_date: str | None = None,
    client: salesforce_client.SalesforceClient | None = None,
) -> list[dict]:
    """Accounts with an open Opportunity renewing on or before `before_date`
    (and, if given, on or after `after_date` — for a calendar-window screen)."""
    client = client or salesforce_client.get_client(config.SALESFORCE_ORG_ALIAS)
    today = date.today().isoformat()
    soql = (
        "SELECT AccountId, Account.Name, Id, CloseDate, Amount FROM Opportunity "
        f"WHERE CloseDate <= {before_date} AND IsClosed = false "
        + (f"AND CloseDate >= {after_date} " if after_date else "")
        + "ORDER BY CloseDate ASC LIMIT 200"
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


def prescreen_bulk(
    before_date: str,
    after_date: str | None = None,
    max_accounts: int | None = None,
    max_workers: int = 1,
) -> list[dict]:
    """Screens every renewing account. A single account's `sf` CLI call
    occasionally hangs/times out under real-world load (observed: one query
    stalling past config.SF_CLI_TIMEOUT_SECONDS after ~25+ prior calls in the
    same run) — that failure is surfaced and skipped rather than aborting the
    whole screen, matching run.py's run_golden per-account failure handling.

    max_accounts caps how many (soonest-renewing first, list is already
    sorted) get screened — useful for a web request that shouldn't hang for
    minutes. max_workers > 1 screens accounts concurrently via a thread pool:
    each account's screen is dominated by `sf` CLI subprocess wait, not CPU,
    and each call spawns its own separate subprocess, so this is safe on one
    shared client instance (mirrors run.py --golden's existing pattern)."""
    client = salesforce_client.get_client(config.SALESFORCE_ORG_ALIAS)
    today = date.today().isoformat()
    accounts = list_renewing_accounts(before_date, after_date, client)
    if max_accounts is not None:
        accounts = accounts[:max_accounts]

    results = []

    def _screen(acct: dict) -> dict | None:
        try:
            result = prescreen_account(acct["account_id"], today, client)
        except salesforce_client.SalesforceQueryError as exc:
            print(f"SKIPPED {acct['account_id']} ({acct['name']}): {exc}", file=sys.stderr)
            return None
        result["name"] = acct["name"]
        result["close_date"] = acct["close_date"]
        result["amount"] = acct["amount"]
        return result

    if max_workers <= 1:
        for acct in accounts:
            result = _screen(acct)
            if result is not None:
                results.append(result)
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_screen, acct): acct for acct in accounts}
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    results.append(result)
    return sorted(results, key=lambda r: (-r["high_severity_count"], -r["signal_count"]))
