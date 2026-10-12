"""
mapping.py — turns a source's raw records into standard records.

A mapping is per entity: {standard_field: field_spec}. A field_spec is one of

  "Amount"                              a path into the raw record
  "status_transitions.paid_at | date"   a path piped through filters
  "Title | startswith('(Former)') | not"
  "'Closed'" / 42 / true                a constant
  {expr: "priority", map: {urgent: high, normal: medium}, default: low}

Paths are dotted, with optional list indexes: `items.data[0].quantity`.

Filters (args are 'quoted strings', numbers, true/false/null, or bare words):
  lower upper strip str int num bool not first len sum empty present
  date              ISO string / epoch seconds / epoch ms  -> YYYY-MM-DD
  cents             divide by 100
  domain            email or URL (or a list of them) -> list of bare domains
  default(x)        x when the value is null/empty
  coalesce(path)    value at another path when this one is null
  eq(x) ne(x) in(a,b..) not_in(a,b..) startswith(x) endswith(x) contains(x)
  multiply(x) divide(x)

The output is then coerced to the standard field's declared type (model.py),
strings are clipped, and anything not declared in the model is dropped.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from . import model


class MappingError(Exception):
    pass


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_INDEX = re.compile(r"^(.*?)\[(\-?\d+)\]$")


def get_path(record, path: str):
    """Dotted lookup that tolerates missing keys (returns None). An exact key
    match wins first, so raw keys that themselves contain dots still work."""
    if record is None or path in ("", None):
        return record
    if isinstance(record, dict) and path in record:
        return record[path]
    current = record
    for part in path.split("."):
        if current is None:
            return None
        index = None
        match = _INDEX.match(part)
        if match:
            part, index = match.group(1), int(match.group(2))
        if part:
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        if index is not None:
            if not isinstance(current, list) or not (-len(current) <= index < len(current)):
                return None
            current = current[index]
    return current


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

def to_date(value) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        number = float(value)
        if number > 1e11:  # epoch milliseconds
            number /= 1000
        return datetime.fromtimestamp(number, tz=timezone.utc).strftime("%Y-%m-%d")
    text = str(value).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def to_num(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    try:
        text = str(value).replace(",", "").replace("$", "").strip()
        number = float(text)
        return int(number) if number.is_integer() else number
    except ValueError:
        return None


def to_bool(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in ("true", "t", "yes", "y", "1"):
        return True
    if text in ("false", "f", "no", "n", "0", ""):
        return False
    return None


def to_domains(value) -> list[str]:
    values = value if isinstance(value, list) else [value]
    out = []
    for item in values:
        if not item:
            continue
        text = str(item).strip().lower()
        if "@" in text:
            text = text.split("@", 1)[1]
        text = re.sub(r"^[a-z]+://", "", text)
        text = text.split("/", 1)[0].split(":", 1)[0]
        if text.startswith("www."):
            text = text[4:]
        if "." in text and text not in out:
            out.append(text)
    return out


# ---------------------------------------------------------------------------
# Expression parsing
# ---------------------------------------------------------------------------

def _split_outside_quotes(text: str, sep: str) -> list[str]:
    parts, buf, quote, depth = [], [], None, 0
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf).strip())
    return parts


def _literal(token: str):
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return True, token[1:-1]
    lowered = token.lower()
    if lowered in ("true", "false"):
        return True, lowered == "true"
    if lowered in ("null", "none"):
        return True, None
    if re.fullmatch(r"-?\d+(\.\d+)?", token):
        return True, float(token) if "." in token else int(token)
    return False, token


def _parse_args(raw: str) -> list:
    if not raw.strip():
        return []
    return [_literal(a)[1] for a in _split_outside_quotes(raw, ",")]


_FILTER_CALL = re.compile(r"^([a-z_]+)(?:\((.*)\))?$", re.DOTALL)


def _apply_filter(name: str, args: list, value, record):
    if name == "lower":
        return value.lower() if isinstance(value, str) else value
    if name == "upper":
        return value.upper() if isinstance(value, str) else value
    if name == "strip":
        return value.strip() if isinstance(value, str) else value
    if name == "str":
        return None if value is None else str(value)
    if name == "int":
        number = to_num(value)
        return None if number is None else int(number)
    if name == "num":
        return to_num(value)
    if name == "bool":
        return to_bool(value)
    if name == "not":
        as_bool = to_bool(value)
        return None if as_bool is None else not as_bool
    if name == "date":
        return to_date(value)
    if name == "cents":
        number = to_num(value)
        return None if number is None else number / 100
    if name == "domain":
        return to_domains(value)
    if name == "empty":
        return value in (None, "", [], {})
    if name == "present":
        return value not in (None, "", [], {})
    if name == "first":
        return value[0] if isinstance(value, list) and value else (None if isinstance(value, list) else value)
    if name == "len":
        return len(value) if isinstance(value, (list, str, dict)) else 0
    if name == "sum":
        return sum(to_num(v) or 0 for v in value) if isinstance(value, list) else to_num(value)
    if name == "default":
        return args[0] if value in (None, "", []) else value
    if name == "coalesce":
        return get_path(record, str(args[0])) if value in (None, "") else value
    if name == "eq":
        return _loose_eq(value, args[0])
    if name == "ne":
        return not _loose_eq(value, args[0])
    if name == "in":
        return any(_loose_eq(value, a) for a in args)
    if name == "not_in":
        return not any(_loose_eq(value, a) for a in args)
    if name == "startswith":
        return isinstance(value, str) and value.strip().lower().startswith(str(args[0]).lower())
    if name == "endswith":
        return isinstance(value, str) and value.strip().lower().endswith(str(args[0]).lower())
    if name == "contains":
        if isinstance(value, list):
            return any(_loose_eq(v, args[0]) for v in value)
        return isinstance(value, str) and str(args[0]).lower() in value.lower()
    if name == "multiply":
        number = to_num(value)
        return None if number is None else number * to_num(args[0])
    if name == "divide":
        number, divisor = to_num(value), to_num(args[0])
        return None if number is None or not divisor else number / divisor
    raise MappingError(f"unknown filter '{name}'")


def _loose_eq(a, b) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    if isinstance(a, bool) or isinstance(b, bool):
        return to_bool(a) == to_bool(b)
    na, nb = to_num(a), to_num(b)
    if na is not None and nb is not None and not isinstance(a, str) and not isinstance(b, str):
        return na == nb
    return str(a).strip().lower() == str(b).strip().lower() if a is not None and b is not None else a is b


def evaluate(expr, record):
    """Evaluate one field spec against a raw record."""
    if isinstance(expr, dict):
        value = evaluate(expr.get("expr", expr.get("path")), record)
        if "map" in expr:
            table = {str(k).lower(): v for k, v in (expr["map"] or {}).items()}
            key = str(value).lower() if value is not None else None
            value = table.get(key, expr.get("default", value if expr.get("passthrough") else None))
        elif value in (None, "") and "default" in expr:
            value = expr["default"]
        return value
    if not isinstance(expr, str):
        return expr  # a YAML constant (number / bool / null)
    parts = _split_outside_quotes(expr, "|")
    is_literal, head = _literal(parts[0])
    value = head if is_literal else get_path(record, head)
    for part in parts[1:]:
        match = _FILTER_CALL.match(part.strip())
        if not match:
            raise MappingError(f"cannot parse filter '{part}' in '{expr}'")
        value = _apply_filter(match.group(1), _parse_args(match.group(2) or ""), value, record)
    return value


def validate_expr(expr) -> list[str]:
    """Static check used by `validate-config`: unknown filters / bad syntax."""
    errors = []
    if isinstance(expr, dict):
        inner = expr.get("expr", expr.get("path"))
        return validate_expr(inner) if inner is not None else ["mapping dict needs 'expr'"]
    if not isinstance(expr, str):
        return errors
    for part in _split_outside_quotes(expr, "|")[1:]:
        match = _FILTER_CALL.match(part.strip())
        if not match:
            errors.append(f"cannot parse filter '{part}' in '{expr}'")
            continue
        try:
            _apply_filter(match.group(1), _parse_args(match.group(2) or "") or [None], None, {})
        except MappingError as exc:
            errors.append(str(exc))
        except Exception:  # noqa: BLE001 - a filter choking on None is fine statically
            pass
    return errors


# ---------------------------------------------------------------------------
# Coercion + record mapping
# ---------------------------------------------------------------------------

def coerce(value, ftype: str):
    if ftype == "date":
        return to_date(value)
    if ftype == "num":
        return to_num(value)
    if ftype == "bool":
        return to_bool(value)
    if ftype == "list":
        if value in (None, ""):
            return []
        return value if isinstance(value, list) else [value]
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return None  # a nested object is never a valid standard string
    text = str(value).strip()
    return text[: model.MAX_STRING_LENGTH] if text else None


def map_record(raw: dict, entity: str, fields: dict, source: str) -> dict | None:
    """One raw record -> one standard record, or None when it has no id."""
    record = {"_source": source, "_entity": entity}
    for std_field, spec in fields.items():
        ftype = model.field_type(entity, std_field)
        if ftype is None:
            continue  # validate-config reports these; at run time just drop
        record[std_field] = coerce(evaluate(spec, raw), ftype)
    if "domain" in record:
        record["domain"] = to_domains(record["domain"])
    if record.get("id") in (None, ""):
        return None
    record["ref"] = model.make_ref(source, entity, record["id"])
    return record


def unknown_fields(entity: str, fields: dict) -> list[str]:
    return [f for f in fields if model.field_type(entity, f) is None]
