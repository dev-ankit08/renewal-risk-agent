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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import model
from .mapping import _loose_eq, to_num

SEVERITIES = ("low", "medium", "high")


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

# ---------------------------------------------------------------------------
# The rule table: one entry per rule type drives evaluation, the plain-English
# explanation, validation and the admin UI's signal builder.
# ---------------------------------------------------------------------------

OPERATORS = {  # where-clause operators, as the builder offers them
    "eq": "is", "ne": "is not", "in": "is one of", "not_in": "is not one of",
    "lt": "is less than", "lte": "is at most", "gt": "is more than", "gte": "is at least",
    "null": "is empty", "within_days": "is within the last N days",
    "within_next_days": "is within the next N days", "before_as_of": "is before the assessment date",
}


def _plural(entity) -> str:
    if isinstance(entity, list):
        return " or ".join(_plural(e) for e in entity)
    word = str(entity or "record").replace("_", " ")
    return word[:-1] + "ies" if word.endswith("y") and word[-2:-1] not in "aeiou" else word + "s"  # activities, surveys


_WORDS = {"arr": "ARR", "sow": "SOW", "csat": "CSAT", "nps": "NPS", "rag": "RAG", "crm": "CRM", "erp": "ERP", "opp": "opportunity", "p1": "P1"}


def label(name) -> str:
    """A signal, area or source name for people: opp_stalled -> "Opportunity stalled"."""
    text = " ".join(_WORDS.get(w, w) for w in str(name or "").split("_") if w)
    return text[:1].upper() + text[1:]


def _fname(field) -> str:
    return {"arr": "ARR", "rag": "RAG status"}.get(str(field), str(field or "value").replace("_", " "))


def _agg_name(agg) -> str:
    agg = str(agg or "sum")
    return {"sum": "total", "avg": "average", "count": "count"}.get(agg, agg.split(":", 1)[0])


def _value_text(value) -> str:
    if value == "$as_of":
        return "the assessment date"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        items = [_value_text(v) for v in value]
        return ", ".join(items[:-1]) + " or " + items[-1] if len(items) > 1 else "".join(items)
    return str(value)


_COMPARE = {"lt": "below", "lte": "at most", "gt": "above", "gte": "at least"}
_DATE_COMPARE = {"lt": "before", "lte": "on or before", "gt": "after", "gte": "on or after"}


def _cond_text(field: str, cond) -> str:
    """One condition as a phrase that follows '<records> that ...'."""
    name = _fname(field)
    if field.startswith("is_"):  # yes/no fields read as adjectives: "are open"
        adj = field[3:].replace("_", " ")
        op, value = next(iter(cond.items())) if isinstance(cond, dict) and len(cond) == 1 else ("eq", cond)
        if isinstance(value, bool) and op in ("eq", "ne"):
            positive = value if op == "eq" else not value
            return f"are {'' if positive else 'not '}{adj}" + (" (or not recorded)" if op == "ne" else "")
    if isinstance(cond, list):
        return f"have {name} {_value_text(cond)}"
    if not isinstance(cond, dict):
        return f"have {name} {_value_text(cond)}"
    parts = []
    for op, value in cond.items():
        if op == "null":
            parts.append(f"have {'no' if value else 'a'} {name}")
        elif op in ("within_days", "within_next_days"):
            parts.append(f"have {name} in the {'last' if op == 'within_days' else 'next'} {value} days")
        elif op == "before_as_of":
            parts.append(f"have {name} {'before' if value else 'on or after'} the assessment date")
        elif op in _COMPARE:
            words = _DATE_COMPARE if value == "$as_of" else _COMPARE
            parts.append(f"have {name} {words[op]} {_value_text(value)}")
        elif op in ("ne", "not_in"):
            parts.append(f"do not have {name} {_value_text(value)}")
        else:
            parts.append(f"have {name} {_value_text(value)}")
    return " and ".join(parts)


def _records(spec: dict) -> str:
    where = spec.get("where") or {}
    text = _plural(spec.get("entity") or spec.get("entities"))
    return text + (" that " + " and ".join(_cond_text(f, c) for f, c in where.items()) if where else "")


