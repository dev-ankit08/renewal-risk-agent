"""Every shipped example config and preset must pass static validation."""
from pathlib import Path

import pytest

from renewal_risk.core.settings import load_presets, load_settings
from renewal_risk.core.validate import validate_settings

EXAMPLES = sorted(p.parent for p in (Path(__file__).resolve().parent.parent / "examples").glob("*/config/connectors.yaml"))
ENV = ["STRIPE_API_KEY", "ZENDESK_API_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "HUBSPOT_TOKEN",
       "FRESHDESK_API_KEY", "ERP_ACCOUNT", "ERP_TOKEN"]


@pytest.mark.parametrize("config_dir", EXAMPLES, ids=lambda p: p.parent.name)
def test_example_configs_validate(config_dir, monkeypatch):
    for name in ENV:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("USAGE_DB_DSN", "sqlite://")
    errors, _ = validate_settings(load_settings(config_dir))
    assert errors == []


def test_every_preset_resolves_with_dummy_params(tmp_path):
    for name, preset in load_presets().items():
        params = {p: "x" for p in (preset.get("params") or {})}
        for method in preset.get("auth_methods") or [{}]:
            params.update({p: "x" for p in (method.get("params") or {})})
            break  # the first (default) method
        (tmp_path / "connectors.yaml").write_text(
            "sources:\n  s:\n    preset: " + name + "\n" + "".join(f"    {k}: {v}\n" for k, v in params.items()), encoding="utf-8")
        errors, _ = validate_settings(load_settings(tmp_path))
        assert [e for e in errors if "primary source" not in e] == [], name
