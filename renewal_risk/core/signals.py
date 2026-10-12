"""
signals.py — deterministic risk signals over standard records, driven by
signals.yaml. No model call, no cost.

Two uses:
  1. the free prescreen (which renewals deserve a full, paid assessment), and
  2. "candidate signals" handed to each Claude specialist alongside the records,
     so the model verifies and explains computed facts instead of re-deriving
     arithmetic.

Rule types (all thresholds are inclusive; `direction: below` flips them):

  count             how many records match `where` (optionally within window_days)
  ratio_vs_baseline matching records in the last window vs the average of earlier windows
  days_since        days since the latest date on matching records
  pct_change        current value vs a baseline value (can come from another entity)
  window_change     sum of a field in the last window vs the window before it
  ratio             numerator aggregate / denominator aggregate
  exists            first matching `cases` entry decides the severity
  date_diff         days between two date fields on a record (worst record wins)
  absent            a `trigger` record exists but no `expected` record does
  (pct_change with current.per_record + baseline.same_record: worst record wins)

Where clauses: {field: value} | {field: [a, b]} | {field: {op: value}} with ops
eq ne in not_in lt lte gt gte null within_days within_next_days before_as_of. "$as_of" is the as-of date.
Pick: first | earliest:<field> | latest:<field> | max:<field> | min:<field>
Aggregates: sum | count | latest:<field> (value of that record's `field`) | avg
"""
from __future__ import annotations

import string
from datetime import date, datetime, timedelta

from . import model
from .mapping import _loose_eq, to_num