def _levels(sig: dict, unit: str = "", default_direction: str = "above") -> str:
    th = sig.get("thresholds") or {}
    above = sig.get("direction", default_direction) not in ("below",)
    parts = [f"{lvl} at {th[lvl]}{unit} or {'more' if above else 'less'}" for lvl in SEVERITIES if lvl in th]
    return (" " + ", ".join(parts).capitalize() + ".") if parts else ""


def _window_text(sig: dict) -> str:
    return f" from the last {sig['window_days']} days (by {_fname(sig.get('date_field', 'created_at'))})" if sig.get("window_days") else ""


def _explain_pct(sig: dict) -> str:
    cur, base = sig.get("current", {}), sig.get("baseline", {})
    direction = "drop" if sig.get("direction", "decrease") == "decrease" else "rise"
    if base.get("same_record"):
        head = f"For each of the {_records(cur)}, compares {_fname(cur.get('field'))} with {_fname(base.get('field'))}"
        head += " and takes the worst one" if cur.get("per_record") else ""
    else:
        head = (f"Compares {_fname(cur.get('field'))} on {_records(cur)} with {_fname(base.get('field'))} "
                f"on {_records(base)}")
    return f"{head}, as a % {direction}.{_levels(sig, '%')}"


# Input kinds the builder renders generically: entity, where, number, date_field,
# field, agg, thresholds, severity, group (nested spec), cases (list of where + severity).
_GROUP = ({"key": "entity", "kind": "entity"}, {"key": "where", "kind": "where", "optional": True})
_GROUP_FIELD = _GROUP + ({"key": "field", "kind": "field"},)
_THRESHOLDS = {"key": "thresholds", "kind": "thresholds"}


@dataclass(frozen=True)
class RuleType:
    label: str
    summary: str
    inputs: tuple
    evaluate: Callable
    explain: Callable[[dict], str]


