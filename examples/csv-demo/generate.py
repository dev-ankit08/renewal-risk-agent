#!/usr/bin/env python3
"""
generate.py — writes the offline demo dataset (four "systems") with dates
relative to a given day, so the demo always looks current.

    python examples/csv-demo/generate.py            # relative to today
    python examples/csv-demo/generate.py 2026-10-10 # relative to a fixed day (tests)

Systems (all synthetic):
  data/crm/       accounts, renewals, contracts, line items, contacts, activities   (CRM, primary)
  data/billing/   customers, invoices, payments                                     (Stripe-like)
  data/support/   organizations, tickets                                            (Zendesk-like)
  data/usage.sqlite   usage(id, tenant_id, metric, day, value)                       (product DB)

Built-in stories:
  A001 Acme Corp        high   renewal 25% below ARR, escalations, overdue invoices, champion left, usage down
  A002 Globex           medium open ticket backlog, no activity for ~75 days      (billing linked by domain only)
  A003 Initech          low    healthy
  A004 Umbrella Health  high   failed + disputed payments, subscription set to cancel
  A005 Stark Logistics  medium low seat utilisation                               (billing match needs review)
  A006 Wayne Retail     low    healthy, no billing customer at all
"""
from __future__ import annotations

import csv
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent


def build(root: Path, today: date) -> None:
    d = lambda days: (today + timedelta(days=days)).isoformat()  # noqa: E731
    epoch = lambda days: int(datetime.combine(today + timedelta(days=days), datetime.min.time(), tzinfo=timezone.utc).timestamp())  # noqa: E731

    def write(rel: str, header: list[str], rows: list[list]) -> None:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)

    write("crm/accounts.csv", ["Id", "Name", "Website", "OwnerEmail", "Region", "Segment", "CreatedDate"], [
        ["A001", "Acme Corp", "https://www.acme.example", "csm.one@demo.example", "EMEA", "Enterprise", d(-900)],
        ["A002", "Globex", "globex.example", "csm.two@demo.example", "EMEA", "Mid-market", d(-800)],
        ["A003", "Initech", "initech.example", "csm.one@demo.example", "AMER", "Mid-market", d(-700)],
        ["A004", "Umbrella Health", "umbrella-health.example", "csm.two@demo.example", "AMER", "Enterprise", d(-650)],
        ["A005", "Stark Logistics", "stark-logistics.example", "csm.one@demo.example", "APAC", "Enterprise", d(-600)],
        ["A006", "Wayne Retail", "wayne-retail.example", "csm.two@demo.example", "APAC", "SMB", d(-500)],
    ])
    write("crm/renewals.csv", ["Id", "AccountId", "AccountName", "Name", "Amount", "CloseDate", "Stage", "IsClosed", "StageChanged", "CreatedDate"], [
        ["R001", "A001", "Acme Corp", "Acme renewal", 90000, d(45), "Negotiation", "false", d(-75), d(-120)],
        ["R002", "A002", "Globex", "Globex renewal", 60000, d(60), "Proposal", "false", d(-20), d(-90)],
        ["R003", "A003", "Initech", "Initech renewal", 52000, d(30), "Commit", "false", d(-5), d(-60)],
        ["R004", "A004", "Umbrella Health", "Umbrella renewal", 150000, d(75), "Discovery", "false", d(-10), d(-50)],
        ["R005", "A005", "Stark Logistics", "Stark renewal", 200000, d(100), "Proposal", "false", d(-12), d(-40)],
        ["R006", "A006", "Wayne Retail", "Wayne renewal", 15000, d(20), "Commit", "false", d(-3), d(-45)],
        ["R007", "A003", "Initech", "Initech 2025 renewal", 48000, d(-335), "Closed Won", "true", d(-335), d(-400)],
        # Closed renewals from a year+ ago, with the history before them: what the Accuracy backtest replays.
        ["R101", "A001", "Acme Corp", "Acme 2025 renewal", 100000, d(-410), "Closed Won", "true", d(-410), d(-560)],
        ["R102", "A002", "Globex", "Globex 2025 renewal", 180000, d(-430), "Closed Won", "true", d(-430), d(-600)],
        ["R104", "A004", "Umbrella Health", "Umbrella 2025 renewal", 150000, d(-420), "Closed Lost", "true", d(-420), d(-600)],
        ["R105", "A005", "Stark Logistics", "Stark 2025 renewal", 200000, d(-400), "Closed Won", "true", d(-400), d(-560)],
        ["R106", "A006", "Wayne Retail", "Wayne 2025 renewal", 15000, d(-390), "Closed Lost", "true", d(-390), d(-480)],
    ])
    write("crm/contracts.csv", ["Id", "AccountId", "ARR", "Status", "StartDate", "EndDate", "CreatedDate"], [
        ["C001", "A001", 120000, "Activated", d(-320), d(45), d(-330)],
        ["C002", "A002", 60000, "Activated", d(-300), d(60), d(-310)],
        ["C003", "A003", 50000, "Activated", d(-335), d(30), d(-340)],
        ["C004", "A004", 150000, "Activated", d(-290), d(75), d(-300)],
        ["C005", "A005", 200000, "Activated", d(-265), d(100), d(-270)],
        ["C006", "A006", 15000, "Activated", d(-345), d(20), d(-350)],
        ["C101", "A001", 140000, "Expired", d(-775), d(-410), d(-780)],  # Acme renewed smaller: 140k -> 100k
        ["C102", "A002", 150000, "Expired", d(-795), d(-430), d(-800)],
    ])
    write("crm/line_items.csv", ["Id", "AccountId", "Product", "Quantity", "Status", "CreatedDate", "LastModifiedDate"], [
        ["L001", "A001", "Platform seats", 120, "Installed", d(-320), d(-30)],
        ["L002", "A001", "Analytics add-on", 0, "Obsolete", d(-320), d(-25)],
        ["L003", "A002", "Platform seats", 60, "Installed", d(-300), d(-100)],
        ["L004", "A003", "Platform seats", 50, "Installed", d(-335), d(-90)],
        ["L005", "A004", "Platform seats", 150, "Installed", d(-290), d(-60)],
        ["L006", "A005", "Platform seats", 400, "Installed", d(-265), d(-60)],
        ["L007", "A006", "Platform seats", 15, "Installed", d(-345), d(-100)],
    ])
    write("crm/contacts.csv", ["Id", "AccountId", "Name", "Title", "IsPrimary", "Status", "CreatedDate"], [
        ["P001", "A001", "Dana Reyes", "VP Operations", "true", "left", d(-400)],
        ["P002", "A001", "Lee Park", "IT Manager", "false", "active", d(-200)],
        ["P003", "A002", "Sam Ortiz", "Head of Support", "true", "active", d(-300)],
        ["P004", "A003", "Kim Lo", "COO", "true", "active", d(-330)],
        ["P005", "A003", "Ari Shah", "Finance Lead", "false", "active", d(-200)],
        ["P006", "A004", "Jo Bell", "CIO", "true", "active", d(-280)],
        ["P007", "A004", "Rae Kim", "Procurement", "false", "active", d(-100)],
        ["P008", "A005", "Max Hart", "Director IT", "true", "active", d(-260)],
        ["P009", "A005", "Ivy Chen", "Ops Analyst", "false", "active", d(-200)],
        ["P010", "A006", "Ned Ray", "Owner", "true", "active", d(-340)],
        ["P011", "A006", "Uma Das", "Store Manager", "false", "active", d(-150)],
    ])
    write("crm/activities.csv", ["Id", "AccountId", "Type", "ActivityDate", "CreatedDate"], [
        ["E001", "A001", "meeting", d(-40), d(-40)],
        ["E002", "A002", "call", d(-75), d(-75)],
        ["E003", "A003", "meeting", d(-7), d(-7)],
        ["E004", "A004", "meeting", d(-14), d(-14)],
        ["E005", "A005", "call", d(-21), d(-21)],
        ["E006", "A006", "meeting", d(-10), d(-10)],
    ])

    write("billing/customers.csv", ["id", "name", "email", "crm_account_id", "created"], [
        ["cus_acme", "Acme Corp", "ap@acme.example", "A001", epoch(-900)],
        ["cus_glbx", "Globex Inc.", "billing@globex.example", "", epoch(-800)],
        ["cus_init", "Initech", "finance@initech.example", "A003", epoch(-700)],
        ["cus_umb", "Umbrella Health", "accounts@umbrella-health.example", "A004", epoch(-650)],
        ["cus_stark", "Stark Logistics Ltd", "stark.logistics.ap@gmail.com", "", epoch(-600)],
        ["cus_orphan", "Unrelated Co", "x@unrelated.example", "", epoch(-300)],
    ])
    write("billing/invoices.csv", ["id", "customer", "total", "amount_remaining", "status", "due_date", "created"], [
        ["in_a1", "cus_acme", 3000000, 3000000, "open", epoch(-40), epoch(-70)],
        ["in_a2", "cus_acme", 3000000, 3000000, "open", epoch(-10), epoch(-40)],
        ["in_a3", "cus_acme", 3000000, 0, "paid", epoch(-100), epoch(-130)],
        ["in_g1", "cus_glbx", 1500000, 0, "paid", epoch(-20), epoch(-50)],
        ["in_i1", "cus_init", 1250000, 0, "paid", epoch(-15), epoch(-45)],
        ["in_u1", "cus_umb", 3750000, 0, "paid", epoch(-30), epoch(-60)],
        ["in_s1", "cus_stark", 5000000, 0, "paid", epoch(-25), epoch(-55)],
    ])
    write("billing/payments.csv", ["id", "customer", "amount", "status", "disputed", "created"], [
        ["ch_u1", "cus_umb", 3750000, "failed", "false", epoch(-35)],
        ["ch_u2", "cus_umb", 3750000, "succeeded", "true", epoch(-28)],
        ["ch_i1", "cus_init", 1250000, "succeeded", "false", epoch(-14)],
    ])
    write("billing/subscriptions.csv", ["id", "customer", "status", "cancel_at_period_end", "current_period_end", "created"], [
        ["sub_u1", "cus_umb", "active", "true", epoch(75), epoch(-290)],
        ["sub_a1", "cus_acme", "active", "false", epoch(45), epoch(-320)],
    ])

    write("support/organizations.csv", ["id", "name", "domain_names", "external_id"], [
        ["9001", "Acme Corp", "acme.example", "A001"],
        ["9002", "Globex", "globex.example", "A002"],
        ["9003", "Initech", "initech.example", "A003"],
        ["9004", "Umbrella Health", "umbrella-health.example", "A004"],
        ["9005", "Stark Logistics", "stark-logistics.example", "A005"],
        ["9006", "Wayne Retail", "wayne-retail.example", "A006"],
    ])
    tickets = [
        # Acme: escalations, high priority, recent spike
        ["7001", "9001", "open", "urgent", "escalated billing", 1, d(-12), d(-2)],
        ["7002", "9001", "open", "high", "escalated", "", d(-20), d(-3)],
        ["7003", "9001", "pending", "normal", "", "", d(-35), d(-5)],
        ["7004", "9001", "solved", "normal", "", 1, d(-60), d(-50)],
        ["7005", "9001", "solved", "low", "", "", d(-250), d(-240)],
        # Globex: backlog
        ["7011", "9002", "open", "normal", "", "", d(-30), d(-6)],
        ["7012", "9002", "open", "normal", "", "", d(-25), d(-6)],
        ["7013", "9002", "pending", "normal", "", "", d(-15), d(-6)],
        # Initech: healthy
        ["7021", "9003", "solved", "low", "", 5, d(-100), d(-95)],
        # Stark: one open normal
        ["7051", "9005", "open", "normal", "", "", d(-8), d(-1)],
        # A year+ ago, before the 2025 renewals: Acme and Umbrella escalated and unhappy, Stark too (renewed anyway).
        ["7101", "9001", "solved", "high", "escalated", 1, d(-560), d(-540)],
        ["7102", "9001", "solved", "urgent", "escalated", "", d(-530), d(-520)],
        ["7141", "9004", "solved", "high", "escalated", 1, d(-560), d(-545)],
        ["7142", "9004", "solved", "urgent", "escalated", 2, d(-525), d(-515)],
        ["7151", "9005", "solved", "high", "escalated", 2, d(-540), d(-530)],
        ["7152", "9005", "solved", "normal", "escalated", "", d(-520), d(-505)],
    ]
    write("support/tickets.csv", ["id", "organization_id", "status", "priority", "tags", "satisfaction", "created_at", "updated_at"], tickets)

    db = root / "usage.sqlite"
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE usage (id TEXT PRIMARY KEY, tenant_id TEXT, metric TEXT, day TEXT, value REAL)")
    profile = {"A001": (100, 55), "A002": (55, 52), "A003": (45, 47), "A004": (130, 125), "A005": (150, 140), "A006": (14, 14)}
    rows = []
    for tenant, (before, after) in profile.items():
        for week in range(1, 9):  # 8 weekly points: weeks 1-4 recent, 5-8 prior
            day = today - timedelta(days=7 * week - 3)
            value = after if week <= 4 else before
            rows.append((f"U-{tenant}-{week}", tenant, "active_users", day.isoformat(), value))
    conn.executemany("INSERT INTO usage VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    build(HERE / "data", day)
    print(f"Demo data written to {HERE / 'data'} (relative to {day}).")
