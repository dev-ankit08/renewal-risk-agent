"""
guard.py — source-independent safety checks on standard records.

  strip_banned_fields  drop free-text-looking keys from a raw dict. Standard
                       records already cannot carry free text (the model has
                       no such field), so this is the belt to that braces —
                       used on anything raw that is ever shown to an LLM
                       (e.g. `mapping suggest` samples).
  audit_leakage        Gate 0: any observed-date field after as_of means
                       future data leaked into a run. evaluate.py refuses to
                       print accuracy numbers for a run that has any.

Query-level guards (read-only checks, as-of injection) are per platform and
live in each connector (salesforce.block_dml / enforce_as_of, sql.check_read_only).
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import model


class GuardViolation(Exception):
    pass


_BANNED_SUBSTRINGS = ("description", "subject", "body", "comment", "note", "verbatim", "message", "content", "transcript")


def is_banned(field_name: str) -> bool:
    lname = field_name.lower()
    return any(s in lname for s in _BANNED_SUBSTRINGS)


def strip_banned_fields(record: dict) -> tuple[dict, list[str]]:
    """Return (cleaned_record, stripped_field_names). Never mutates the input."""
    cleaned, stripped = {}, []
    for key, value in record.items():
        if is_banned(key):
            stripped.append(key)
        elif isinstance(value, dict):
            cleaned[key], inner = strip_banned_fields(value)
            stripped.extend(f"{key}.{i}" for i in inner)
        else:
            cleaned[key] = value
    return cleaned, stripped


def _parse_date(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def audit_leakage(records: list[dict], as_of_date: str, date_fields=model.OBSERVED_DATE_FIELDS) -> list[dict]:
    """Every record whose observed-date field falls after as_of. Empty = pass."""
    as_of_dt = _parse_date(as_of_date)
    if as_of_dt is None:
        raise GuardViolation(f"invalid as_of_date: {as_of_date!r}")
    violations = []
    for record in records:
        for field in date_fields:
            value = record.get(field)
            record_dt = _parse_date(value)
            if record_dt is not None and record_dt > as_of_dt:
                violations.append({
                    "record": record.get("ref") or record.get("id"),
                    "field": field, "value": value, "as_of": as_of_date,
                })
    return violations
