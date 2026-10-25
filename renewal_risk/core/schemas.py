"""
schemas.py — the contract between pipeline stages.

  EVIDENCE_PACKET_SCHEMA   what each specialist emits
  RISK_ASSESSMENT_SCHEMA   what synthesis emits, built only from packets

Both are sent to the API as structured-output schemas, so they stick to the
subset structured outputs supports (every object closed and fully required;
no numeric bounds). Bounds and cross-field rules are checked locally after
the call by `check_assessment` / `check_packet`.

evidence_ref format: "<source>:<entity>/<id>" (see model.make_ref).
"""
from __future__ import annotations

SIGNAL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
        "value": {"type": "string"},
        "evidence_ref": {"type": "string"},
    },
    "required": ["name", "severity", "value", "evidence_ref"],
    "additionalProperties": False,
}

EVIDENCE_PACKET_SCHEMA = {
    "type": "object",
    "properties": {
        "specialist": {"type": "string"},
        "account_id": {"type": "string"},
        "as_of": {"type": "string"},
        "signals": {"type": "array", "items": SIGNAL_SCHEMA},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["specialist", "account_id", "as_of", "signals", "gaps", "evidence_refs"],
    "additionalProperties": False,
}

ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string"},
        "owner_role": {"type": "string"},
        "priority": {"type": "string", "enum": ["low", "medium", "high"]},
        "due_in_days": {"type": "integer"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "playbook_id": {"type": "string"},
        "why": {"type": "string"},
    },
    "required": ["action", "owner_role", "priority", "due_in_days", "evidence_refs", "playbook_id", "why"],
    "additionalProperties": False,
}

RISK_ASSESSMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "account_id": {"type": "string"},
        "score": {"type": "integer"},
        "band": {"type": "string", "enum": ["low", "medium", "high"]},
        "drivers": {"type": "array", "items": {"type": "string"}},
        "citations": {"type": "array", "items": {"type": "string"}},
        "abstained": {"type": "boolean"},
        "rationale": {"type": "string"},
        "recommended_actions": {"type": "array", "items": ACTION_SCHEMA},
    },
    "required": ["account_id", "score", "band", "drivers", "citations", "abstained", "rationale", "recommended_actions"],
    "additionalProperties": False,
}

COORDINATOR_SCHEMA = {
    "type": "object",
    "properties": {
        "specialists": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
    },
    "required": ["specialists", "rationale"],
    "additionalProperties": False,
}

INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["single_account", "bulk_window", "unknown"]},
        "company_name": {"type": "string"},
        "window_start": {"type": "string"},
        "window_end": {"type": "string"},
        "band_filter": {"type": "string", "enum": ["any", "medium_or_high", "high"]},
    },
    "required": ["intent", "company_name", "window_start", "window_end", "band_filter"],
    "additionalProperties": False,
}


class SchemaValidationError(Exception):
    pass


try:
    import jsonschema as _jsonschema

    def validate(obj, schema) -> None:
        try:
            _jsonschema.validate(obj, schema)
        except _jsonschema.exceptions.ValidationError as exc:
            raise SchemaValidationError(exc.message) from exc

except ImportError:  # pragma: no cover
    def validate(obj, schema, path="$") -> None:
        t = schema.get("type")
        types = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}
        if t and not isinstance(obj, types[t]):
            raise SchemaValidationError(f"{path}: expected {t}")
        if "enum" in schema and obj not in schema["enum"]:
            raise SchemaValidationError(f"{path}: {obj!r} not allowed")
        if t == "object":
            for key in schema.get("required", []):
                if key not in obj:
                    raise SchemaValidationError(f"{path}: missing {key!r}")
            for key, sub in schema.get("properties", {}).items():
                if key in obj:
                    validate(obj[key], sub, f"{path}.{key}")
        if t == "array" and schema.get("items"):
            for i, item in enumerate(obj):
                validate(item, schema["items"], f"{path}[{i}]")




def check_assessment(assessment: dict) -> None:
    validate(assessment, RISK_ASSESSMENT_SCHEMA)
    if not 0 <= assessment["score"] <= 100:
        raise SchemaValidationError(f"score {assessment['score']} outside 0-100")


def check_packet(packet: dict) -> None:
    validate(packet, EVIDENCE_PACKET_SCHEMA)
