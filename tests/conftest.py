"""Shared fixtures: the CSV demo built into a temp dir, and a fake model client."""
from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples" / "csv-demo"))

TODAY = date.today()


class FakeLLM:
    """Deterministic stand-in for AnthropicLLM. Specialists echo the candidate
    signals (plus one invented ref, which the pipeline must drop); synthesis
    scores by severity and recommends one action per high signal."""

    def __init__(self, fabricate: bool = True):
        self.calls: list[str] = []
        self.fabricate = fabricate

    @staticmethod
    def _block(text: str, tag: str):
        m = re.search(rf"<{tag}>\n(.*)\n</{tag}>", text, re.S)
        return json.loads(m.group(1)) if m else None

    def structured(self, role, system, user, schema, usage=None, check=None):
        self.calls.append(role)
        if usage is not None:
            usage.add("fake", 100, 50, 0.001)
        if role == "coordinator":
            available = json.loads(user.split("Available specialists:\n", 1)[1])
            return {"specialists": list(available), "rationale": "all have data"}
        if role == "specialist":
            role_name = re.search(r'Set specialist to "(\w+)"', system).group(1)
            account = re.search(r"Account: (\S+)", user).group(1)
            as_of = re.search(r"As-of date: (\S+)", user).group(1)
            cands = self._block(user, "candidate_signals")
            signals = [{k: s[k] for k in ("name", "severity", "value", "evidence_ref")} for s in cands["signals"]]
            if self.fabricate and signals:
                signals.append({"name": "made_up", "severity": "high", "value": "x", "evidence_ref": "crm:renewal/NOPE"})
            data = {"specialist": role_name, "account_id": account, "as_of": as_of, "signals": signals,
                    "gaps": list(cands["gaps"]), "evidence_refs": [s["evidence_ref"] for s in signals]}
        elif role == "synthesis":
            packets = self._block(user, "packets")
            account = re.search(r"Account: (\S+)", user).group(1)
            sigs = [s for p in packets for s in p["signals"]]
            score = min(100, sum({"high": 20, "medium": 10, "low": 3}[s["severity"]] for s in sigs))
            refs = sorted({s["evidence_ref"] for s in sigs})
            data = {
                "account_id": account, "score": score, "band": "low", "drivers": [s["name"] for s in sigs][:5],
                "citations": refs + (["billing:invoice/FAKE"] if self.fabricate else []), "abstained": not sigs,
                "rationale": f"{len(sigs)} signals.",
                "recommended_actions": [
                    {"action": f"Address {s['name']}", "owner_role": "Account owner", "priority": "high", "due_in_days": 7,
                     "evidence_refs": [s["evidence_ref"]]} for s in sigs if s["severity"] == "high"
                ][:4],
            }
        elif "value_map" in json.dumps(schema):
            # Mapping agent: take the best deterministic candidate, and show a value map for priority.
            cands = json.loads(user.split("(name/label/type similarity):\n", 1)[1].split("\n\n<source_fields>", 1)[0])
            fields = []
            for std, ranked in cands.items():
                if ranked:
                    value_map = [{"source_value": "P1", "standard_value": "urgent"}] if std == "priority" else []
                    fields.append({"field": std, "source_field": ranked[0]["source_field"], "expr": ranked[0]["source_field"],
                                   "value_map": value_map, "confidence": 0.9, "reason": "fake model"})
            data = {"fields": fields}
        else:
            data = {"intent": "unknown", "company_name": "", "window_start": "", "window_end": "", "band_filter": "any"}
        (check or (lambda d: None))(data)
        return data


@pytest.fixture()
def demo_config(tmp_path, monkeypatch):
    import generate

    demo = tmp_path / "demo"
    shutil.copytree(ROOT / "examples" / "csv-demo" / "config", demo / "config")
    generate.build(demo / "data", TODAY)
    monkeypatch.setenv("RENEWAL_RISK_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("RENEWAL_AGENT_ZDR_CONFIRMED", raising=False)
    return demo / "config"


@pytest.fixture()
def ctx(demo_config):
    from renewal_risk.core.settings import load_settings
    from renewal_risk.service.context import build_context, set_context

    context = build_context(load_settings(demo_config), llm=FakeLLM())
    set_context(context)
    yield context
    context.engine.close()
    set_context(None)
