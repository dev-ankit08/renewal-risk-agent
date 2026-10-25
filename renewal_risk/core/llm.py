"""
llm.py — the one place that talks to the Anthropic API.

Every call asks for structured JSON output (output_config.format) against a
schema from schemas.py, then validates locally and retries once on a schema
miss. (Forced tool_choice, which the first version used, is rejected by the
current Opus/Sonnet models.)

`role` picks the model from agent.yaml: coordinator | specialist | synthesis |
utility (intent parsing, mapping suggestions — uses the specialist model).
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field

from . import schemas

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    pass


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    by_model: dict = field(default_factory=dict)

    def add(self, model: str, input_tokens: int, output_tokens: int, cost: float) -> None:
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost_usd += cost
        self.by_model[model] = self.by_model.get(model, 0) + 1


class AnthropicLLM:
    def __init__(self, llm_settings: dict):
        self.cfg = llm_settings
        self._client = None
        self._lock = threading.Lock()
        self._fallback_enabled = bool(llm_settings.get("refusal_fallback", True))

    @property
    def client(self):
        with self._lock:
            if self._client is None:
                try:
                    import anthropic
                except ImportError as exc:
                    raise LLMError("the anthropic package is not installed (`pip install anthropic`)") from exc
                # Resolves ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / an `ant auth login` profile.
                self._client = anthropic.Anthropic(max_retries=3)
            return self._client

    @property
    def configured(self) -> bool:
        """A usable credential is set (the `.env.example` placeholder `sk-ant-...` is not)."""
        key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or ""
        return len(key) >= 40 and "..." not in key

    def model_for(self, role: str) -> str:
        key = {"coordinator": "coordinator_model", "synthesis": "synthesis_model"}.get(role, "specialist_model")
        return self.cfg[key]

    def _cost(self, model: str, usage) -> float:
        rate = (self.cfg.get("rates") or {}).get(model)
        if not rate:
            return 0.0
        uncached = getattr(usage, "input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        output = getattr(usage, "output_tokens", 0) or 0
        return (uncached * rate["input"] + cache_read * rate["input"] * 0.1
                + cache_write * rate["input"] * 1.25 + output * rate["output"]) / 1_000_000

    def _create(self, model: str, system: str, user: str, schema: dict):
        import anthropic

        kwargs = dict(
            model=model,
            max_tokens=int(self.cfg.get("max_tokens", 16000)),
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.cfg.get("effort", "medium"), "format": {"type": "json_schema", "schema": schema}},
        )
        if self._fallback_enabled:
            kwargs["extra_headers"] = {"anthropic-beta": FALLBACK_BETA}
            kwargs["extra_body"] = {"fallbacks": "default"}
        try:
            return self.client.messages.create(**kwargs)
        except anthropic.BadRequestError as exc:
            # Some deployments (proxies, other platforms) reject the fallback
            # parameter: turn it off for this process and retry plainly.
            if self._fallback_enabled and "fallback" in str(exc).lower():
                self._fallback_enabled = False
                kwargs.pop("extra_headers"), kwargs.pop("extra_body")
                return self.client.messages.create(**kwargs)
            raise LLMError(f"Anthropic API rejected the request: {exc.message}") from exc
        except anthropic.AuthenticationError as exc:
            raise LLMError("Anthropic authentication failed — set ANTHROPIC_API_KEY (see .env.example)") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("Anthropic rate limit hit after retries — lower concurrency or try again shortly") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"cannot reach the Anthropic API: {exc}") from exc

    def structured(self, role: str, system: str, user: str, schema: dict, usage: Usage | None = None, check=None) -> dict:
        model = self.model_for(role)
        last_error = None
        for _attempt in (1, 2):
            response = self._create(model, system, user, schema)
            if usage is not None:
                usage.add(response.model, getattr(response.usage, "input_tokens", 0) or 0,
                          getattr(response.usage, "output_tokens", 0) or 0, self._cost(response.model, response.usage))
            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                raise LLMError(f"the model declined this request ({getattr(details, 'category', None) or 'no category'})")
            if response.stop_reason == "max_tokens":
                last_error = "response hit max_tokens"
                continue
            text = next((b.text for b in response.content if b.type == "text"), None)
            if text is None:
                last_error = "no text block in response"
                continue
            try:
                data = json.loads(text)
                (check or (lambda d: schemas.validate(d, schema)))(data)
                return data
            except (ValueError, schemas.SchemaValidationError) as exc:
                last_error = str(exc)
        raise LLMError(f"{role} call did not return valid output: {last_error}")


def zdr_confirmed(agent_settings: dict) -> bool:
    if os.environ.get("RENEWAL_AGENT_ZDR_CONFIRMED", "").lower() == "true":
        return True
    return bool((agent_settings.get("policy") or {}).get("zdr_confirmed"))