SEVERITIES = ("low", "medium", "high")
RULE_TYPES = ("count", "ratio_vs_baseline", "days_since", "pct_change", "window_change", "ratio", "exists", "date_diff", "absent")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _d(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _resolve(value, as_of: str):
    return as_of if value == "$as_of" else value


def _condition(record_value, cond, as_of: str) -> bool:
    if isinstance(cond, list):
        return any(_loose_eq(record_value, _resolve(c, as_of)) for c in cond)
    if not isinstance(cond, dict):
        return _loose_eq(record_value, _resolve(cond, as_of))
    for op, raw in cond.items():
        target = _resolve(raw, as_of)
        if op == "eq" and not _loose_eq(record_value, target):
            return False
        if op == "ne" and _loose_eq(record_value, target):
            return False
        if op == "in" and not any(_loose_eq(record_value, t) for t in target):
            return False
        if op == "not_in" and any(_loose_eq(record_value, t) for t in target):
            return False
        if op == "null" and (record_value in (None, "")) != bool(target):
            return False
        if op in ("lt", "lte", "gt", "gte"):
            if record_value in (None, ""):
                return False
            a, b = (to_num(record_value), to_num(target))
            if a is None or b is None:  # compare as dates / strings
                a, b = str(record_value)[:10], str(target)[:10]
            if (op == "lt" and not a < b) or (op == "lte" and not a <= b) or (op == "gt" and not a > b) or (op == "gte" and not a >= b):
                return False
        if op == "within_days":
            rd, ad = _d(record_value), _d(as_of)
            if rd is None or ad is None or not (ad - timedelta(days=int(target)) <= rd <= ad):
                return False
        if op == "within_next_days":
            rd, ad = _d(record_value), _d(as_of)
            if rd is None or ad is None or not (ad <= rd <= ad + timedelta(days=int(target))):
                return False
        if op == "before_as_of":
            rd = _d(record_value)
            if rd is None or (rd < _d(as_of)) != bool(target):
                return False
    return True


def matches(record: dict, where: dict | None, as_of: str) -> bool:
    return all(_condition(record.get(f), c, as_of) for f, c in (where or {}).items())


def select(records: dict[str, list[dict]], spec: dict, as_of: str) -> list[dict]:
    entities = spec.get("entity") or spec.get("entities")
    entities = entities if isinstance(entities, list) else [entities]
    out = []
    for entity in entities:
        out.extend(r for r in records.get(entity, []) if matches(r, spec.get("where"), as_of))
    return out


def pick(rows: list[dict], how: str | None) -> dict | None:
    if not rows:
        return None
    how = how or "first"
    if ":" in how:
        mode, fld = how.split(":", 1)
        candidates = [r for r in rows if r.get(fld) not in (None, "")]
        if not candidates:
            return None
        key = (lambda r: to_num(r[fld]) if to_num(r[fld]) is not None and mode in ("max", "min") else str(r[fld]))
        if mode in ("earliest", "min"):
            return min(candidates, key=key)
        return max(candidates, key=key)
    return rows[0]


def aggregate(rows: list[dict], fld: str | None, how: str | None):
    how = how or "sum"
    if how == "count":
        return len(rows)
    if how.startswith(("latest:", "earliest:", "max:", "min:", "first")):
        chosen = pick(rows, how)
        return to_num(chosen.get(fld)) if chosen else None
    values = [to_num(r.get(fld)) for r in rows if to_num(r.get(fld)) is not None]
    if not values:
        return None
    return sum(values) / len(values) if how == "avg" else sum(values)


def severity_for(value, thresholds: dict, direction: str = "above") -> str | None:
    if value is None:
        return None
    for level in reversed(SEVERITIES):
        if level not in thresholds:
            continue
        limit = to_num(thresholds[level])
        if limit is None:
            continue
        if (direction == "below" and value <= limit) or (direction != "below" and value >= limit):
            return level
    return None


class _SafeFormatter(string.Formatter):
    def format_field(self, value, format_spec):
        if value is None:
            return "n/a"
        try:
            return super().format_field(value, format_spec)
        except (ValueError, TypeError):
            return str(value)

    def get_value(self, key, args, kwargs):
        return kwargs.get(key) if isinstance(key, str) else super().get_value(key, args, kwargs)


def render(template: str | None, fallback: str, **values) -> str:
    if not template:
        return fallback
    try:
        return _SafeFormatter().format(template, **values)
    except Exception:  # noqa: BLE001 - a bad template must never break a run
        return fallback


# ---------------------------------------------------------------------------
# Rule evaluation — each returns (severity|None, metric, value_text, evidence_record|None, gap|None)
# ---------------------------------------------------------------------------

def _window(rows, date_field, as_of, start_days, end_days):
    ad = _d(as_of)
    lo, hi = ad - timedelta(days=start_days), ad - timedelta(days=end_days)
    return [r for r in rows if _d(r.get(date_field)) and lo < _d(r.get(date_field)) <= hi]


def _rule_count(sig, records, as_of):
    rows = select(records, sig, as_of)
    if sig.get("window_days"):
        rows = _window(rows, sig.get("date_field", "created_at"), as_of, int(sig["window_days"]), 0)
    count = len(rows)
    sev = severity_for(count, sig.get("thresholds", {}), sig.get("direction", "above"))
    evidence = pick(rows, f"latest:{sig.get('date_field', 'created_at')}") or (rows[0] if rows else None)
    text = render(sig.get("value_template"), f"{count} matching {sig.get('entity')} record(s)", count=count, window_days=sig.get("window_days"))
    return sev, count, text, evidence, None


def _rule_ratio_vs_baseline(sig, records, as_of):
    rows = select(records, sig, as_of)
    field_ = sig.get("date_field", "created_at")
    window = int(sig.get("window_days", 90))
    periods = int(sig.get("baseline_windows", 3))
    recent = _window(rows, field_, as_of, window, 0)
    earlier = [len(_window(rows, field_, as_of, window * (i + 1), window * i)) for i in range(1, periods + 1)]
    baseline = sum(earlier) / periods if periods else 0
    if len(recent) < int(sig.get("min_count", 2)):
        return None, 0, f"{len(recent)} in the last {window} days", None, None
    ratio = len(recent) / baseline if baseline > 0 else float(len(recent))
    sev = severity_for(ratio, sig.get("thresholds", {}))
    evidence = pick(recent, f"latest:{field_}")
    text = render(sig.get("value_template"), f"{len(recent)} in the last {window} days vs a baseline of {baseline:.1f} ({ratio:.1f}x)",
                  recent=len(recent), baseline=baseline, ratio=ratio, window_days=window)
    return sev, round(ratio, 2), text, evidence, None


def _rule_days_since(sig, records, as_of):
    rows = select(records, sig, as_of)
    field_ = sig.get("date_field", "created_at")
    dated = [r for r in rows if _d(r.get(field_)) and _d(r.get(field_)) <= _d(as_of)]
    if not dated:
        return None, None, "", None, render(sig.get("gap_template"), f"{sig['name']} not evaluated — no dated {sig.get('entity') or sig.get('entities')} records")
    latest = pick(dated, f"latest:{field_}")
    days = (_d(as_of) - _d(latest[field_])).days
    sev = severity_for(days, sig.get("thresholds", {}))
    text = render(sig.get("value_template"), f"{days} days since the last record ({latest[field_]})", days=days, last_date=latest[field_])
    return sev, days, text, latest, None


def _pct_metric(sig, current, baseline):
    change = (current - baseline) / baseline * 100
    return change, (-change if sig.get("direction", "decrease") == "decrease" else change)


def _rule_pct_change(sig, records, as_of):
    cur_spec, base_spec = sig.get("current", {}), sig.get("baseline", {})
    rows = select(records, cur_spec, as_of)
    if base_spec.get("same_record") and cur_spec.get("per_record"):
        # Worst record wins, e.g. the most over-budget project.
        scored = []
        for row in rows:
            current, baseline = to_num(row.get(cur_spec.get("field"))), to_num(row.get(base_spec.get("field")))
            if current is not None and baseline:
                change, metric = _pct_metric(sig, current, baseline)
                scored.append((metric, change, current, baseline, row))
        if not scored:
            return None, None, "", None, render(sig.get("gap_template"), f"{sig['name']} not evaluated — no {cur_spec.get('entity')} with both {cur_spec.get('field')} and {base_spec.get('field')}")
        metric, change, current, baseline, row = max(scored, key=lambda t: t[0])
        sev = severity_for(metric, sig.get("thresholds", {}))
        text = render(sig.get("value_template"), f"{cur_spec.get('field')} {current:,.0f} vs {base_spec.get('field')} {baseline:,.0f} ({change:+.0f}%)",
                      current=current, baseline=baseline, pct=abs(change), change=change, **_fields(row))
        return sev, round(metric, 1), text, row, None
    cur_row = pick(rows, cur_spec.get("pick"))
    if cur_row is None:
        return None, None, "", None, render(sig.get("gap_template"), f"{sig['name']} not evaluated — no {cur_spec.get('entity')} record to compare")
    current = to_num(cur_row.get(cur_spec.get("field")))
    if base_spec.get("same_record"):
        baseline = to_num(cur_row.get(base_spec.get("field")))
    else:
        base_row = pick(select(records, base_spec, as_of), base_spec.get("pick"))
        baseline = to_num(base_row.get(base_spec.get("field"))) if base_row else None
    if current is None or not baseline:
        return None, None, "", cur_row, render(sig.get("gap_template"), f"{sig['name']} not evaluated — missing {cur_spec.get('field')} or {base_spec.get('field')}")
    change, metric = _pct_metric(sig, current, baseline)
    sev = severity_for(metric, sig.get("thresholds", {}))
    text = render(sig.get("value_template"), f"{cur_spec.get('field')} {current:,.0f} vs {base_spec.get('field')} {baseline:,.0f} ({change:+.0f}%)",
                  current=current, baseline=baseline, pct=abs(change), change=change)
    return sev, round(metric, 1), text, cur_row, None


def _fields(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def _rule_date_diff(sig, records, as_of):
    """Days from `start_field` to `end_field` on each matching record; the worst wins
    (e.g. forecast_end - planned_end = schedule slip)."""
    rows = select(records, sig, as_of)
    scored = []
    for row in rows:
        start, end = _d(row.get(sig["start_field"])), _d(row.get(sig["end_field"]))
        if start and end:
            scored.append(((end - start).days, row))
    if not scored:
        return None, None, "", None, render(sig.get("gap_template"), f"{sig['name']} not evaluated — no {sig.get('entity')} with both {sig['start_field']} and {sig['end_field']}")
    days, row = max(scored, key=lambda t: t[0])
    sev = severity_for(days, sig.get("thresholds", {}))
    text = render(sig.get("value_template"), f"{days} days between {sig['start_field']} and {sig['end_field']}", days=days, **_fields(row))
    return sev, days, text, row, None


def _rule_absent(sig, records, as_of):
    """Fires when a `trigger` record exists but no `expected` record does
    (e.g. a SOW ending soon with no open renewal in the pipeline)."""
    trigger_rows = select(records, sig["trigger"], as_of)
    if not trigger_rows:
        return None, 0, "", None, None
    if select(records, sig["expected"], as_of):
        return None, 0, "", None, None
    row = pick(trigger_rows, sig["trigger"].get("pick"))
    text = render(sig.get("value_template"), f"{sig['trigger'].get('entity')} {row.get('id')} has no matching {sig['expected'].get('entity')}", **_fields(row))
    return sig.get("severity", "medium"), 1, text, row, None


def _rule_window_change(sig, records, as_of):
    rows = select(records, sig, as_of)
    field_, date_field = sig.get("field", "value"), sig.get("date_field", "period_start")
    window = int(sig.get("window_days", 30))
    recent_rows = _window(rows, date_field, as_of, window, 0)
    prior_rows = _window(rows, date_field, as_of, window * 2, window)
    recent, prior = aggregate(recent_rows, field_, sig.get("agg", "sum")), aggregate(prior_rows, field_, sig.get("agg", "sum"))
    if recent is None or not prior:
        return None, None, "", None, render(sig.get("gap_template"), f"{sig['name']} not evaluated — not enough {sig.get('entity')} history for two {window}-day windows")
    change = (recent - prior) / prior * 100
    metric = -change if sig.get("direction", "decrease") == "decrease" else change
    sev = severity_for(metric, sig.get("thresholds", {}))
    evidence = pick(recent_rows, f"latest:{date_field}")
    text = render(sig.get("value_template"), f"{field_} {recent:,.0f} in the last {window} days vs {prior:,.0f} before ({change:+.0f}%)",
                  recent=recent, prior=prior, pct=abs(change), change=change, window_days=window)
    return sev, round(metric, 1), text, evidence, None


def _rule_ratio(sig, records, as_of):
    num_spec, den_spec = sig.get("numerator", {}), sig.get("denominator", {})
    num_rows, den_rows = select(records, num_spec, as_of), select(records, den_spec, as_of)
    numerator = aggregate(num_rows, num_spec.get("field"), num_spec.get("agg"))
    denominator = aggregate(den_rows, den_spec.get("field"), den_spec.get("agg"))
    if numerator is None or not denominator:
        return None, None, "", None, render(sig.get("gap_template"), f"{sig['name']} not evaluated — missing {num_spec.get('entity')} or {den_spec.get('entity')} data")
    ratio = numerator / denominator
    sev = severity_for(ratio, sig.get("thresholds", {}), sig.get("direction", "below"))
    evidence = pick(num_rows, num_spec.get("agg") if ":" in str(num_spec.get("agg")) else None)
    text = render(sig.get("value_template"), f"{numerator:,.0f} / {denominator:,.0f} = {ratio:.0%}", numerator=numerator, denominator=denominator, ratio=ratio)
    return sev, round(ratio, 3), text, evidence, None


def _rule_exists(sig, records, as_of):
    for case in sig.get("cases", []):
        rows = select(records, {"entity": sig.get("entity"), "entities": sig.get("entities"), "where": {**(sig.get("where") or {}), **(case.get("where") or {})}}, as_of)
        if rows:
            row = pick(rows, sig.get("pick"))
            text = render(case.get("value_template") or sig.get("value_template"), f"{len(rows)} matching record(s)", count=len(rows), record=row, **{k: v for k, v in row.items() if not k.startswith("_")})
            return case.get("severity", "medium"), len(rows), text, row, None
    return None, 0, "", None, None


_RULES = {
    "count": _rule_count, "ratio_vs_baseline": _rule_ratio_vs_baseline, "days_since": _rule_days_since,
    "pct_change": _rule_pct_change, "window_change": _rule_window_change, "ratio": _rule_ratio, "exists": _rule_exists,
    "date_diff": _rule_date_diff, "absent": _rule_absent,
}


def required_entities(sig: dict) -> set[str]:
    names: set[str] = set()
    for spec in (sig, sig.get("current"), sig.get("baseline"), sig.get("numerator"), sig.get("denominator"), sig.get("trigger"), sig.get("expected")):
        if not isinstance(spec, dict):
            continue
        for key in ("entity", "entities"):
            value = spec.get(key)
            if isinstance(value, list):
                names.update(value)
            elif value:
                names.add(value)
    return names


def evaluate(signals: list[dict], records: dict[str, list[dict]], as_of: str, available: set[str]) -> dict[str, dict]:
    """-> {specialist: {"signals": [...], "gaps": [...], "evidence_refs": [...]}}"""
    out: dict[str, dict] = {}
    for sig in signals:
        bucket = out.setdefault(sig.get("specialist", "general"), {"signals": [], "gaps": [], "evidence_refs": []})
        needed = required_entities(sig)
        missing = sorted(needed - available)
        # Partial availability is fine for multi-entity rules (activity from
        # one source, meetings from another); only skip when nothing is there.
        if missing and (needed - set(missing)) == set():
            bucket["gaps"].append(f"{sig['name']} not evaluated — no source provides {', '.join(missing)}")
            continue
        rule = _RULES.get(sig.get("rule"))
        if rule is None:
            bucket["gaps"].append(f"{sig['name']}: unknown rule type '{sig.get('rule')}'")
            continue
        try:
            severity, metric, text, evidence, gap = rule(sig, records, as_of)
        except Exception as exc:  # noqa: BLE001 - one misconfigured signal must not sink the run
            bucket["gaps"].append(f"{sig['name']} failed: {type(exc).__name__}: {exc}")
            continue
        if gap:
            bucket["gaps"].append(gap)
        if severity:
            ref = evidence.get("ref") if evidence else None
            if ref is None:
                bucket["gaps"].append(f"{sig['name']} fired but had no record to cite — dropped")
                continue
            bucket["signals"].append({"name": sig["name"], "severity": severity, "value": text, "evidence_ref": ref, "metric": metric})
            bucket["evidence_refs"].append(ref)
    return out


def validate(signals: list[dict]) -> list[str]:
    problems = []
    for sig in signals:
        name = sig.get("name", "<unnamed>")
        if sig.get("rule") not in RULE_TYPES:
            problems.append(f"signal '{name}': rule must be one of {', '.join(RULE_TYPES)}")
        for entity in required_entities(sig):
            if entity not in model.ENTITIES:
                problems.append(f"signal '{name}': unknown entity '{entity}'")
        for level in (sig.get("thresholds") or {}):
            if level not in SEVERITIES:
                problems.append(f"signal '{name}': threshold level '{level}' must be low/medium/high")
    return problems
