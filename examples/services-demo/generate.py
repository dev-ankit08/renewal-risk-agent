#!/usr/bin/env python3
"""
generate.py — offline demo for the IT services pack (all synthetic).

    python examples/services-demo/generate.py            # relative to today
    python examples/services-demo/generate.py 2026-10-10

Systems:
  data/crm/        clients, SOW renewals / extensions, contracts (SOWs), contacts, activities
  data/psa/        projects (RAG, budget, plan vs forecast), milestones, change requests, timesheets
  data/desk/       escalations (Jira-like export)
  data/finance/    invoices

Stories:
  C001 Northwind Bank    high    RED project 30% over budget, 2 missed milestones, open escalation,
                                 SOW ends in 45 days with no extension in the pipeline
  C002 Contoso Retail    medium  billable hours down ~60% (work winding down), SOW renewal open
  C003 Fabrikam Health   low     healthy delivery, extension in negotiation
  C004 Tailspin Air      medium  AMBER project forecast 40 days late, an invoice overdue
"""
from __future__ import annotations

import csv
import sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent


def build(root: Path, today: date) -> None:
    d = lambda days: (today + timedelta(days=days)).isoformat()  # noqa: E731

    def write(rel: str, header: list[str], rows: list[list]) -> None:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)

    write("crm/clients.csv", ["ClientId", "ClientName", "Website", "AccountManager", "Practice", "Region", "Created"], [
        ["C001", "Northwind Bank", "northwind.example", "pd.one@services.example", "Banking", "EMEA", d(-1000)],
        ["C002", "Contoso Retail", "contoso.example", "pd.two@services.example", "Retail", "AMER", d(-900)],
        ["C003", "Fabrikam Health", "fabrikam.example", "pd.one@services.example", "Healthcare", "EMEA", d(-800)],
        ["C004", "Tailspin Air", "tailspin.example", "pd.two@services.example", "Travel", "APAC", d(-700)],
    ])
    write("crm/opportunities.csv", ["OppId", "ClientId", "ClientName", "Title", "Value", "ExpectedClose", "Stage", "Status", "Created"], [
        ["O002", "C002", "Contoso Retail", "Contoso managed services renewal", 400000, d(70), "Proposal", "Open", d(-40)],
        ["O003", "C003", "Fabrikam Health", "Fabrikam phase 3 extension", 650000, d(50), "Negotiation", "Open", d(-30)],
        ["O004", "C004", "Tailspin Air", "Tailspin support extension", 220000, d(90), "Qualification", "Open", d(-20)],
        ["O001", "C001", "Northwind Bank", "Northwind phase 1 (won)", 900000, d(-320), "Closed Won", "Won", d(-400)],
    ])
    write("crm/sows.csv", ["SowId", "ClientId", "AnnualValue", "Status", "Start", "End", "Created"], [
        ["S001", "C001", 900000, "Active", d(-320), d(45), d(-330)],
        ["S002", "C002", 500000, "Active", d(-295), d(70), d(-300)],
        ["S003", "C003", 600000, "Active", d(-315), d(50), d(-320)],
        ["S004", "C004", 200000, "Active", d(-275), d(90), d(-280)],
    ])
    write("crm/contacts.csv", ["ContactId", "ClientId", "FullName", "JobTitle", "Sponsor", "Active", "Created"], [
        ["K001", "C001", "Riya Mehta", "CTO", "yes", "yes", d(-500)],
        ["K002", "C001", "Tom Gray", "Head of Digital", "no", "yes", d(-300)],
        ["K003", "C002", "Ana Silva", "VP IT", "yes", "yes", d(-400)],
        ["K004", "C003", "Ben Okafor", "CIO", "yes", "yes", d(-350)],
        ["K005", "C003", "Lia Wong", "PMO lead", "no", "yes", d(-200)],
        ["K006", "C004", "Sam Patel", "Director Ops", "yes", "yes", d(-300)],
        ["K007", "C004", "Eve Novak", "Procurement", "no", "yes", d(-120)],
    ])
    write("crm/meetings.csv", ["MeetingId", "ClientId", "Kind", "Date"], [
        ["M001", "C001", "steering committee", d(-35)], ["M002", "C002", "QBR", d(-20)],
        ["M003", "C003", "steering committee", d(-6)], ["M004", "C004", "QBR", d(-15)],
    ])

    write("psa/projects.csv", ["ProjectId", "ClientId", "ProjectName", "Phase", "Health", "HealthSince", "BudgetHours", "ActualHours",
                               "StartDate", "PlannedEnd", "ForecastEnd", "Created"], [
        ["P001", "C001", "Core banking migration", "In progress", "Red", d(-21), 8000, 10400, d(-320), d(30), d(55), d(-330)],
        ["P002", "C002", "Store systems support", "In progress", "Green", d(-90), 6000, 4200, d(-295), d(70), d(70), d(-300)],
        ["P003", "C003", "Patient portal phase 2", "In progress", "Green", d(-120), 5000, 4100, d(-315), d(50), d(52), d(-320)],
        ["P004", "C004", "Crew scheduling rollout", "In progress", "Amber", d(-30), 3000, 2900, d(-275), d(20), d(60), d(-280)],
    ])
    write("psa/milestones.csv", ["MilestoneId", "ClientId", "ProjectId", "Name", "Due", "Done", "DoneOn", "Created"], [
        ["MS1", "C001", "P001", "Data migration rehearsal", d(-30), "no", "", d(-300)],
        ["MS2", "C001", "P001", "UAT sign-off", d(-10), "no", "", d(-300)],
        ["MS3", "C001", "P001", "Design approved", d(-200), "yes", d(-198), d(-300)],
        ["MS4", "C003", "P003", "Beta release", d(-20), "yes", d(-21), d(-300)],
        ["MS5", "C004", "P004", "Pilot airport live", d(-5), "yes", d(-2), d(-250)],
    ])
    write("psa/change_requests.csv", ["CrId", "ClientId", "ProjectId", "Status", "Value", "Created"], [
        ["CR1", "C001", "P001", "Submitted", 120000, d(-25)],
        ["CR2", "C003", "P003", "Approved", 80000, d(-60)],
    ])
    rows = []
    hours = {"C001": (400, 420), "C002": (380, 150), "C003": (300, 310), "C004": (200, 190)}  # (prior 30d, last 30d) per client
    for client, (prior, recent) in hours.items():
        for week in range(8):  # 4 weeks recent, 4 weeks prior
            day = today - timedelta(days=7 * week + 3)
            rows.append([f"T-{client}-{week}", client, f"P00{client[-1]}", round((recent if week < 4 else prior) / 4, 1), "Y", day.isoformat()])
    write("psa/timesheets.csv", ["EntryId", "ClientId", "ProjectId", "Hours", "Billable", "WorkDate"], rows)

    write("desk/escalations.csv", ["Key", "ClientId", "Priority", "Status", "Created", "Resolved", "Updated"], [
        ["ESC-11", "C001", "Highest", "In Progress", d(-12), "", d(-1)],
        ["ESC-7", "C001", "High", "Done", d(-150), d(-140), d(-140)],
        ["ESC-9", "C004", "High", "Done", d(-80), d(-70), d(-70)],
    ])
    write("finance/invoices.csv", ["InvoiceNo", "ClientId", "Amount", "Balance", "Status", "DueDate", "Issued"], [
        ["INV-100", "C001", 75000, 0, "Paid", d(-40), d(-70)],
        ["INV-200", "C002", 42000, 0, "Paid", d(-20), d(-50)],
        ["INV-300", "C003", 50000, 0, "Paid", d(-15), d(-45)],
        ["INV-400", "C004", 17000, 17000, "Open", d(-25), d(-55)],
    ])


if __name__ == "__main__":
    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    build(HERE / "data", day)
    print(f"Services demo data written to {HERE / 'data'} (relative to {day}).")
