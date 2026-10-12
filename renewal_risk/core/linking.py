"""
linking.py — which records in each non-primary source belong to which account.

The primary source (connectors.yaml `primary:`) owns the account list. Every
other source is linked to it by the rules under `linking.<source>`, tried in
order, first match wins per customer:

  - match: same_id                              the source already uses the primary account id
  - match: "key_account_id = account.id"        a customer field equals an account field
  - match: "id = account.key_stripe_customer_id"   (either side can hold the foreign id)
  - match: domain                               customer email/web domain == account domain
  - match: fuzzy_name   threshold: 0.88         similar names -> REVIEW QUEUE ONLY, never auto
                        review: false           (...unless the admin explicitly opts out)

config/links.csv (source,account_id,link_value,status) holds manual links and
review decisions; `approved` rows always win, `rejected` rows are never used.

A source with no `customer` entity and no rules is treated as `same_id`.
An account can link to several customers (e.g. two Stripe customers).
"""
from __future__ import annotations

import csv
import json
import re
import threading
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from .connector import ConnectorPool

FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com",
    "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com", "zoho.com",
}

_SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|gmbh|ag|sa|sas|bv|nv|plc|pty|pvt|private|group|holdings|the)\b\.?",
    re.IGNORECASE,
)


def normalise_name(name: str) -> str:
    text = _SUFFIXES.sub(" ", str(name or "").lower())
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def name_similarity(a: str, b: str) -> float:
    na, nb = normalise_name(a), normalise_name(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


@dataclass
class SourceLinks:
    source: str
    method: str  # "same_id" or "rules"
    links: dict[str, list[dict]] = field(default_factory=dict)  # account_id -> [{value, method}]
    review: list[dict] = field(default_factory=list)
    unmatched_customers: list[dict] = field(default_factory=list)
    customer_count: int = 0
    error: str | None = None

@dataclass
class LinkReport:
    built_at: float
    account_ids: list[str]
    accounts: dict[str, str]  # id -> name
    sources: dict[str, SourceLinks]

    def to_json(self) -> dict:
        return {
            "built_at": self.built_at,
            "accounts": self.accounts,
            "sources": {
                name: {"method": s.method, "links": s.links, "review": s.review, "error": s.error,
                       "customer_count": s.customer_count, "unmatched_customers": s.unmatched_customers[:500]}
                for name, s in self.sources.items()
            },
        }

    @classmethod
    def from_json(cls, data: dict) -> "LinkReport":
        sources = {
            name: SourceLinks(name, s["method"], s.get("links", {}), s.get("review", []),
                              s.get("unmatched_customers", []), s.get("customer_count", 0), s.get("error"))
            for name, s in data.get("sources", {}).items()
        }
        accounts = data.get("accounts", {})
        return cls(data.get("built_at", 0), list(accounts), accounts, sources)


def read_manual_links(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("source") and r.get("account_id")]


def write_manual_links(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["source", "account_id", "link_value", "status", "note"])
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in writer.fieldnames})


