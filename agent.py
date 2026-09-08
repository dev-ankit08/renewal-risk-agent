"""
agent.py — orchestrator, ledger, brief rendering.

run_account() is the whole pipeline for one account:
  Coordinator -> gather records per specialist (through soql_guard) ->
  specialists -> leakage audit (Gate 0) -> Synthesis -> RunResult.

render_brief()/write_brief()/append_briefs_csv() turn a RunResult into the
one-page markdown brief and the rollup CSV. Ledger writes every stage to
runs.sqlite for later inspection ("read the packets by hand").
"""
from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import config
import guard
import model_client
import salesforce_client

# OpportunityFieldHistory / CaseHistory are deliberately excluded: neither has
# any populated data for this org's bulk-loaded test records (no real
# field-change tracking occurred), and CaseHistory is never consumed by any
# heuristic. Re-add OpportunityFieldHistory if real field-history tracking is
# ever enabled and opp_stalled needs to be evaluated for real.
COMMERCIAL_OBJECTS = ["Opportunity", "Contract", "Asset"]
RELATIONSHIP_OBJECTS = ["Case", "Contact", "OpportunityContactRole", "Task", "Event"]

SPECIALIST_OBJECTS = {
    "commercial": COMMERCIAL_OBJECTS,
    "relationship": RELATIONSHIP_OBJECTS,
}

# Guardrail: this pipeline must never query Account fields beyond Id/AccountId
# joins. Account.Renewal_Risk_Profile__c / Account.Expected_Outcome__c are
# ground-truth test labels (see build_golden_set.py) — reading them here would
# be the agent answering from the answer key instead of detecting risk itself.


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    """Append-only run log to runs.sqlite — one row per pipeline stage."""

    def __init__(self, path: str = config.RUNS_DB):
        self.path = path
        conn = sqlite3.connect(self.path)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS runs (
                account_id TEXT,
                as_of TEXT,
                stage TEXT,
                detail TEXT,
                ts TEXT
            )"""
        )
        conn.commit()
        conn.close()

    def log(self, account_id: str, as_of: str, stage: str, detail) -> None:
        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT INTO runs (account_id, as_of, stage, detail, ts) VALUES (?, ?, ?, ?, ?)",
            (account_id, as_of, stage, json.dumps(detail, default=str), _now_iso()),
        )
        conn.commit()
        conn.close()


def gather_records(client: salesforce_client.SalesforceClient, specialist: str, account_id: str, as_of: str) -> dict[str, list[dict]]:
    """Issue one SOQL query per object this specialist owns. Every call goes
    through the client, which routes it through soql_guard before returning
    anything.

    Real Salesforce has no `SELECT *` — every query names its fields via
    FIELDS(ALL), which requires an explicit LIMIT (Salesforce caps FIELDS(ALL)
    queries at 200 rows). Most objects filter directly on AccountId, but
    OpportunityContactRole has no AccountId field at all — it's reached via a
    two-step join through this account's own Opportunity Ids."""
    records: dict[str, list[dict]] = {}
    for object_name in SPECIALIST_OBJECTS[specialist]:
        if object_name == "OpportunityContactRole":
            # Opportunity is never in the same specialist's object list as
            # OpportunityContactRole (it's commercial-only), so this is always
            # a fresh, cheap Id-only lookup, not a full-record re-fetch.
            opp_soql = f"SELECT Id FROM Opportunity WHERE AccountId = '{account_id}' LIMIT 200"
            opp_ids = [r["Id"] for r in client.soql_query(opp_soql, as_of)]
            if not opp_ids:
                records[object_name] = []
                continue
            id_list = ", ".join(f"'{oid}'" for oid in opp_ids)
            soql = f"SELECT FIELDS(ALL) FROM OpportunityContactRole WHERE OpportunityId IN ({id_list}) LIMIT 200"
        else:
            soql = f"SELECT FIELDS(ALL) FROM {object_name} WHERE AccountId = '{account_id}' LIMIT 200"
        records[object_name] = client.soql_query(soql, as_of)
    return records


