"""
evaluate.py — the evaluation gates, over runs logged in the store.

  Gate 0  leakage: any record dated after as_of in any run -> no accuracy numbers
  Gate 1  precision@k >= 2x a naive baseline (rank by ARR, biggest first)
  Fabrication  every citation / action ref must be an evidence_ref a packet emitted
  Faithfulness worksheet  a sample of evidence for a human to verify in the source system

Golden CSV columns: account_id,name,arr,renewal_date,as_of,outcome
(outcome: renewed | churned | downgraded). Run the assessments first with
`renewal-risk assess --golden golden.csv`.
"""
from __future__ import annotations

import csv
import json
import random
import sqlite3

POSITIVE_OUTCOMES = {"churned", "downgraded"}


def load_golden(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _latest(conn, account_id: str, stage: str):
    row = conn.execute("SELECT detail FROM runs WHERE account_id = ? AND stage = ? ORDER BY rowid DESC LIMIT 1", (account_id, stage)).fetchone()
    return json.loads(row[0]) if row else None


def load_run_data(golden: list[dict], db_path: str) -> tuple[dict, list[str]]:
    conn = sqlite3.connect(db_path)
    data, missing = {}, []
    for row in golden:
        account_id = row["account_id"]
        assessment = _latest(conn, account_id, "synthesis")
        if assessment is None:
            missing.append(account_id)
            continue
        stages = [r[0] for r in conn.execute("SELECT DISTINCT stage FROM runs WHERE account_id = ? AND stage LIKE 'specialist:%'", (account_id,))]
        data[account_id] = {
            "assessment": assessment,
            "packets": [p for p in (_latest(conn, account_id, s) for s in stages) if p],
            "leakage_violations": _latest(conn, account_id, "leakage_audit") or [],
        }
    conn.close()
    return data, missing


def gate0_leakage(run_data: dict) -> list[dict]:
    return [{"account_id": a, **v} for a, d in run_data.items() for v in d["leakage_violations"]]


def precision_at_k(rows: list[dict], score, k: int) -> float:
    if k <= 0 or not rows:
        return 0.0
    top = sorted(rows, key=lambda r: -score(r))[:k]
    return sum(1 for r in top if r["outcome"] in POSITIVE_OUTCOMES) / min(k, len(rows))


def gate1_precision(golden: list[dict], run_data: dict, k: int) -> dict:
    rows = [r for r in golden if r["account_id"] in run_data]
    agent_p = precision_at_k(rows, lambda r: run_data[r["account_id"]]["assessment"]["score"], k)
    base_p = precision_at_k(rows, lambda r: float(r.get("arr") or 0), k)
    passed = agent_p > 0 if base_p == 0 else agent_p >= 2 * base_p
    return {"agent": agent_p, "baseline": base_p, "k": k, "n": len(rows), "passed": passed}


def fabrication_check(run_data: dict) -> list[dict]:
    out = []
    for account_id, d in run_data.items():
        known = {r for p in d["packets"] for r in p.get("evidence_refs", [])}
        refs = list(d["assessment"].get("citations", []))
        for action in d["assessment"].get("recommended_actions", []):
            refs.extend(action.get("evidence_refs", []))
        out.extend({"account_id": account_id, "ref": r} for r in refs if r not in known)
    return out


def write_worksheet(run_data: dict, path: str = "faithfulness_worksheet.csv", n: int = 25) -> int:
    rows = [
        {"account_id": a, "specialist": p["specialist"], "signal": s["name"], "claimed_value": s["value"],
         "evidence_ref": s["evidence_ref"], "verified_in_source": "", "notes": ""}
        for a, d in run_data.items() for p in d["packets"] for s in p.get("signals", [])
    ]
    sample = random.sample(rows, min(n, len(rows))) if rows else []
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["account_id", "specialist", "signal", "claimed_value", "evidence_ref", "verified_in_source", "notes"])
        writer.writeheader()
        writer.writerows(sample)
    return len(sample)


def run(golden_path: str, db_path: str, k: int) -> int:
    golden = load_golden(golden_path)
    data, missing = load_run_data(golden, db_path)
    print(f"Loaded {len(golden)} golden accounts, {len(data)} with a logged run.")
    if missing:
        print(f"WARNING: no run logged for {len(missing)} account(s): {missing}")
    leaks = gate0_leakage(data)
    if leaks:
        print("GATE 0 FAILED — future-dated records reached a run. Accuracy numbers withheld:")
        for v in leaks[:50]:
            print(f"  {v}")
        return 1
    print("Gate 0 (leakage): PASS")
    g1 = gate1_precision(golden, data, k)
    print(f"Gate 1 (precision@{k}): {'PASS' if g1['passed'] else 'FAIL'} — agent {g1['agent']:.2f} vs baseline {g1['baseline']:.2f} (need >= 2x), n={g1['n']}")
    fab = fabrication_check(data)
    print(f"Fabrication check: {'PASS' if not fab else f'FAIL — {len(fab)} unbacked ref(s)'}")
    for v in fab[:20]:
        print(f"  {v}")
    n = write_worksheet(data)
    print(f"Faithfulness worksheet: {n} evidence item(s) in faithfulness_worksheet.csv — have someone who did not build this verify each in the source system (target >= 95%).")
    return 0 if g1["passed"] and not fab else 1