RULES: dict[str, RuleType] = {
    "count": RuleType(
        "Count records", "How many matching records there are, e.g. open escalated tickets.",
        _GROUP + ({"key": "window_days", "kind": "number", "optional": True}, {"key": "date_field", "kind": "date_field", "optional": True}, _THRESHOLDS),
        _rule_count, lambda s: f"Counts {_records(s)}{_window_text(s)}.{_levels(s)}"),
    "days_since": RuleType(
        "Days since last event", "How long ago the latest matching event happened, e.g. last meeting.",
        _GROUP + ({"key": "date_field", "kind": "date_field"}, _THRESHOLDS),
        _rule_days_since, lambda s: f"Measures the days since the most recent {_fname(s.get('date_field'))} on {_records(s)}.{_levels(s, ' days')}"),
    "window_change": RuleType(
        "Change vs previous period", "A total or average in the last N days vs the N days before, e.g. usage drop.",
        _GROUP_FIELD + ({"key": "agg", "kind": "agg"}, {"key": "date_field", "kind": "date_field"}, {"key": "window_days", "kind": "number"}, _THRESHOLDS),
        _rule_window_change,
        lambda s: (f"Compares the {_agg_name(s.get('agg'))} of {_fname(s.get('field'))} on {_records(s)} over the last {s.get('window_days', 30)} days "
                   f"with the {s.get('window_days', 30)} days before, as a % {'drop' if s.get('direction', 'decrease') == 'decrease' else 'rise'}.{_levels(s, '%')}")),
    "ratio_vs_baseline": RuleType(
        "Spike vs the account's normal", "Records in the last N days vs the account's own average, e.g. a ticket spike.",
        _GROUP + ({"key": "date_field", "kind": "date_field"}, {"key": "window_days", "kind": "number"}, {"key": "baseline_windows", "kind": "number"}, _THRESHOLDS),
        _rule_ratio_vs_baseline,
        lambda s: (f"Compares how many {_records(s)} appeared in the last {s.get('window_days', 90)} days with the average of the "
                   f"{s.get('baseline_windows', 3)} earlier periods of the same length (needs at least {s.get('min_count', 2)} recent).{_levels(s, 'x the usual rate')}")),
    "pct_change": RuleType(
        "Percent difference", "One value compared with another, e.g. renewal amount vs current contract value.",
        ({"key": "current", "kind": "group", "inputs": _GROUP_FIELD}, {"key": "baseline", "kind": "group", "inputs": _GROUP_FIELD}, _THRESHOLDS),
        _rule_pct_change, _explain_pct),
    "ratio": RuleType(
        "Ratio of two totals", "One total divided by another, e.g. active users per licensed seat.",
        ({"key": "numerator", "kind": "group", "inputs": _GROUP_FIELD + ({"key": "agg", "kind": "agg"},)},
         {"key": "denominator", "kind": "group", "inputs": _GROUP_FIELD + ({"key": "agg", "kind": "agg"},)}, _THRESHOLDS),
        _rule_ratio,
        lambda s: (f"Divides the {_agg_name(s.get('numerator', {}).get('agg'))} of {_fname(s.get('numerator', {}).get('field'))} on {_records(s.get('numerator', {}))} "
                   f"by the {_agg_name(s.get('denominator', {}).get('agg'))} of {_fname(s.get('denominator', {}).get('field'))} on {_records(s.get('denominator', {}))}."
                   f"{_levels(s, '', 'below')}")),
    "date_diff": RuleType(
        "Days between two dates", "Gap between two dates on a record, e.g. forecast vs planned end.",
        _GROUP + ({"key": "start_field", "kind": "date_field", "required": True}, {"key": "end_field", "kind": "date_field", "required": True}, _THRESHOLDS),
        _rule_date_diff,
        lambda s: f"For each of the {_records(s)}, counts the days from {_fname(s.get('start_field'))} to {_fname(s.get('end_field'))} and takes the largest.{_levels(s, ' days')}"),
    "exists": RuleType(
        "A situation is present", "Fires when a matching record exists, e.g. the primary contact has left.",
        _GROUP + ({"key": "cases", "kind": "cases"},),
        _rule_exists,
        lambda s: "Flags " + "; otherwise ".join(
            f"{c.get('severity', 'medium')} if any " + _records({"entity": s.get("entity") or s.get("entities"), "where": {**(s.get('where') or {}), **(c.get('where') or {})}})
            for c in s.get("cases", [])) + "."),
    "absent": RuleType(
        "Something expected is missing", "Fires when a trigger record exists but no expected record does, e.g. contract ending with no renewal.",
        ({"key": "trigger", "kind": "group", "inputs": _GROUP}, {"key": "expected", "kind": "group", "inputs": _GROUP}, {"key": "severity", "kind": "severity"}),
        _rule_absent, lambda s: f"Flags {s.get('severity', 'medium')} risk when there are {_records(s.get('trigger', {}))} but no {_records(s.get('expected', {}))}."),
}
RULE_TYPES = tuple(RULES)


def explain(sig: dict) -> str:
    """Plain-English 'how it is calculated' for any signal (built-in or custom)."""
    rule = RULES.get(sig.get("rule"))
    if rule is None:
        return "Unknown rule type."
    try:
        return rule.explain(sig).replace("  ", " ")
    except Exception:  # noqa: BLE001 - an incomplete draft still gets a readable answer
        return f"{rule.label}: {rule.summary}"


