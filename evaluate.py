#!/usr/bin/env python3
"""
evaluate.py — precision@k vs baseline, fabrication check, faithfulness worksheet.

Reads results a prior `run.py` invocation logged to runs.sqlite (the Ledger)
for every account in a golden-set CSV, then runs the gates in order:

  Gate 0  audit_leakage across every account. Any violation -> refuse to
          print accuracy numbers for this run. Not negotiable.
  Gate 1  precision@k >= 2x baseline (baseline = ARR-descending, i.e. "flag
          the biggest accounts" — the naive thing CS ops does without a model).
  Fabrication check  every citation in a RiskAssessment must resolve to an
          evidence_ref one of that account's packets actually emitted.
  Faithfulness worksheet  can't be automated — writes a sample of evidence_refs
          to faithfulness_worksheet.csv for a CS ops analyst to open in
          Salesforce and confirm by hand. That review IS the gate; this just
          prepares the sample.

Usage: python3 evaluate.py golden_set.csv --k 20
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sqlite3
import sys

import config
import guard

# See run.py for why this is necessary on Windows.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

POSITIVE_OUTCOMES = {"churned", "downgraded"}


def load_golden(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _latest_detail(conn, account_id: str, stage: str):
    row = conn.execute(
        "SELECT detail FROM runs WHERE account_id = ? AND stage = ? ORDER BY ts DESC LIMIT 1",
        (account_id, stage),
    ).fetchone()
    return json.loads(row[0]) if row else None


def _latest_packets(conn, account_id: str) -> list[dict]:
    stages = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT stage FROM runs WHERE account_id = ? AND stage LIKE 'specialist:%'",
            (account_id,),
        ).fetchall()
    ]
    packets = []
    for stage in stages:
        detail = _latest_detail(conn, account_id, stage)
        if detail:
            packets.append(detail)
    return packets


def load_run_data(golden_rows: list[dict], db_path: str = config.RUNS_DB) -> dict[str, dict]:
    """account_id -> {assessment, packets, leakage_violations}. Missing accounts
    (never run) are reported, not silently skipped."""
    conn = sqlite3.connect(db_path)
    data = {}
    missing = []
    for row in golden_rows:
        account_id = row["account_id"]
        assessment = _latest_detail(conn, account_id, "synthesis")
        if assessment is None:
            missing.append(account_id)
            continue
        data[account_id] = {
            "assessment": assessment,
            "packets": _latest_packets(conn, account_id),
            "leakage_violations": _latest_detail(conn, account_id, "leakage_audit") or [],
        }
    conn.close()
    if missing:
        print(f"WARNING: {len(missing)} golden accounts have no run in {db_path} — run.py hasn't processed them: {missing}", file=sys.stderr)
    return data


# --- Gate 0: leakage --------------------------------------------------------

def gate0_leakage(run_data: dict[str, dict]) -> list[dict]:
    violations = []
    for account_id, data in run_data.items():
        for v in data["leakage_violations"]:
            violations.append({"account_id": account_id, **v})
    return violations


# --- Gate 1: precision@k vs baseline ----------------------------------------

def precision_at_k(rows: list[dict], score_key_fn, k: int) -> float:
    if k <= 0 or not rows:
        return 0.0
    ranked = sorted(rows, key=lambda r: -score_key_fn(r))
    top_k = ranked[:k]
    hits = sum(1 for r in top_k if r["outcome"] in POSITIVE_OUTCOMES)
    return hits / min(k, len(rows))


def gate1_precision(golden_rows: list[dict], run_data: dict[str, dict], k: int) -> dict:
    scorable = [r for r in golden_rows if r["account_id"] in run_data]
    agent_p = precision_at_k(scorable, lambda r: run_data[r["account_id"]]["assessment"]["score"], k)
    baseline_p = precision_at_k(scorable, lambda r: float(r["arr"]), k)

    if baseline_p == 0:
        passed = agent_p > 0
    else:
        passed = agent_p >= 2 * baseline_p

    return {"agent_precision_at_k": agent_p, "baseline_precision_at_k": baseline_p, "k": k, "passed": passed, "n": len(scorable)}


# --- Fabrication check -------------------------------------------------------

def fabrication_check(run_data: dict[str, dict]) -> list[dict]:
    """Every citation a RiskAssessment makes must resolve to an evidence_ref
    one of that account's own packets emitted. A citation that doesn't is a
    fabrication — the model claiming a source that was never given to it."""
    violations = []
    for account_id, data in run_data.items():
        known_refs = set()
        for packet in data["packets"]:
            known_refs.update(packet.get("evidence_refs", []))
        for citation in data["assessment"].get("citations", []):
            if citation not in known_refs:
                violations.append({"account_id": account_id, "fabricated_citation": citation})
    return violations


# --- Faithfulness worksheet (manual gate — this just prepares the sample) --

def write_faithfulness_worksheet(run_data: dict[str, dict], out_path: str = "faithfulness_worksheet.csv", sample_size: int = 25) -> int:
    candidates = []
    for account_id, data in run_data.items():
        for packet in data["packets"]:
            for signal in packet.get("signals", []):
                candidates.append(
                    {
                        "account_id": account_id,
                        "specialist": packet["specialist"],
                        "signal": signal["name"],
                        "claimed_value": signal["value"],
                        "evidence_ref": signal["evidence_ref"],
                        "verified_in_salesforce": "",
                        "notes": "",
                    }
                )
    sample = random.sample(candidates, min(sample_size, len(candidates))) if candidates else []
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["account_id", "specialist", "signal", "claimed_value", "evidence_ref", "verified_in_salesforce", "notes"])
        writer.writeheader()
        writer.writerows(sample)
    return len(sample)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the two hard gates + fabrication check against a golden set.")
    parser.add_argument("golden", help="Path to golden-set CSV (account_id,name,arr,renewal_date,as_of,outcome).")
    parser.add_argument("--k", type=int, default=20, help="k for precision@k.")
    parser.add_argument("--db", default=config.RUNS_DB, help="Path to runs.sqlite.")
    args = parser.parse_args()

    golden_rows = load_golden(args.golden)
    run_data = load_run_data(golden_rows, args.db)

    print(f"Loaded {len(golden_rows)} golden accounts, {len(run_data)} with a logged run.\n")

    # Gate 0 — non-negotiable, runs before anything else prints.
    leakage_violations = gate0_leakage(run_data)
    if leakage_violations:
        print("GATE 0 FAILED — leakage detected. Accuracy numbers are meaningless until this is fixed:")
        for v in leakage_violations:
            print(f"  {v}")
        return 1
    print("Gate 0 (leakage audit): PASS — 0 violations across all logged runs.\n")

    # Gate 1
    gate1 = gate1_precision(golden_rows, run_data, args.k)
    status = "PASS" if gate1["passed"] else "FAIL"
    print(
        f"Gate 1 (precision@{gate1['k']}): {status} — agent={gate1['agent_precision_at_k']:.2f} "
        f"vs baseline={gate1['baseline_precision_at_k']:.2f} (need agent >= 2x baseline), n={gate1['n']}"
    )
    print()

    # Fabrication check
    fab_violations = fabrication_check(run_data)
    if fab_violations:
        print(f"Fabrication check: FAIL — {len(fab_violations)} citation(s) not backed by any packet evidence_ref:")
        for v in fab_violations:
            print(f"  {v}")
    else:
        print("Fabrication check: PASS — every citation resolves to a real evidence_ref.")
    print()

    # Faithfulness worksheet — manual gate, half a day, most valuable half day of the POC.
    n = write_faithfulness_worksheet(run_data)
    print(f"Faithfulness worksheet: {n} evidence_ref(s) sampled to faithfulness_worksheet.csv.")
    print("Hand this to a CS ops analyst who didn't build this pipeline — target >= 95% confirmed in Salesforce.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
