#!/usr/bin/env python3
"""
build_golden_set.py — one-off script, not part of the regular CLI.

Queries the real org for the tagged test accounts (Account.Renewal_Risk_Profile__c
!= null) plus each one's Contract.ARR__c and latest Opportunity.CloseDate, and
writes golden_set.csv using Account.Expected_Outcome__c as the `outcome`
column.

This is the ONE sanctioned place in this codebase that reads
Renewal_Risk_Profile__c / Expected_Outcome__c — they are ground-truth test
labels for building/checking this golden set, never an input to the agent's
own risk detection (agent.py's gather_records must never query Account fields
beyond Id/AccountId joins).

as_of is set to today for every row: this is bulk-loaded synthetic test data
with no genuine historical staging, and every record's own CreatedDate is
today (an audit field that can't be backdated via the API) — a computed
"120 days before renewal" as_of would exclude almost every record from
guard.enforce_as_of's CreatedDate <= as_of filter.

Usage: python3 build_golden_set.py
"""
from __future__ import annotations

import csv
import sys

import config
import salesforce_client
from datetime import date

# See run.py for why this is necessary on Windows.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

OUTPUT_PATH = "golden_set.csv"
EXAMPLE_PATH = "golden_set.example.csv"
EXAMPLE_PROFILES = [
    "Healthy", "Stalled", "Escalating", "Churning",
    "Commercial Risk Only", "Relationship Risk Only",
]


def main() -> int:
    client = salesforce_client.get_client(config.SALESFORCE_ORG_ALIAS)
    today = date.today().isoformat()

    accounts = client.soql_query(
        "SELECT Id, Name, Renewal_Risk_Profile__c, Expected_Outcome__c FROM Account "
        "WHERE Renewal_Risk_Profile__c != null LIMIT 200",
        today,
    )

    rows = []
    for acct in accounts:
        account_id = acct["Id"]
        contracts = client.soql_query(f"SELECT FIELDS(ALL) FROM Contract WHERE AccountId = '{account_id}' LIMIT 1", today)
        opps = client.soql_query(
            f"SELECT CloseDate FROM Opportunity WHERE AccountId = '{account_id}' ORDER BY CloseDate DESC LIMIT 1",
            today,
        )
        rows.append({
            "account_id": account_id,
            "name": acct["Name"],
            "arr": contracts[0].get("ARR__c") if contracts else "",
            "renewal_date": opps[0]["CloseDate"] if opps else "",
            "as_of": today,
            "outcome": acct.get("Expected_Outcome__c") or "",
            "_profile": acct.get("Renewal_Risk_Profile__c") or "",
        })

    fieldnames = ["account_id", "name", "arr", "renewal_date", "as_of", "outcome"]
    with open(OUTPUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows to {OUTPUT_PATH}")

    # A representative, tracked example: one account per profile.
    by_profile = {}
    for row in rows:
        by_profile.setdefault(row["_profile"], row)
    example_rows = [by_profile[p] for p in EXAMPLE_PROFILES if p in by_profile]
    with open(EXAMPLE_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(example_rows)
    print(f"Wrote {len(example_rows)} representative rows to {EXAMPLE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
