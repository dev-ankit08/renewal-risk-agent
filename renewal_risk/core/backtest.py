"""
backtest.py — "how often is it right?", measured on renewals that already closed.

Each closed renewal is replayed as it looked `lead_days` before it closed: only
records created by then are read, and the renewal itself is put back to "open"
with its final stage and amount removed, so the answer cannot leak in. The
enabled signals run on that data and are scored by the same rules as live
answers; the prediction is then compared with what really happened.

Outcome of a closed renewal: an uploaded outcomes.csv row (account_id,
close_date, outcome) wins; otherwise the CRM stage decides (agent.yaml
`backtest.lost_stages` / `won_stages`), and a won renewal that came in
`downgrade_pct`% or more below the prior amount or contract ARR is a downgrade.

Signals that read a record's *current* state (status, stage, open/closed...)
see today's values, not the values back then; they are reported as approximate.
No AI is called; the weight search reuses the signals already found.
"""
from __future__ import annotations

import csv
import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from itertools import product

from . import signals
from .connector import ConnectorError
from .pipeline import band_for, rules_config, score_signals
from .settings import deep_merge

POSITIVE = ("churned", "downgraded")
OUTCOMES = ("renewed",) + POSITIVE
STATEFUL_FIELDS = ("is_open", "status", "stage", "stage_changed_at", "rag", "cancel_scheduled", "is_active", "quantity")
DEFAULTS = {"lead_days": 90, "lost_stages": ["lost", "cancel", "churn"], "won_stages": ["won"], "downgrade_pct": 5}
MAX_CASES = 200
_GRID = {"high": (25, 35, 50), "medium": (10, 15, 25), "low": (0, 5), "t_high": (60, 70), "t_medium": (25, 40)}


def approximate(sig: dict) -> bool:
    """Does the signal read a field that only holds today's value?"""
    text = json.dumps({k: v for k, v in sig.items() if k not in ("description", "value_template", "how_calculated_note")})
    return any(f'"{f}"' in text for f in STATEFUL_FIELDS)


def outcome_of(case: dict, contract_arr: float | None, cfg: dict, uploaded: dict) -> str | None:
    if (case["account_id"], case["close_date"]) in uploaded:
        return uploaded[(case["account_id"], case["close_date"])]
    stage = str(case.get("stage") or "").lower()
    if any(w in stage for w in cfg["lost_stages"]):
        return "churned"
    if not any(w in stage for w in cfg["won_stages"]):
        return None
    before = case.get("prior_amount") or contract_arr
    try:
        shrank = before and float(case["amount"]) < float(before) * (1 - cfg["downgrade_pct"] / 100)
    except (TypeError, ValueError):
        shrank = False
    return "downgraded" if shrank else "renewed"


def _replay(engine, case: dict, lead_days: int, cfg: dict, uploaded: dict) -> dict:
    as_of = (date.fromisoformat(case["close_date"][:10]) - timedelta(days=lead_days)).isoformat()
    needed = set().union(*(signals.required_entities(s) for s in engine.settings.signals)) | {"renewal", "contract"}
    records, _gaps, _used = engine.gather(case["account_id"], as_of, entities=needed & engine.available_entities())
    contract = max((r for r in records.get("contract", []) if r.get("arr")), key=lambda r: str(r.get("start_date") or ""), default=None)
    outcome = outcome_of(case, (contract or {}).get("arr"), cfg, uploaded)
    for r in records.get("renewal", []):  # the deal as it stood then: open, final stage and amount unknown
        if r.get("ref") == case["renewal_ref"]:
            r.update(is_open=True, stage=None, stage_changed_at=None, amount=None)
    candidates = signals.evaluate(engine.settings.signals, records, as_of, engine.available_entities())
    coverage = engine.coverage(candidates)
    fired = [{"name": s["name"], "severity": s["severity"]} for b in candidates.values() for s in b["signals"]]
    return {**{k: case.get(k) for k in ("account_id", "name", "close_date", "amount", "stage")}, "as_of": as_of,
            "outcome": outcome, "fired": fired, "enough": coverage["enough"], "checked": coverage["checked"], "total": coverage["total"]}


def _predict(case: dict, rules: dict) -> tuple[bool, int]:
    """(flagged as at risk, score) under one rules setting — the same scoring as live answers."""
    score = score_signals(case["fired"], rules["weights"])
    return case["enough"] and band_for(score, rules["thresholds"]) in ("medium", "high"), score