class Linker:
    def __init__(self, settings, pool: ConnectorPool):
        self.settings = settings
        self.pool = pool
        self.cache_path = settings.state_dir / "links.json"
        self.manual_path = settings.dir / "links.csv"
        self.cache_hours = float((settings.agent.get("linking") or {}).get("cache_hours", 24))
        self._report: LinkReport | None = None
        self._lock = threading.Lock()

    # -- rules ---------------------------------------------------------------

    def rules_for(self, source: str) -> list[dict]:
        configured = self.settings.linking.get(source)
        if configured:
            return [r if isinstance(r, dict) else {"match": r} for r in configured]
        has_customer = "customer" in (self.settings.sources.get(source, {}).get("entities") or {})
        if not has_customer:
            return [{"match": "same_id"}]
        return [{"match": "domain"}, {"match": "fuzzy_name"}]

    def is_same_id(self, source: str) -> bool:
        if source == self.settings.primary:
            return True
        return any(r.get("match") == "same_id" for r in self.rules_for(source))

    # -- building ------------------------------------------------------------

    def build(self, only: list[str] | None = None) -> LinkReport:
        primary = self.pool.get(self.settings.primary)
        accounts = primary.list("account")
        account_ids = [a["id"] for a in accounts]
        report = LinkReport(time.time(), account_ids, {a["id"]: a.get("name") or a["id"] for a in accounts}, {})
        manual = read_manual_links(self.manual_path)
        for source in self.settings.sources:
            if source == self.settings.primary or (only and source not in only):
                continue
            if self.is_same_id(source):
                report.sources[source] = SourceLinks(source, "same_id")
                continue
            try:
                report.sources[source] = self._build_source(source, accounts, manual)
            except Exception as exc:  # noqa: BLE001 - one broken source must not block the others
                report.sources[source] = SourceLinks(source, "rules", error=f"{type(exc).__name__}: {exc}")
        with self._lock:
            self._report = report
        self._save(report)
        return report

    def _build_source(self, source: str, accounts: list[dict], manual: list[dict]) -> SourceLinks:
        customers = self.pool.get(source).list("customer")
        result = SourceLinks(source, "rules", customer_count=len(customers))
        rejected = {(m["account_id"], m.get("link_value")) for m in manual if m["source"] == source and m.get("status") == "rejected"}
        approved = [m for m in manual if m["source"] == source and m.get("status", "approved") == "approved"]
        claimed: set[str] = set()

        def add(account_id: str, value: str, method: str) -> None:
            if (account_id, value) in rejected:
                return
            entries = result.links.setdefault(account_id, [])
            if not any(e["value"] == value for e in entries):
                entries.append({"value": value, "method": method})
            claimed.add(value)

        for m in approved:
            add(m["account_id"], m["link_value"], "manual")

        accounts_by_id = {a["id"]: a for a in accounts}
        for rule in self.rules_for(source):
            kind = rule.get("match", "")
            pending = [c for c in customers if c["id"] not in claimed]
            if kind == "domain":
                self._match_domain(pending, accounts, add, result)
            elif kind == "fuzzy_name":
                self._match_fuzzy(pending, accounts, add, result, rule, rejected)
            elif "=" in kind:
                left, right = [s.strip() for s in kind.split("=", 1)]
                left = left.removeprefix("customer.")
                if not right.startswith("account."):
                    raise ValueError(f"linking rule '{kind}': right side must be account.<field>")
                right = right.removeprefix("account.")
                index: dict[str, list[str]] = {}
                for a in accounts:
                    if a.get(right) not in (None, ""):
                        index.setdefault(str(a[right]).strip().lower(), []).append(a["id"])
                for c in pending:
                    value = c.get(left)
                    hits = index.get(str(value).strip().lower(), []) if value not in (None, "") else []
                    if len(hits) == 1:
                        add(hits[0], c["id"], f"key:{left}")
            elif kind:
                raise ValueError(f"unknown linking rule '{kind}' for source '{source}'")

        result.unmatched_customers = [
            {"id": c["id"], "name": c.get("name")} for c in customers
            if c["id"] not in claimed and not any(r["customer_id"] == c["id"] for r in result.review)
        ]
        for item in result.review:
            item["account_name"] = accounts_by_id.get(item["account_id"], {}).get("name")
        return result

    @staticmethod
    def _match_domain(pending, accounts, add, result) -> None:
        index: dict[str, set[str]] = {}
        for a in accounts:
            for d in a.get("domain") or []:
                if d not in FREE_EMAIL_DOMAINS:
                    index.setdefault(d, set()).add(a["id"])
        for c in pending:
            hits: set[str] = set()
            for d in c.get("domain") or []:
                if d not in FREE_EMAIL_DOMAINS:
                    hits |= index.get(d, set())
            if len(hits) == 1:
                add(next(iter(hits)), c["id"], "domain")
            elif len(hits) > 1:
                for account_id in sorted(hits):
                    result.review.append({"account_id": account_id, "customer_id": c["id"], "customer_name": c.get("name"),
                                          "score": None, "reason": "domain shared by several accounts"})

    @staticmethod
    def _match_fuzzy(pending, accounts, add, result, rule, rejected) -> None:
        threshold = float(rule.get("threshold", 0.88))
        auto = rule.get("review") is False
        for c in pending:
            best, best_score = None, 0.0
            for a in accounts:
                score = name_similarity(c.get("name"), a.get("name"))
                if score > best_score:
                    best, best_score = a, score
            if best is None or best_score < threshold or (best["id"], c["id"]) in rejected:
                continue
            if auto:
                add(best["id"], c["id"], f"fuzzy:{best_score:.2f}")
            else:
                result.review.append({"account_id": best["id"], "customer_id": c["id"], "customer_name": c.get("name"),
                                      "score": round(best_score, 3), "reason": "similar name"})

    # -- cache ---------------------------------------------------------------

    def _save(self, report: LinkReport) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(report.to_json(), indent=1), encoding="utf-8")

    def report(self, refresh: bool = False) -> LinkReport | None:
        with self._lock:
            if self._report is None and self.cache_path.exists():
                try:
                    self._report = LinkReport.from_json(json.loads(self.cache_path.read_text(encoding="utf-8")))
                except (ValueError, KeyError):
                    self._report = None
            report = self._report
        stale = (
            report is None
            or (time.time() - report.built_at) > self.cache_hours * 3600
            or (self.manual_path.exists() and self.manual_path.stat().st_mtime > report.built_at)
        )
        if refresh or stale:
            needs_rules = any(not self.is_same_id(s) for s in self.settings.sources if s != self.settings.primary)
            if not needs_rules:
                return None
            return self.build()
        return report

    def link_values(self, source: str, account_id: str) -> list[str]:
        """The values to fetch `source` records with for one account ([] = not linked)."""
        if self.is_same_id(source):
            return [account_id]
        report = self.report()
        if report is None or source not in report.sources:
            return []
        return [e["value"] for e in report.sources[source].links.get(account_id, [])]
