"""
model_client.py — Anthropic wrapper.

Every role (coordinator / specialist / synthesis) goes through this module,
always via the real Anthropic API through prompts.py's prompt builders,
forcing structured output through tool use. Output is validated against
schemas.py either way.
"""
from __future__ import annotations

import config
import prompts
import schemas


class ModelClientError(Exception):
    pass


def _real_client():
    try:
        import anthropic
    except ImportError as exc:
        raise ModelClientError(
            "anthropic package not installed. Run `pip install -r requirements.txt`."
        ) from exc
    if not config.ANTHROPIC_API_KEY:
        raise ModelClientError(
            "ANTHROPIC_API_KEY not set. Copy .env.example to .env and set it, "
            "or export ANTHROPIC_API_KEY in your shell."
        )
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
    client = _real_client()
    prompt = prompts.coordinator_prompt(account_id)
    return _call_structured(client, config.COORDINATOR_MODEL, prompt, schema=None)


def call_specialist(role: str, account_id: str, as_of: str, records: dict) -> dict:
    client = _real_client()
    prompt = prompts.specialist_prompt(role, account_id, as_of, records)
    packet = _call_structured(client, config.SPECIALIST_MODEL, prompt, schemas.EVIDENCE_PACKET_SCHEMA)

    schemas.validate(packet, schemas.EVIDENCE_PACKET_SCHEMA)
    return packet


def call_synthesis(account_id: str, packets: list[dict]) -> dict:
    client = _real_client()
    prompt = prompts.synthesis_prompt(account_id, packets)
    assessment = _call_structured(client, config.SYNTHESIS_MODEL, prompt, schemas.RISK_ASSESSMENT_SCHEMA)

    schemas.validate(assessment, schemas.RISK_ASSESSMENT_SCHEMA)
    return assessment
