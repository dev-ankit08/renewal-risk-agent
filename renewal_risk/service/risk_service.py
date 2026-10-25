"""
risk_service.py — the one entry point the REST API, Slack and the scheduler
use. Access control is applied here, so every interface enforces the same rules.

  ask(principal, question)            plain-English question -> answer dict
  account_assessment(principal, id)   stored (if fresh) or new assessment
  portfolio(principal, start, end)    renewals in a window: free screen for all,
                                      full assessment for the riskiest few
"""
from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from .. import nlquery
from ..access.rbac import AccessDenied, AccessPolicy, Principal, can_view
from ..core.connector import ConnectorError
from ..core.llm import LLMError
from ..core.pipeline import AccountLookupError, Engine, PolicyError

BAND_ORDER = {"low": 0, "medium": 1, "high": 2}


def screen_band(screen: dict) -> str:
    if screen["high_count"]:
        return "high"
    if screen["medium_count"]:
        return "medium"
    return "low"


def assessment_view(result: dict) -> dict:
    """The shape every interface renders (API JSON, Slack blocks, CLI)."""
    a = result["assessment"]
    how = result.get("explanations") or {}
    signals = [
        {**s, "specialist": p["specialist"], "how_calculated": how.get(s["name"], "")}
        for p in result.get("packets", []) for s in p.get("signals", [])
    ]
    signals.sort(key=lambda s: -BAND_ORDER.get(s["severity"], 0))
    gaps = list(result.get("source_gaps", [])) + [g for p in result.get("packets", []) for g in p.get("gaps", [])]
    return {
        "account_id": result["account_id"],
        "account_name": result.get("account_name") or result["account_id"],
        "as_of": result["as_of"],
        "score": a["score"], "band": a["band"], "abstained": a["abstained"],
        "drivers": a["drivers"], "rationale": a["rationale"],
        "recommended_actions": a.get("recommended_actions", []),
        "citations": a["citations"], "signals": signals, "gaps": gaps,
        "leakage_violations": len(result.get("leakage_violations", [])),
        "fabricated_refs": result.get("fabricated_refs", []),
        "cost_usd": result.get("cost_usd"), "assessed_at": result.get("created_at"),
        "record_counts": result.get("record_counts", {}),
        "assessment_id": result.get("assessment_id"),
    }


