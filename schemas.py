"""
schemas.py — the contract everything else depends on.

Two JSON Schemas:
  EVIDENCE_PACKET_SCHEMA  — what a specialist (Commercial / Relationship) emits.
  RISK_ASSESSMENT_SCHEMA  — what Synthesis emits, built only from packets.

`validate(obj, schema)` uses the `jsonschema` package if installed, and falls
back to a small built-in validator otherwise.
"""
from __future__ import annotations

EVIDENCE_PACKET_SCHEMA = {
    "type": "object",
    "required": ["specialist", "account_id", "as_of", "signals", "gaps", "evidence_refs", "verbatim_excluded"],
    "properties": {
        "specialist": {"type": "string", "enum": ["commercial", "relationship"]},
        "account_id": {"type": "string"},
        "as_of": {"type": "string"},
        "signals": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "severity", "value", "evidence_ref"],
                "properties": {
                    "name": {"type": "string"},
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "value": {"type": "string"},
                    "evidence_ref": {"type": "string"},
                },
            },
        },
        "gaps": {"type": "array", "items": {"type": "string"}},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "verbatim_excluded": {"type": "boolean", "enum": [True]},
    },
}

RISK_ASSESSMENT_SCHEMA = {
    "type": "object",
    "required": ["account_id", "score", "band", "drivers", "citations", "abstained", "rationale"],
    "properties": {
        "account_id": {"type": "string"},
        "score": {"type": "number", "minimum": 0, "maximum": 100},
        "band": {"type": "string", "enum": ["low", "medium", "high"]},
        "drivers": {"type": "array", "items": {"type": "string"}},
        "citations": {"type": "array", "items": {"type": "string"}},
        "abstained": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
}


class SchemaValidationError(Exception):
    pass


try:
    import jsonschema as _jsonschema

    def validate(obj, schema) -> None:
        try:
            _jsonschema.validate(obj, schema)
        except _jsonschema.exceptions.ValidationError as exc:
            raise SchemaValidationError(str(exc)) from exc

except ImportError:  # pragma: no cover - exercised in environments without jsonschema
    def _check_type(value, expected, path):
        types = {
            "object": dict,
            "array": list,
            "string": str,
            "number": (int, float),
            "boolean": bool,
        }
        py_type = types.get(expected)
        if py_type is not None and not isinstance(value, py_type):
            raise SchemaValidationError(f"{path}: expected {expected}, got {type(value).__name__}")

    def _validate_node(value, schema, path="$"):
        expected_type = schema.get("type")
        if expected_type:
            _check_type(value, expected_type, path)

        if "enum" in schema and value not in schema["enum"]:
            raise SchemaValidationError(f"{path}: {value!r} not in allowed values {schema['enum']}")

        if expected_type == "number":
            if "minimum" in schema and value < schema["minimum"]:
                raise SchemaValidationError(f"{path}: {value} below minimum {schema['minimum']}")
            if "maximum" in schema and value > schema["maximum"]:
                raise SchemaValidationError(f"{path}: {value} above maximum {schema['maximum']}")

        if expected_type == "object":
            for key in schema.get("required", []):
                if key not in value:
                    raise SchemaValidationError(f"{path}: missing required field {key!r}")
            for key, subschema in schema.get("properties", {}).items():
                if key in value:
                    _validate_node(value[key], subschema, f"{path}.{key}")

        if expected_type == "array":
            item_schema = schema.get("items")
            if item_schema:
                for idx, item in enumerate(value):
                    _validate_node(item, item_schema, f"{path}[{idx}]")

    def validate(obj, schema) -> None:
        _validate_node(obj, schema)


def is_valid(obj, schema) -> bool:
    try:
        validate(obj, schema)
        return True
    except SchemaValidationError:
        return False