@dataclass
class RunResult:
    account_id: str
    as_of: str
    coordinator: dict
    packets: list[dict]
    assessment: dict
    leakage_violations: list[dict]


def run_account(
    account_id: str,
    as_of: str,
    client: salesforce_client.SalesforceClient | None = None,
    ledger: Ledger | None = None,
) -> RunResult:
    client = client or salesforce_client.get_client(config.SALESFORCE_ORG_ALIAS)
    ledger = ledger or Ledger()

    plan = model_client.call_coordinator(account_id)
    ledger.log(account_id, as_of, "coordinator", plan)

    packets = []
    all_records: list[dict] = []
    specialists = plan.get("specialists") or ["commercial", "relationship"]
    for specialist in specialists:
        records = gather_records(client, specialist, account_id, as_of)
        for recs in records.values():
            all_records.extend(recs)
        packet = model_client.call_specialist(specialist, account_id, as_of, records)
        ledger.log(account_id, as_of, f"specialist:{specialist}", packet)
        packets.append(packet)

    # Gate 0: post-hoc leakage audit across every record gathered this run.
    leakage_violations = guard.audit_leakage(all_records, as_of)
    ledger.log(account_id, as_of, "leakage_audit", leakage_violations)

    assessment = model_client.call_synthesis(account_id, packets)
    ledger.log(account_id, as_of, "synthesis", assessment)

    return RunResult(
        account_id=account_id,
        as_of=as_of,
        coordinator=plan,
        packets=packets,
        assessment=assessment,
        leakage_violations=leakage_violations,
    )


def render_brief(result: RunResult, account_name: str | None = None) -> str:
    a = result.assessment
    lines = [
        f"# Renewal Risk Brief — {account_name or result.account_id}",
        "",
        f"**Account:** {result.account_id}  ",
        f"**As of:** {result.as_of}  ",
        f"**Risk score:** {a['score']} ({a['band']})  ",
        f"**Abstained:** {a['abstained']}",
        "",
        "## Drivers",
    ]
    if a["drivers"]:
        lines.extend(f"- {d}" for d in a["drivers"])
    else:
        lines.append("- none")

    lines += ["", "## Evidence by specialist"]
    for packet in result.packets:
        lines.append(f"### {packet['specialist'].capitalize()}")
        if packet["signals"]:
            for s in packet["signals"]:
                lines.append(f"- **{s['name']}** ({s['severity']}): {s['value']} — `{s['evidence_ref']}`")
        else:
            lines.append("- no signals detected")
        if packet["gaps"]:
            lines.append("- Gaps: " + "; ".join(packet["gaps"]))

    lines += ["", "## Citations"]
    if a["citations"]:
        lines.extend(f"- `{c}`" for c in a["citations"])
    else:
        lines.append("- none")

    lines += ["", "## Rationale", a["rationale"]]

    if result.leakage_violations:
        lines += ["", "## ⚠ Leakage audit violations", json.dumps(result.leakage_violations, default=str)]

    lines += ["", "---", "_No Salesforce write-back. No Slack/email. Read-only pipeline._"]
    return "\n".join(lines) + "\n"


def write_brief(result: RunResult, account_name: str | None = None, briefs_dir: str = config.BRIEFS_DIR) -> Path:
    Path(briefs_dir).mkdir(parents=True, exist_ok=True)
    path = Path(briefs_dir) / f"{result.account_id}.md"
    path.write_text(render_brief(result, account_name), encoding="utf-8")
    return path


def append_briefs_csv(result: RunResult, account_name: str | None = None, csv_path: str = config.BRIEFS_CSV) -> None:
    file_exists = Path(csv_path).exists()
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["account_id", "account_name", "as_of", "score", "band", "abstained", "leakage_violations"])
        a = result.assessment
        writer.writerow(
            [
                result.account_id,
                account_name or "",
                result.as_of,
                a["score"],
                a["band"],
                a["abstained"],
                len(result.leakage_violations),
            ]
        )
