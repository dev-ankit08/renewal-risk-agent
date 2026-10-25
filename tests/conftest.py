"""Shared fixtures: the CSV demo built into a temp dir, and a fake model client."""
from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

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
    def _play_for(system: str, signal: str) -> str:
        """The playbook id the synthesis prompt lists for this signal, if any."""
        m = re.search(rf"- id (\w+): when [^\n]*\b{re.escape(signal)}\b", system)
        return m.group(1) if m else ""

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
                     "evidence_refs": [s["evidence_ref"]], "why": f"{s['name']} is high", "playbook_id": self._play_for(system, s["name"])}
                    for s in sigs if s["severity"] == "high"
                ][:4],
            }
        elif "spec_json" in json.dumps(schema):
            # Signal drafting: a fixed, valid answer to "3+ open urgent tickets".
            spec = {"entity": "ticket", "where": {"is_open": True, "priority": ["urgent", "high"]}, "thresholds": {"medium": 2, "high": 3}}
            data = {"name": "Urgent Ticket Pileup", "description": "Several urgent tickets open at once.", "specialist": "relationship",
                    "rule": "count", "spec_json": json.dumps(spec), "reason": "count of open urgent tickets"}
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
    # Hermetic copy: skip what the admin UI writes while someone tries the demo by hand
    # (approved links, Slack settings, uploads, overrides) and force the demo's own pack.
    shutil.copytree(ROOT / "examples" / "csv-demo" / "config", demo / "config",
                    ignore=shutil.ignore_patterns("links.csv", "slack.yaml", "signals.yaml", "uploads", "mappings", "*.tmp"))
    agent_yaml = demo / "config" / "agent.yaml"
    agent = yaml.safe_load(agent_yaml.read_text(encoding="utf-8")) or {}
    agent["pack"] = "saas"
    agent_yaml.write_text(yaml.safe_dump(agent), encoding="utf-8")
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


@pytest.fixture()
def admin(ctx):
    """A signed-in admin client on the CSV demo config."""
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from renewal_risk.admin.api import secret_store_for
    from renewal_risk.admin.auth import setup_token
    from renewal_risk.service.api import app

    client = TestClient(app)
    status = client.get("/admin/api/status").json()
    assert status["needs_setup"] is True
    token = setup_token(secret_store_for(ctx), ctx.store)
    assert client.post("/admin/api/setup", json={"setup_token": "wrong", "email": "a@x.com", "password": "long-enough-pw"}).status_code == 403
    r = client.post("/admin/api/setup", json={"setup_token": token, "email": "Admin@Demo.example", "password": "long-enough-pw", "pack": "saas"})
    assert r.status_code == 200 and "rr_admin" in r.cookies
    return client
