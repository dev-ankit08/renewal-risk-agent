#!/usr/bin/env python3
"""
build_golden_set.py — one-off evaluation helper for the Salesforce test sandbox.

Reads the tagged test accounts (Account.Renewal_Risk_Profile__c != null), each
one's Contract.ARR__c and latest Opportunity.CloseDate, and writes
golden_set.csv with Account.Expected_Outcome__c as `outcome`.

This is the ONLY place that reads Renewal_Risk_Profile__c / Expected_Outcome__c:
they are the answer key for evaluation, never an input to the agent (the
Salesforce preset selects Account fields explicitly and never names them).

as_of is today for every row: the sandbox data was bulk-loaded, so every
CreatedDate is the load date and an earlier as_of would exclude everything.

Usage:
  python scripts/build_golden_set.py --config examples/salesforce-only/config [--source crm]
  renewal-risk --config examples/salesforce-only/config assess --golden golden_set.csv
  renewal-risk --config examples/salesforce-only/config evaluate golden_set.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from renewal_risk.connectors.salesforce import escape_soql  # noqa: E402
from renewal_risk.core.connector import create  # noqa: E402
from renewal_risk.core.settings import load_settings  # noqa: E402

FIELDS = ["account_id", "name", "arr", "renewal_date", "as_of", "outcome"]
EXAMPLE_PROFILES = ["Healthy", "Stalled", "Escalating", "Churning", "Commercial Risk Only", "Relationship Risk Only"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.environ.get("RENEWAL_RISK_CONFIG_DIR", "config"))
    parser.add_argument("--source", help="the salesforce source name (default: the primary source)")
    parser.add_argument("--out", default="golden_set.csv")
    args = parser.parse_args()

    settings = load_settings(args.config)
    name = args.source or settings.primary
    conn = create(name, settings.sources[name])
    if conn.type_name != "salesforce":
        print(f"source '{name}' is not a salesforce source", file=sys.stderr)
        return 2

    def soql(query: str) -> list[dict]:
        return conn.execute({"soql": query}, {}, "list")

    today = date.today().isoformat()
    rows = []
    for acct in soql("SELECT Id, Name, Renewal_Risk_Profile__c, Expected_Outcome__c FROM Account WHERE Renewal_Risk_Profile__c != null LIMIT 200"):
        account_id = escape_soql(acct["Id"])
        contracts = soql(f"SELECT ARR__c FROM Contract WHERE AccountId = '{account_id}' LIMIT 1")
        opps = soql(f"SELECT CloseDate FROM Opportunity WHERE AccountId = '{account_id}' ORDER BY CloseDate DESC LIMIT 1")
        rows.append({
            "account_id": acct["Id"], "name": acct["Name"],
            "arr": contracts[0].get("ARR__c") if contracts else "",
            "renewal_date": opps[0]["CloseDate"] if opps else "",
            "as_of": today, "outcome": acct.get("Expected_Outcome__c") or "",
            "_profile": acct.get("Renewal_Risk_Profile__c") or "",
        })
    conn.close()

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows to {args.out}")

    by_profile: dict[str, dict] = {}
    for row in rows:
        by_profile.setdefault(row["_profile"], row)
    example = Path(__file__).resolve().parent.parent / "examples" / "salesforce-only" / "golden_set.example.csv"
    with open(example, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(by_profile[p] for p in EXAMPLE_PROFILES if p in by_profile)
    print(f"Wrote representative rows to {example}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
