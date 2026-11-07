"""
backtest.py — "how often is it right?", measured on renewals that already closed.

Two ways to get the agent's call on a closed renewal:
  live    what the agent actually said at the time — the quick check saved for the
          renewal (Slack lists, the ask page, the nightly scan) at least `lead_days`
          before it closed. Nothing is re-read; the truest measure.
  replay  how it looked `lead_days` before it closed: only records created by then
          are read, and the renewal itself is put back to "open" with its final stage
          and amount removed, so the answer cannot leak in. Works without history.
`auto` uses live when any saved prediction qualifies, replay otherwise.

Outcome of a closed renewal: an uploaded outcomes.csv row (account_id,
close_date, outcome) wins; otherwise the CRM stage decides (agent.yaml
`backtest.lost_stages` / `won_stages`), and a won renewal that came in
`downgrade_pct`% or more below the prior amount or contract ARR is a downgrade.

From the outcomes it suggests (the admin applies, nothing changes on its own):
  * scoring: points per severity, the band cut-offs, and a factor per warning sign
    learned from how often it preceded a loss (pulled towards 1 when history is thin);
  * a medium / high threshold per warning sign, from the value it measured on every
    renewal — fired or not — once there is enough history.
Signals that read a record's *current* state are approximate in replay mode. No AI is called.
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
PRIOR = 2            # pseudo-renewals at the average loss rate, so a sign seen twice cannot swing its factor
MIN_FIRED = 3        # a sign must have fired this often before its factor is learned
MIN_CASES, MIN_EACH = 8, 3   # per-sign thresholds: renewals measured, and at least this many losses and renewals
MAX_CUTS = 50


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


def _case(case: dict, as_of: str, outcome: str | None, fired: list[dict], metrics: dict, enough: bool, **extra) -> dict:
    return {**{k: case.get(k) for k in ("account_id", "name", "close_date", "amount", "stage")}, "as_of": as_of,
            "outcome": outcome, "fired": fired, "metrics": metrics, "enough": enough, **extra}


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
    fired = [{"name": s["name"], "severity": s["severity"], "section": s.get("section")} for b in candidates.values() for s in b["signals"]]
    metrics = {k: v for b in candidates.values() for k, v in b["metrics"].items()}
    return _case(case, as_of, outcome, fired, metrics, coverage["enough"], checked=coverage["checked"], total=coverage["total"])


def _logged(closed: list[dict], predictions_for, lead_days: int, cfg: dict, uploaded: dict) -> list[dict]:
    """The latest saved prediction made at least `lead_days` before each renewal closed."""
    saved = predictions_for([c["renewal_ref"] for c in closed]) if predictions_for else {}
    out = []
    for case in closed:
        cutoff = (date.fromisoformat(case["close_date"][:10]) - timedelta(days=lead_days)).isoformat()
        early = [p for p in saved.get(case["renewal_ref"], []) if p["predicted_on"] <= cutoff]
        if early:
            p = early[-1]
            out.append(_case(case, p["predicted_on"], outcome_of(case, None, cfg, uploaded), p["fired"], p["metrics"], bool(p["enough"]),
                             band_then=p["band"]))  # what it said; `predicted` re-scores with today's settings
    return out


def _predict(case: dict, rules: dict) -> tuple[bool, int]:
    """(flagged as at risk, score) under one rules setting — the same scoring as live answers."""
    score = score_signals(case["fired"], rules)
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


def calibration(cases: list[dict], rules: dict) -> list[dict]:
    """Per band: how many renewals were called that way, and how many were then lost or shrank."""
    rows = {b: [0, 0] for b in ("high", "medium", "low")}
    for c in cases:
        if c["enough"]:
            band = band_for(score_signals(c["fired"], rules), rules["thresholds"])
            rows[band][0] += 1
            rows[band][1] += c["outcome"] in POSITIVE
    return [{"band": b, "renewals": n, "lost": lost, "share": round(lost / n, 3) if n else None} for b, (n, lost) in rows.items()]


def _baseline(cases: list[dict], k: int) -> int:
    """At-risk renewals a naive "biggest deals first" list of the same length would have caught."""
    top = sorted(cases, key=lambda c: -float(c.get("amount") or 0))[:k]
    return sum(c["outcome"] in POSITIVE for c in top)


def _signal_stats(cases: list[dict]) -> dict[str, list[int]]:
    """{sign: [times fired, times a loss followed]}."""
    stats: dict[str, list[int]] = {}
    for c in cases:
        for name in {s["name"] for s in c["fired"]}:
            hit = stats.setdefault(name, [0, 0])
            hit[0] += 1
            hit[1] += c["outcome"] in POSITIVE
    return stats


def _per_signal(cases: list[dict], base_rate: float, approx: set[str]) -> list[dict]:
    rows = []
    for name, (fired, right) in sorted(_signal_stats(cases).items(), key=lambda kv: -kv[1][0]):
        precision = right / fired
        rows.append({"name": name, "fired": fired, "preceded_loss": right, "precision": round(precision, 3),
                     "lift": round(precision / base_rate, 2) if base_rate else None, "approximate": name in approx,
                     "weak": fired >= MIN_FIRED and precision < base_rate})
    return rows


def learn_factors(cases: list[dict], base_rate: float) -> dict[str, float]:
    """Per sign: how much more (or less) often a loss followed than on average, pulled towards 1
    by PRIOR average renewals and clamped to 0.5x-2x, in steps of 0.05."""
    if not base_rate:
        return {}
    out = {}
    for name, (fired, right) in _signal_stats(cases).items():
        if fired >= MIN_FIRED:
            factor = round(min(2.0, max(0.5, (right + PRIOR * base_rate) / (fired + PRIOR) / base_rate)) * 20) / 20
            if factor != 1:
                out[name] = factor
    return out


def _grid_best(cases: list[dict], base: dict) -> tuple[dict, float]:
    best, best_f1 = base, metrics(cases, base)["f1"]
    for h, m, lo, th, tm in product(*_GRID.values()):
        if tm >= th:
            continue
        rules = {**base, "weights": {"high": h, "medium": m, "low": lo}, "thresholds": {"high": th, "medium": tm}}
        f1 = metrics(cases, rules)["f1"]
        if f1 > best_f1 + 1e-9:
            best, best_f1 = rules, f1
    return best, best_f1


def suggest(cases: list[dict], current: dict, factors: dict[str, float] | None = None) -> dict:
    """Best F1 over a small fixed grid, with and without the learned per-sign factors
    (scored on the signals already found — no data is read again). Returns `current` if nothing beats it."""
    best, best_f1 = _grid_best(cases, current)
    if factors:
        learned, f1 = _grid_best(cases, {**current, "signal_factors": {**(current.get("signal_factors") or {}), **factors}})
        if f1 > best_f1 + 1e-9:
            best = learned
    return best


def _cuts(values: list[float]) -> list[float]:
    distinct = sorted(set(values))
    if len(distinct) <= MAX_CUTS:
        return distinct
    return sorted({distinct[round(i * (len(distinct) - 1) / (MAX_CUTS - 1))] for i in range(MAX_CUTS)})


def _flags(points: list[tuple[float, bool]], cut: float, above: bool) -> tuple[int, int]:
    """(losses flagged, renewals flagged) when the sign fires at `cut`."""
    hits = [lost for v, lost in points if (v >= cut if above else v <= cut)]
    return sum(hits), len(hits) - sum(hits)


def suggest_thresholds(cases: list[dict], signal_defs: list[dict]) -> list[dict]:
    """A medium / high threshold per numeric warning sign, from the value it measured on each renewal.
    medium: the cut that best separates losses from renewals (Youden's J = catch rate - false alarm rate);
    high: the most lenient cut whose flags were losses at least twice as often as average (and 60%+)."""
    rows = []
    for sig in signal_defs:
        current = {k: signals.to_num((sig.get("thresholds") or {}).get(k)) for k in ("medium", "high")}
        if all(v is None for v in current.values()):
            continue
        name, above = sig["name"], signals.threshold_direction(sig) != "below"
        points = [(float(c["metrics"][name]), c["outcome"] in POSITIVE) for c in cases if name in (c.get("metrics") or {})]
        losses = sum(lost for _, lost in points)
        row = {"name": name, "direction": "above" if above else "below", "current": current, "measured": len(points), "losses": losses}
        if len(points) < MIN_CASES or losses < MIN_EACH or len(points) - losses < MIN_EACH:
            rows.append({**row, "status": "not_enough"})
            continue
        renewals, base = len(points) - losses, losses / len(points)
        strict_first = sorted(_cuts([v for v, _ in points]), reverse=above)
        medium, best_j = None, 0.0
        for cut in strict_first:  # strictest first, so a tie keeps the one with fewer false alarms
            caught, false = _flags(points, cut, above)
            if caught / losses - false / renewals > best_j + 1e-9:
                medium, best_j = cut, caught / losses - false / renewals
        if medium is None:
            rows.append({**row, "status": "no_signal"})  # no value separates losses from renewals
            continue
        high = None
        for cut in strict_first:
            if (cut < medium) if above else (cut > medium):
                break
            caught, false = _flags(points, cut, above)
            if caught + false >= 2 and caught / (caught + false) >= max(2 * base, 0.6):
                high = cut  # keep going: the most lenient cut that still qualifies wins
        suggested = {"medium": round(medium, 3), "high": round(high, 3) if high is not None else None}
        lowest_now = current["medium"] if current["medium"] is not None else current["high"]
        now = dict(zip(("caught", "false_alarms"), _flags(points, lowest_now, above)))
        then = dict(zip(("caught", "false_alarms"), _flags(points, medium, above)))
        rows.append({**row, "status": "same" if suggested == current else "suggested", "suggested": suggested, "now": now, "then": then})
    return rows


def run(engine, start: str, end: str, lead_days: int | None = None, uploaded: dict | None = None,
        mode: str = "auto", predictions_for=None) -> dict:
    cfg = deep_merge(DEFAULTS, engine.settings.agent.get("backtest") or {})
    lead_days = int(lead_days or cfg["lead_days"])
    rules = rules_config(engine.settings.agent)
    try:
        closed = engine.list_renewals(end, start, closed=True)[:MAX_CASES]
    except ConnectorError as exc:
        return {"error": str(exc)}
    logged = _logged(closed, predictions_for, lead_days, cfg, uploaded or {}) if mode in ("auto", "live") else []
    live_cases = [c for c in logged if c["outcome"]]
    used = "live" if mode == "live" or (mode == "auto" and live_cases) else "replay"
    if used == "live":
        replayed = logged
    else:
        with ThreadPoolExecutor(max_workers=4) as pool:
            replayed = list(pool.map(lambda c: _replay(engine, c, lead_days, cfg, uploaded or {}), closed))
    cases = [c for c in replayed if c["outcome"]]
    for c in cases:
        c["predicted"], c["score"] = _predict(c, rules)
    positives = sum(c["outcome"] in POSITIVE for c in cases)
    base_rate = positives / len(cases) if cases else 0.0
    current = metrics(cases, rules)
    flagged = current["caught"] + current["false_alarms"]
    better = suggest(cases, rules, learn_factors(cases, base_rate))
    approx = {s["name"] for s in engine.settings.signals if approximate(s)} if used == "replay" else set()
    scoring_keys = ("weights", "thresholds", "signal_factors")
    return {
        "mode": used, "live_available": len(live_cases), "closed": len(closed),
        "window": {"start": start, "end": end}, "lead_days": lead_days, "tested": len(cases), "at_risk": positives,
        "metrics": current, "baseline_caught": _baseline(cases, flagged), "calibration": calibration(cases, rules),
        "signals": _per_signal(cases, base_rate, approx),
        "thresholds": suggest_thresholds(cases, engine.settings.signals),
        "rules": {k: rules.get(k) or {} for k in scoring_keys},
        "suggested": ({**{k: better.get(k) or {} for k in scoring_keys}, "metrics": metrics(cases, better)} if better is not rules else None),
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