def builder_metadata() -> dict:
    """Everything the admin UI's signal builder needs, derived from the table above."""
    return {"rules": {name: {"label": r.label, "summary": r.summary, "inputs": r.inputs} for name, r in RULES.items()},
            "operators": OPERATORS, "severities": SEVERITIES}


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
    """-> {specialist: {"signals": [...], "gaps": [...], "evidence_refs": [...], "checked": [names]}}
    `checked` = signals that ran on real data (fired or not); the rest are gaps."""
    out: dict[str, dict] = {}
    for sig in signals:
        bucket = out.setdefault(sig.get("specialist", "general"), {"signals": [], "gaps": [], "evidence_refs": [], "checked": []})
        needed = required_entities(sig)
        missing = sorted(needed - available)
        # Partial availability is fine for multi-entity rules (activity from
        # one source, meetings from another); only skip when nothing is there.
        if missing and (needed - set(missing)) == set():
            bucket["gaps"].append(f"{sig['name']} not evaluated — no source provides {', '.join(missing)}")
            continue
        if needed and not any(records.get(e) for e in needed):  # "0 tickets" for an account with no tickets at all is no evidence
            bucket["gaps"].append(f"{sig['name']} not evaluated — no {_plural(sorted(needed))} for this account")
            continue
        rule = RULES.get(sig.get("rule"))
        if rule is None:
            bucket["gaps"].append(f"{sig['name']}: unknown rule type '{sig.get('rule')}'")
            continue
        try:
            severity, metric, text, evidence, gap = rule.evaluate(sig, records, as_of)
        except Exception as exc:  # noqa: BLE001 - one misconfigured signal must not sink the run
            bucket["gaps"].append(f"{sig['name']} failed: {type(exc).__name__}: {exc}")
            continue
        if gap:
            bucket["gaps"].append(gap)
            continue
        if severity:
            ref = evidence.get("ref") if evidence else None
            if ref is None:
                bucket["gaps"].append(f"{sig['name']} fired but had no record to cite — dropped")
                continue
            bucket["signals"].append({"name": sig["name"], "severity": severity, "value": text, "evidence_ref": ref, "metric": metric})
            bucket["evidence_refs"].append(ref)
        bucket["checked"].append(sig["name"])
    return out


def _check_inputs(name: str, spec: dict, inputs: tuple, entity: str | None, problems: list[str]) -> None:
    """Walk the rule's input table: required keys present, fields exist on their entity."""
    entity = spec.get("entity") or entity
    for inp in inputs:
        key, kind, value = inp["key"], inp["kind"], spec.get(inp["key"])
        if value in (None, "", [], {}):
            if kind == "entity" and spec.get("same_record"):
                continue  # compares two fields of the same record
            if inp.get("required", kind in ("entity", "group", "cases", "field")):
                problems.append(f"signal '{name}': '{key}' is required")
            continue
        if kind == "entity":
            for e in (value if isinstance(value, list) else [value]):
                if e not in model.ENTITIES:
                    problems.append(f"signal '{name}': unknown entity '{e}'")
        elif kind in ("field", "date_field") and entity in model.ENTITIES and model.field_type(entity, value) is None:
            problems.append(f"signal '{name}': '{value}' is not a field of {entity}")
        elif kind == "where":
            _check_where(name, value, entity, problems)
        elif kind == "group":
            _check_inputs(name, value, inp["inputs"], None, problems)
        elif kind == "cases":
            for case in value:
                _check_where(name, case.get("where") or {}, entity, problems)
                if case.get("severity", "medium") not in SEVERITIES:
                    problems.append(f"signal '{name}': case severity must be low, medium or high")


def _check_where(name: str, where: dict, entity, problems: list[str]) -> None:
    if not isinstance(where, dict):
        problems.append(f"signal '{name}': conditions must be field: value pairs")
        return
    for fld, cond in where.items():
        if isinstance(entity, str) and entity in model.ENTITIES and model.field_type(entity, fld) is None:
            problems.append(f"signal '{name}': '{fld}' is not a field of {entity}")
        if isinstance(cond, dict):
            problems.extend(f"signal '{name}': unknown operator '{op}'" for op in cond if op not in OPERATORS)


def validate(signals: list[dict]) -> list[str]:
    problems: list[str] = []
    for sig in signals:
        name = sig.get("name", "<unnamed>")
        rule = RULES.get(sig.get("rule"))
        if rule is None:
            problems.append(f"signal '{name}': rule must be one of {', '.join(RULE_TYPES)}")
            continue
        _check_inputs(name, sig, rule.inputs, None, problems)
        for level, limit in (sig.get("thresholds") or {}).items():
            if level not in SEVERITIES:
                problems.append(f"signal '{name}': threshold level '{level}' must be low/medium/high")
            elif to_num(limit) is None:
                problems.append(f"signal '{name}': threshold '{level}' must be a number")
    return problems
