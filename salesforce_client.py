"""
salesforce_client.py — one interface, one real implementation.

SfCliSalesforce shells out to the Salesforce CLI (`sf`), already authenticated
on this machine against the org alias in config.SALESFORCE_ORG_ALIAS. There is
no MCP connection and no Salesforce token/URL to configure — auth is whatever
that `sf` session already has.

Every read passes through guard.soql_guard / guard.strip_banned_fields first.
Specialists never see an unguarded record.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from abc import ABC, abstractmethod

import config
import guard


class SalesforceClient(ABC):
    @abstractmethod
    def soql_query(self, soql: str, as_of_date: str) -> list[dict]:
        ...

    @abstractmethod
    def get_record(self, object_name: str, record_id: str) -> dict | None:
        ...

    @abstractmethod
    def describe_object(self, object_name: str) -> dict:
        ...


class SalesforceQueryError(Exception):
    """Raised when the `sf` CLI itself reports a failure (bad SOQL, org not
    found, not authenticated, etc.) — carries the org's own error message."""


def _resolve_sf_executable() -> str:
    path = shutil.which("sf")
    if not path:
        raise SalesforceQueryError(
            "The `sf` (Salesforce CLI) executable was not found on PATH. "
            "Install it from https://developer.salesforce.com/tools/salesforcecli "
            "and confirm `sf org list` shows your target org as Connected."
        )
    return path


class SfCliSalesforce(SalesforceClient):
    """The only Salesforce integration in this repo. No mock/live toggle —
    this is always the real org identified by `org_alias`."""

    def __init__(self, org_alias: str):
        self.org_alias = org_alias
        self._sf_path = _resolve_sf_executable()

    def soql_query(self, soql: str, as_of_date: str, date_field: str = "CreatedDate") -> list[dict]:
        guarded = guard.soql_guard(soql, as_of_date, date_field)
        result = self._run_sf_json(["data", "query", "--query", guarded])
        records = result.get("result", {}).get("records", [])
        cleaned = []
        for record in records:
            record = self._strip_attributes(record)
            safe_record, _stripped = guard.strip_banned_fields(record)
            cleaned.append(safe_record)
        return cleaned

    def get_record(self, object_name: str, record_id: str) -> dict | None:
        # No as_of_date in this method's signature, so the full soql_guard
        # (which requires one) doesn't apply here — but the DML block always does.
        soql = guard.block_dml(f"SELECT FIELDS(ALL) FROM {object_name} WHERE Id = '{record_id}' LIMIT 1")
        result = self._run_sf_json(["data", "query", "--query", soql])
        records = result.get("result", {}).get("records", [])
        if not records:
            return None
        record = self._strip_attributes(records[0])
        safe_record, _stripped = guard.strip_banned_fields(record)
        return safe_record

    def describe_object(self, object_name: str) -> dict:
        result = self._run_sf_json(["sobject", "describe", "--sobject", object_name])
        fields = [f["name"] for f in result.get("result", {}).get("fields", [])]
        return {"name": object_name, "fields": fields}

    def _run_sf_json(self, args: list[str]) -> dict:
        full_args = [self._sf_path, *args, "--target-org", self.org_alias, "--json"]
        try:
            proc = subprocess.run(
                full_args,
                capture_output=True,
                text=True,
                encoding="utf-8",  # sf CLI always emits UTF-8 JSON; without this,
                                   # subprocess decodes with the system locale's
                                   # preferred encoding (cp1252 on Windows),
                                   # silently mangling any non-ASCII field value.
                timeout=config.SF_CLI_TIMEOUT_SECONDS,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise SalesforceQueryError(f"Could not run `sf`: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise SalesforceQueryError(
                f"`sf {' '.join(args)}` timed out after {config.SF_CLI_TIMEOUT_SECONDS}s"
            ) from exc

        stdout = proc.stdout or ""
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            brace = stdout.find("{")
            if brace == -1:
                raise SalesforceQueryError(
                    f"`sf {' '.join(args)}` returned no parseable JSON.\n"
                    f"stdout: {stdout!r}\nstderr: {proc.stderr!r}"
                )
            try:
                parsed = json.loads(stdout[brace:])
            except json.JSONDecodeError as exc:
                raise SalesforceQueryError(
                    f"`sf {' '.join(args)}` returned malformed JSON.\n"
                    f"stdout: {stdout!r}\nstderr: {proc.stderr!r}"
                ) from exc

        if parsed.get("status") != 0:
            message = parsed.get("message") or parsed.get("name") or str(parsed)
            raise SalesforceQueryError(message)
        return parsed

    @staticmethod
    def _strip_attributes(record: dict) -> dict:
        return {k: v for k, v in record.items() if k != "attributes"}


def get_client(org_alias: str | None = None) -> SalesforceClient:
    """Refuses to run against any org alias other than the known synthetic
    test sandbox unless config.ZDR_CONFIRMED is set — see config.py. Real
    Salesforce data reaches the Anthropic API in specialist prompts, so
    pointing this at a real org is a deliberate, confirmed act, not a
    side effect of changing an env var."""
    alias = org_alias or config.SALESFORCE_ORG_ALIAS
    known_test_aliases = {"renewalRiskSandbox"}
    if alias not in known_test_aliases and not config.ZDR_CONFIRMED:
        raise RuntimeError(
            f"Refusing to query org alias '{alias}' — it isn't the known synthetic "
            "test sandbox, so it may hold real customer data, which this pipeline "
            "sends to the Anthropic API in specialist prompts. Confirm your "
            "Anthropic API organization has a Zero Data Retention agreement in "
            "place (contact Anthropic to arrange one — this is an account-level "
            "agreement, not something this codebase can configure), then set "
            "RENEWAL_AGENT_ZDR_CONFIRMED=true to proceed."
        )
    return SfCliSalesforce(alias)