def metrics(cases: list[dict], rules: dict) -> dict:
    tp = fp = fn = tn = 0
    for c in cases:
        flagged, _ = _predict(c, rules)
        positive = c["outcome"] in POSITIVE
        tp, fp, fn, tn = tp + (flagged and positive), fp + (flagged and not positive), fn + (positive and not flagged), tn + (not flagged and not positive)
    precision = tp / (tp + fp) if tp + fp else 0.0
    hit_rate = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * hit_rate / (precision + hit_rate) if precision + hit_rate else 0.0
    return {"caught": tp, "missed": fn, "false_alarms": fp, "quiet_correct": tn, "precision": round(precision, 3),
            "hit_rate": round(hit_rate, 3), "f1": round(f1, 3)}


def _baseline(cases: list[dict], k: int) -> int:
    """At-risk renewals a naive "biggest deals first" list of the same length would have caught."""
    top = sorted(cases, key=lambda c: -float(c.get("amount") or 0))[:k]
    return sum(c["outcome"] in POSITIVE for c in top)


def _per_signal(cases: list[dict], base_rate: float, approx: set[str]) -> list[dict]:
    stats: dict[str, list[int]] = {}
    for c in cases:
        for name in {s["name"] for s in c["fired"]}:
            hit = stats.setdefault(name, [0, 0])
            hit[0] += 1
            hit[1] += c["outcome"] in POSITIVE
    rows = []
    for name, (fired, right) in sorted(stats.items(), key=lambda kv: -kv[1][0]):
        precision = right / fired
        rows.append({"name": name, "fired": fired, "preceded_loss": right, "precision": round(precision, 3),
                     "lift": round(precision / base_rate, 2) if base_rate else None, "approximate": name in approx,
                     "weak": fired >= 3 and precision < base_rate})
    return rows


def suggest(cases: list[dict], current: dict) -> dict:
    """Best F1 over a small fixed grid, scored on the signals already found (no data is read again)."""
    best, best_f1 = current, metrics(cases, current)["f1"]
    for h, m, lo, th, tm in product(*_GRID.values()):
        if tm >= th:
            continue
        rules = {**current, "weights": {"high": h, "medium": m, "low": lo}, "thresholds": {"high": th, "medium": tm}}
        f1 = metrics(cases, rules)["f1"]
        if f1 > best_f1 + 1e-9:
            best, best_f1 = rules, f1
    return best


def run(engine, start: str, end: str, lead_days: int | None = None, uploaded: dict | None = None) -> dict:
    cfg = deep_merge(DEFAULTS, engine.settings.agent.get("backtest") or {})
    lead_days = int(lead_days or cfg["lead_days"])
    rules = rules_config(engine.settings.agent)
    try:
        closed = engine.list_renewals(end, start, closed=True)[:MAX_CASES]
    except ConnectorError as exc:
        return {"error": str(exc)}
    with ThreadPoolExecutor(max_workers=4) as pool:
        replayed = list(pool.map(lambda c: _replay(engine, c, lead_days, cfg, uploaded or {}), closed))
    cases = [c for c in replayed if c["outcome"]]
    for c in cases:
        c["predicted"], c["score"] = _predict(c, rules)
    positives = sum(c["outcome"] in POSITIVE for c in cases)
    current = metrics(cases, rules)
    flagged = current["caught"] + current["false_alarms"]
    better = suggest(cases, rules)
    approx = {s["name"] for s in engine.settings.signals if approximate(s)}
    return {
        "window": {"start": start, "end": end}, "lead_days": lead_days, "tested": len(cases), "at_risk": positives,
        "metrics": current, "baseline_caught": _baseline(cases, flagged),
        "signals": _per_signal(cases, positives / len(cases) if cases else 0.0, approx),
        "rules": {k: rules[k] for k in ("weights", "thresholds")},
        "suggested": {"weights": better["weights"], "thresholds": better["thresholds"], "metrics": metrics(cases, better)} if better is not rules else None,
        "cases": cases, "unknown_outcome": [{k: c[k] for k in ("account_id", "name", "close_date", "stage")} for c in replayed if not c["outcome"]],
        "approximate_signals": sorted(approx & {s["name"] for c in cases for s in c["fired"]}),
    }


def read_outcomes(text: str) -> dict:
    """outcomes.csv -> {(account_id, close_date): outcome}; bad rows are ignored."""
    out = {}
    for row in csv.DictReader(io.StringIO(text or "")):
        outcome = str(row.get("outcome") or "").strip().lower()
        if row.get("account_id") and row.get("close_date") and outcome in OUTCOMES:
            out[(row["account_id"].strip(), row["close_date"].strip()[:10])] = outcome
    return out
