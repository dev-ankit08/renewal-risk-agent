"""
pipeline.py — one account, end to end.

  gather      every source that provides an entity a specialist needs is asked
              for this account's records (through the account links), mapped
              to standard records, capped, and as-of filtered
  screen      the signal engine computes candidate signals (free)
  coordinate  optional planning call: which specialists to run
  specialists one Claude call per specialist, in parallel; each must cite refs
              it was actually given — anything else is dropped and recorded
  audit       Gate 0 leakage audit over every record gathered
  synthesize  score, band, drivers, citations, rationale, recommended actions

Engine is the long-lived object (settings + connector pool + linker + model
client); build one per process and share it.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date

from . import guard, prompts, schemas, signals
from .connector import ConnectorError, ConnectorPool
from .linking import Linker
from .llm import AnthropicLLM, LLMError, Usage, zdr_confirmed
from .settings import Settings, get_settings


class PolicyError(Exception):
    """A run that policy forbids (e.g. real customer data without ZDR confirmation)."""


class AccountLookupError(Exception):
    def __init__(self, message: str, candidates: list[dict] | None = None):
        super().__init__(message)
        self.candidates = candidates or []


@dataclass
class RunResult:
    account_id: str
    account_name: str | None
    as_of: str
    assessment: dict
    packets: list[dict]
    candidates: dict
    coordinator: dict | None
    leakage_violations: list[dict]
    fabricated_refs: list[str]
    source_gaps: list[str]
    record_counts: dict
    cost_usd: float
    llm_calls: int
    duration_s: float
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


def band_for(score: float) -> str:
    return "high" if score >= 70 else "medium" if score >= 40 else "low"


def _for_prompt(record: dict) -> dict:
    return {k: v for k, v in record.items() if not k.startswith("_") and v not in (None, "", [])}


class Engine:
    def __init__(self, settings: Settings | None = None, llm=None, store=None, pool: ConnectorPool | None = None):
        self.settings = settings or get_settings()
        self.pool = pool or ConnectorPool(self.settings.sources)
        self.linker = Linker(self.settings, self.pool)
        self.llm = llm or AnthropicLLM(self.settings.agent["llm"])
        self.store = store

    # -- configuration views -------------------------------------------------

    def specialists(self) -> dict[str, dict]:
        return self.settings.agent.get("specialists") or {}

    def available_entities(self) -> set[str]:
        return {e for e in self._needed_entities() if self.pool.providers(e)}

    def _needed_entities(self) -> set[str]:
        needed: set[str] = set()
        for spec in self.specialists().values():
            needed.update(spec.get("entities", []))
        for sig in self.settings.signals:
            needed.update(signals.required_entities(sig))
        return needed

    def primary(self):
        if not self.settings.primary:
            raise ConnectorError("no primary source configured (connectors.yaml `primary:`)")
        return self.pool.get(self.settings.primary)

    def _log(self, account_id: str, as_of: str, stage: str, detail) -> None:
        if self.store is not None:
            self.store.log(account_id, as_of, stage, detail)

    # -- accounts --------------------------------------------------------------

    def get_account(self, account_id: str) -> dict | None:
        primary = self.primary()
        if primary.has_mode("account", "fetch"):
            rows = primary.fetch("account", account_id, date.today().isoformat())
        else:
            rows = [a for a in primary.list("account") if a["id"] == account_id]
        return rows[0] if rows else None

    def find_account(self, text: str) -> dict:
        """Name -> one account. Exact (case-insensitive) match wins; otherwise a
        single fuzzy hit; otherwise AccountLookupError with candidates."""
        text = (text or "").strip()
        if not text:
            raise AccountLookupError("No company name given.")
        hits = self.primary().search("account", text)
        exact = [h for h in hits if str(h.get("name", "")).strip().lower() == text.lower()]
        if len(exact) == 1:
            return exact[0]
        pool = exact or hits
        if len(pool) == 1:
            return pool[0]
        if not pool:
            raise AccountLookupError(f"No account found matching '{text}'.")
        raise AccountLookupError(
            f"Several accounts match '{text}' — which one?",
            [{"account_id": h["id"], "name": h.get("name")} for h in pool[:10]],
        )

    def list_renewals(self, before: str, after: str | None = None) -> list[dict]:
        """Open renewals closing in [after, before], one row per account (earliest)."""
        providers = self.pool.providers("renewal")
        if not providers:
            raise ConnectorError("no source provides 'renewal' — renewal windows need one")
        source = self.settings.primary if self.settings.primary in providers else providers[0]
        rows = self.pool.get(source).list("renewal", before=before, after=after or "1900-01-01")
        names: dict[str, str] = {}
        out: dict[str, dict] = {}
        for r in rows:
            close = r.get("close_date")
            if r.get("is_open") is False or not close or close > before or (after and close < after):
                continue
            account_id = r.get("account_id")
            if not account_id:
                continue
            if account_id not in out or close < out[account_id]["close_date"]:
                out[account_id] = {"account_id": account_id, "name": r.get("account_name"), "close_date": close,
                                   "amount": r.get("amount"), "renewal_ref": r["ref"]}
        missing = [a for a in out.values() if not a["name"]]
        if missing:
            try:
                names = {a["id"]: a.get("name") for a in self.primary().list("account")}
            except ConnectorError:
                names = {}
            for a in missing:
                a["name"] = names.get(a["account_id"]) or a["account_id"]
        return sorted(out.values(), key=lambda a: a["close_date"])

    # -- gathering -------------------------------------------------------------

    def gather(self, account_id: str, as_of: str, entities: set[str] | None = None) -> tuple[dict[str, list[dict]], list[str], set[str]]:
        """-> (records by entity, source gaps, sources that returned data)"""
        entities = entities or self._needed_entities()
        cap = int((self.settings.agent.get("policy") or {}).get("max_records_per_entity", 200))
        records: dict[str, list[dict]] = {e: [] for e in entities}
        gaps: list[str] = []
        used: set[str] = set()
        jobs = []
        for entity in sorted(entities):
            for source in self.pool.providers(entity):
                jobs.append((entity, source))

        def run(job):
            entity, source = job
            try:
                values = self.linker.link_values(source, account_id)
            except Exception as exc:  # noqa: BLE001
                return entity, source, [], f"{source}: could not link accounts ({type(exc).__name__}: {exc})"
            if not values:
                return entity, source, [], f"{source}: account {account_id} is not linked to any {source} customer — its {entity} data was not read"
            out = []
            try:
                for value in values:
                    out.extend(self.pool.get(source).fetch(entity, value, as_of))
            except Exception as exc:  # noqa: BLE001 - one source failing must not sink the run
                return entity, source, [], f"{source}: reading {entity} failed ({type(exc).__name__}: {exc})"
            return entity, source, out, None

        # Sources are independent; MCP-backed ones serialize internally.
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(jobs)))) as pool:
            for entity, source, rows, gap in pool.map(run, jobs):
                if gap:
                    gaps.append(gap)
                for r in rows:
                    r["account_id"] = account_id  # source-side ids replaced by the primary account id
                if rows:
                    used.add(source)
                records[entity].extend(rows)
        for entity, rows in records.items():
            rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
            if len(rows) > cap:
                gaps.append(f"{entity}: {len(rows)} records, only the newest {cap} were assessed")
                records[entity] = rows[:cap]
        return records, sorted(set(gaps)), used

    # -- free screen ---------------------------------------------------------

    def prescreen(self, account_id: str, as_of: str | None = None) -> dict:
        as_of = as_of or date.today().isoformat()
        records, gaps, _used = self.gather(account_id, as_of)
        result = signals.evaluate(self.settings.signals, records, as_of, self.available_entities())
        all_signals = [s for bucket in result.values() for s in bucket["signals"]]
        high = sum(1 for s in all_signals if s["severity"] == "high")
        medium = sum(1 for s in all_signals if s["severity"] == "medium")
        return {
            "account_id": account_id, "as_of": as_of,
            "signals": all_signals, "high_count": high, "medium_count": medium,
            "flag": high > 0 or medium >= 2,
            "gaps": gaps + [g for bucket in result.values() for g in bucket["gaps"]],
        }

    # -- full assessment -------------------------------------------------------

    def _check_policy(self, used_sources: set[str]) -> None:
        if zdr_confirmed(self.settings.agent):
            return
        real = sorted(s for s in used_sources if not self.settings.sources.get(s, {}).get("test_data"))
        if real:
            raise PolicyError(
                f"Refusing to send data from {', '.join(real)} to the Anthropic API: these sources are not marked "
                "`test_data: true` and the data-retention policy is not confirmed. Confirm your Anthropic organization "
                "has a Zero Data Retention (or equivalent) agreement, then set `policy.zdr_confirmed: true` in "
                "agent.yaml or RENEWAL_AGENT_ZDR_CONFIRMED=true."
            )

    def assess(self, account_id: str, as_of: str | None = None, account_name: str | None = None) -> RunResult:
        started = time.time()
        as_of = as_of or date.today().isoformat()
        usage = Usage()
        records, source_gaps, used = self.gather(account_id, as_of)
        self._check_policy(used)
        self._log(account_id, as_of, "gather", {"counts": {e: len(r) for e, r in records.items()}, "gaps": source_gaps})

        available = self.available_entities()
        candidates = signals.evaluate(self.settings.signals, records, as_of, available)
        self._log(account_id, as_of, "candidates", candidates)

        # Specialists with at least one record to read.
        runnable = {}
        for role, spec in self.specialists().items():
            counts = {e: len(records.get(e, [])) for e in spec.get("entities", []) if e in available}
            if sum(counts.values()) > 0:
                runnable[role] = {"entities": counts}

        coordinator = None
        chosen = list(runnable)
        if runnable and self.settings.agent["llm"].get("use_coordinator", True) and len(runnable) > 1:
            system, user = prompts.coordinator_prompt(account_id, runnable)
            coordinator = self.llm.structured("coordinator", system, user, schemas.COORDINATOR_SCHEMA, usage)
            picked = [s for s in coordinator.get("specialists", []) if s in runnable]
            chosen = picked or chosen
            self._log(account_id, as_of, "coordinator", coordinator)

        fabricated: list[str] = []

        def run_specialist(role: str) -> dict:
            spec = self.specialists()[role]
            role_records = {e: [_for_prompt(r) for r in records.get(e, [])] for e in spec.get("entities", []) if records.get(e)}
            catalog = [s for s in self.settings.signals if s.get("specialist") == role]
            role_candidates = candidates.get(role, {"signals": [], "gaps": []})
            system, user = prompts.specialist_prompt(role, spec.get("focus", ""), catalog, account_id, as_of, role_records,
                                                     {"signals": role_candidates["signals"], "gaps": role_candidates["gaps"]})
            packet = self.llm.structured("specialist", system, user, schemas.EVIDENCE_PACKET_SCHEMA, usage, check=schemas.check_packet)
            known = {r["ref"] for rows in role_records.values() for r in rows}
            packet.update({"specialist": role, "account_id": account_id, "as_of": as_of})
            kept = []
            for s in packet["signals"]:
                if s["evidence_ref"] in known:
                    kept.append(s)
                else:
                    fabricated.append(s["evidence_ref"])
                    packet["gaps"].append(f"dropped signal '{s['name']}': cited a record it was not given")
            packet["signals"] = kept
            packet["evidence_refs"] = sorted({r for r in packet["evidence_refs"] if r in known} | {s["evidence_ref"] for s in kept})
            return packet

        packets: list[dict] = []
        if chosen:
            with ThreadPoolExecutor(max_workers=len(chosen)) as pool:
                packets = list(pool.map(run_specialist, chosen))
        for packet in packets:
            self._log(account_id, as_of, f"specialist:{packet['specialist']}", packet)
        for role in runnable:
            if role not in chosen:
                source_gaps.append(f"{role} specialist skipped by the coordinator")

        all_records = [r for rows in records.values() for r in rows]
        leakage = guard.audit_leakage(all_records, as_of)
        self._log(account_id, as_of, "leakage_audit", leakage)

        if not packets:
            assessment = {
                "account_id": account_id, "score": 0, "band": "low", "drivers": [], "citations": [], "abstained": True,
                "rationale": "No source returned any records for this account, so there is nothing to assess. "
                             "Check that the account is linked in each source (`renewal-risk link --preview`).",
                "recommended_actions": [],
            }
        else:
            system, user = prompts.synthesis_prompt(account_id, account_name, as_of, packets, self.settings.playbooks)
            assessment = self.llm.structured("synthesis", system, user, schemas.RISK_ASSESSMENT_SCHEMA, usage, check=schemas.check_assessment)
            known = {r for p in packets for r in p["evidence_refs"]}
            fabricated.extend(c for c in assessment["citations"] if c not in known)
            assessment["citations"] = [c for c in assessment["citations"] if c in known]
            for action in assessment["recommended_actions"]:
                fabricated.extend(r for r in action["evidence_refs"] if r not in known)
                action["evidence_refs"] = [r for r in action["evidence_refs"] if r in known]
            assessment["recommended_actions"] = [a for a in assessment["recommended_actions"] if a["evidence_refs"]]
            assessment["account_id"] = account_id
            assessment["band"] = band_for(assessment["score"])
        self._log(account_id, as_of, "synthesis", assessment)

        result = RunResult(
            account_id=account_id, account_name=account_name, as_of=as_of, assessment=assessment, packets=packets,
            candidates=candidates, coordinator=coordinator, leakage_violations=leakage, fabricated_refs=sorted(set(fabricated)),
            source_gaps=source_gaps, record_counts={e: len(r) for e, r in records.items()},
            cost_usd=round(usage.cost_usd, 4), llm_calls=usage.calls, duration_s=round(time.time() - started, 1),
        )
        if self.store is not None:
            self.store.save_assessment(result)
        return result

    def close(self) -> None:
        self.pool.close()


__all__ = ["Engine", "RunResult", "PolicyError", "AccountLookupError", "LLMError", "ConnectorError", "band_for"]