class RiskService:
    def __init__(self, engine: Engine, store, policy: AccessPolicy | None = None):
        self.engine = engine
        self.store = store
        self.policy = policy or AccessPolicy(engine.settings.access)
        self._directory: dict[str, dict] | None = None
        self._directory_at = 0.0
        self._directory_lock = threading.Lock()

    @property
    def settings(self):
        return self.engine.settings

    def after_assessment(self, result, send=None) -> dict:
        """The view of a fresh assessment, after DMing its action owners
        (service/action_notifier.py; a no-op without Slack)."""
        view = assessment_view(result.to_dict())
        try:
            from .action_notifier import notify
            notify(self, view, self.account(result.account_id) or {"id": result.account_id}, send)
        except Exception as exc:  # noqa: BLE001 - notifications never fail an assessment
            print(f"actions: notify failed for {result.account_id}: {exc}", file=sys.stderr)
        return view

    # -- account directory (for scope checks) --------------------------------

    def directory(self, refresh: bool = False) -> dict[str, dict]:
        with self._directory_lock:
            if refresh or self._directory is None or time.time() - self._directory_at > 600:
                try:
                    self._directory = {a["id"]: a for a in self.engine.primary().list("account")}
                except ConnectorError as exc:
                    print(f"warning: account directory unavailable ({exc}); falling back to per-account lookups", file=sys.stderr)
                    self._directory = self._directory or {}
                self._directory_at = time.time()
            return self._directory

    def account(self, account_id: str) -> dict | None:
        found = self.directory().get(account_id)
        if found is None:
            found = self.engine.get_account(account_id)
            if found is not None and self._directory is not None:
                self._directory[account_id] = found
        return found

    def _visible(self, principal: Principal, account_id: str) -> dict:
        account = self.account(account_id)
        if not can_view(principal, account):
            # Same message whether it doesn't exist or is out of scope: no existence leak.
            raise AccessDenied("No account you have access to matches that.")
        return account

    # -- single account --------------------------------------------------------

    def resolve(self, principal: Principal, text: str) -> dict:
        principal.require("view")
        try:
            account = self.engine.find_account(text)
        except AccountLookupError as exc:
            visible = [c for c in exc.candidates if can_view(principal, self.account(c["account_id"]))]
            if len(visible) == 1:
                return self.account(visible[0]["account_id"])
            if not visible:
                raise AccountLookupError(f"No account you have access to matches '{text}'.") from exc
            raise AccountLookupError(str(exc), visible) from exc
        if not can_view(principal, self.account(account["id"]) or account):
            raise AccountLookupError(f"No account you have access to matches '{text}'.")
        return self.account(account["id"]) or account

    def account_assessment(self, principal: Principal, account_id: str, fresh: bool = False, as_of: str | None = None) -> dict:
        account = self._visible(principal, account_id)
        reuse = float((self.engine.settings.agent.get("bulk") or {}).get("reuse_assessment_hours", 24))
        if not fresh and as_of is None:
            stored = self.store.latest_assessment(account_id, max_age_hours=reuse)
            if stored:
                return {**assessment_view(stored), "from_cache": True}
        if not principal.can("assess"):
            stored = self.store.latest_assessment(account_id)
            if stored:
                return {**assessment_view(stored), "from_cache": True}
            raise AccessDenied("There is no stored assessment for this account yet, and your role cannot start a new one.")
        result = self.engine.assess(account_id, as_of=as_of, account_name=account.get("name"))
        return {**self.after_assessment(result), "from_cache": False}

    # -- portfolio / window --------------------------------------------------

    def portfolio(self, principal: Principal, start: date, end: date, band_filter: str = "any",
                  label: str | None = None, deep: bool = True) -> dict:
        principal.require("bulk")
        renewals = self.engine.list_renewals(end.isoformat(), start.isoformat())
        visible = [r for r in renewals if can_view(principal, self.account(r["account_id"]))]
        today = date.today().isoformat()

        def screen(row):
            try:
                return {**row, "screen": self.engine.prescreen(row["account_id"], today)}
            except Exception as exc:  # noqa: BLE001 - keep the rest of the portfolio
                return {**row, "screen": None, "error": f"{type(exc).__name__}: {exc}"}

        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(screen, visible))

        bulk_cfg = self.engine.settings.agent.get("bulk") or {}
        reuse = float(bulk_cfg.get("reuse_assessment_hours", 24))
        cap = int(bulk_cfg.get("max_deep_assessments", 10))
        for row in rows:
            stored = self.store.latest_assessment(row["account_id"], max_age_hours=reuse)
            row["assessment"] = assessment_view(stored) if stored else None
        ranked = sorted(
            (r for r in rows if r.get("screen") and r["screen"]["flag"] and r["assessment"] is None),
            key=lambda r: (-r["screen"]["high_count"], -r["screen"]["medium_count"]),
        )
        to_assess = ranked[:cap] if (deep and principal.can("assess")) else []
        cost = 0.0

        def assess(row):
            try:
                result = self.engine.assess(row["account_id"], account_name=row.get("name"))
                return row["account_id"], self.after_assessment(result), result.cost_usd, None
            except (LLMError, PolicyError, ConnectorError) as exc:
                return row["account_id"], None, 0.0, str(exc)

        if to_assess:
            with ThreadPoolExecutor(max_workers=3) as pool:
                for account_id, view, spent, error in pool.map(assess, to_assess):
                    cost += spent
                    for row in rows:
                        if row["account_id"] == account_id:
                            row["assessment"] = view
                            if error:
                                row["error"] = error

        for row in rows:
            if row["assessment"]:
                row["band"], row["score"], row["band_source"] = row["assessment"]["band"], row["assessment"]["score"], "assessment"
            elif row.get("screen"):
                row["band"], row["score"], row["band_source"] = screen_band(row["screen"]), None, "screen"
            else:
                row["band"], row["score"], row["band_source"] = None, None, "error"
        minimum = {"any": 0, "medium_or_high": 1, "high": 2}.get(band_filter, 0)
        shown = [r for r in rows if r["band"] and BAND_ORDER[r["band"]] >= minimum]
        shown.sort(key=lambda r: (-BAND_ORDER.get(r["band"], -1), -(r["score"] or 0), r["close_date"]))
        return {
            "label": label or f"{start} to {end}", "window_start": start.isoformat(), "window_end": end.isoformat(),
            "band_filter": band_filter, "renewals_in_window": len(visible), "assessed_now": len(to_assess),
            "not_assessed_flagged": max(0, len(ranked) - len(to_assess)), "cost_usd": round(cost, 4), "rows": shown,
            "errors": [r for r in rows if r.get("error")],
        }

    # -- plain English -------------------------------------------------------

    def ask(self, principal: Principal, question: str) -> dict:
        try:
            intent, params = nlquery.parse_intent(question, llm=self.engine.llm)
        except nlquery.IntentParseError as exc:
            return {"type": "help", "message": str(exc)}
        except LLMError as exc:
            return {"type": "error", "message": f"Could not understand the question: {exc}"}
        try:
            if intent == "single_account":
                account = self.resolve(principal, params["company_name"])
                view = self.account_assessment(principal, account["id"])
                return {"type": "single_account", **view}
            return {"type": "bulk_window", **self.portfolio(principal, params["start"], params["end"], params["band_filter"], params["label"])}
        except AccountLookupError as exc:
            return {"type": "ambiguous" if exc.candidates else "not_found", "message": str(exc), "candidates": exc.candidates}
        except AccessDenied as exc:
            return {"type": "denied", "message": str(exc)}
        except PolicyError as exc:
            return {"type": "error", "message": str(exc)}
        except (ConnectorError, LLMError) as exc:
            return {"type": "error", "message": f"Assessment failed: {exc}"}
