"""
model_client.py — Anthropic wrapper + stub swap seam.

Every role (coordinator / specialist / synthesis) goes through this module.
When config.USE_STUB_MODEL is True, calls dispatch to stub_model.py and no
network call or API key is needed. When False, they call the real Anthropic
API via prompts.py's prompt builders and force structured output through tool
use. USE_STUB_MODEL is independent of config.USE_STUB, which controls
Salesforce (Mock vs MCP) — see config.py.
Output is validated against schemas.py either way — the stub isn't exempt
from the contract it's standing in for.
"""
from __future__ import annotations

import config
import prompts
import schemas
import stub_model


class ModelClientError(Exception):
    pass


def _real_client():
    try:
        import anthropic
    except ImportError as exc:
        raise ModelClientError(
            "anthropic package not installed. Run `pip install -r requirements.txt`, "
            "or use --dry-run which needs no API access."
        ) from exc
    if not config.ANTHROPIC_API_KEY:
        raise ModelClientError("ANTHROPIC_API_KEY not set. Use --dry-run or export the key.")
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _call_structured(client, model: str, prompt: str, schema: dict | None) -> dict:
    """Force the model to return one structured result via tool use."""
    tool = {
        "name": "emit_result",
        "description": "Return the structured result for this call.",
        "input_schema": schema or {"type": "object"},
    }
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        tools=[tool],
        tool_choice={"type": "tool", "name": "emit_result"},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    raise ModelClientError("model did not return a tool_use block")


def call_coordinator(account_id: str) -> dict:
    if config.USE_STUB_MODEL:
        return stub_model.generate_coordinator(account_id)
    client = _real_client()
    prompt = prompts.coordinator_prompt(account_id)
    return _call_structured(client, config.COORDINATOR_MODEL, prompt, schema=None)


def call_specialist(role: str, account_id: str, as_of: str, records: dict) -> dict:
    if config.USE_STUB_MODEL:
        if role == "commercial":
            packet = stub_model.generate_commercial_packet(account_id, as_of, records)
        else:
            packet = stub_model.generate_relationship_packet(account_id, as_of, records)
    else:
        client = _real_client()
        prompt = prompts.specialist_prompt(role, account_id, as_of, records)
        packet = _call_structured(client, config.SPECIALIST_MODEL, prompt, schemas.EVIDENCE_PACKET_SCHEMA)

    schemas.validate(packet, schemas.EVIDENCE_PACKET_SCHEMA)
    return packet


def call_synthesis(account_id: str, packets: list[dict]) -> dict:
    if config.USE_STUB_MODEL:
        assessment = stub_model.generate_synthesis(account_id, packets)
    else:
        client = _real_client()
        prompt = prompts.synthesis_prompt(account_id, packets)
        assessment = _call_structured(client, config.SYNTHESIS_MODEL, prompt, schemas.RISK_ASSESSMENT_SCHEMA)

    schemas.validate(assessment, schemas.RISK_ASSESSMENT_SCHEMA)
    return assessment
