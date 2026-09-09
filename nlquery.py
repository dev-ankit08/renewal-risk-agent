"""
nlquery.py — plain-English question parsing and answer orchestration for the
web UI (webapp.py). No NLP library for parsing (regex-based, instant, free) —
but both question shapes now trigger the REAL Coordinator->Specialists->
Synthesis pipeline (agent.run_account), not a heuristic screen. Every answer
from the web UI costs real Anthropic tokens and real time; the free heuristic
path (heuristics.py/prescreen.prescreen_account/prescreen_bulk) remains used
only by run.py's separate `prescreen` CLI command, not by this module.

Two question shapes are recognized:
  "is <company> at risk"                    -> single_account
  "accounts at risk this quarter/half/year"  -> bulk_window
"""
from __future__ import annotations

import calendar
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import agent
import prescreen
import salesforce_client

PERIOD_ALIASES = {
    "quarter": "quarter", "qtr": "quarter", "quater": "quarter",  # "quater" is a
    # confirmed real-world typo (this project's own original example question)
    "half": "half", "half year": "half", "half-year": "half", "halfyear": "half",
    "year": "year", "fiscal year": "year", "annual": "year",
}

_SINGLE_ACCOUNT_PATTERNS = [
    re.compile(r"\bis\s+['\"]?(.+?)['\"]?\s+(?:at[\s-]risk|a\s+risk)\b", re.IGNORECASE),
    re.compile(r"\brisk\s+(?:for|on|with)\s+['\"]?(.+?)['\"]?\s*[\?\.]*$", re.IGNORECASE),
]

# Longest alias first so "half year" matches before a bare "year" would.
_PERIOD_PATTERN = re.compile(
    r"\bthis\s+(" + "|".join(sorted(PERIOD_ALIASES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


class IntentParseError(Exception):
    """The input text didn't match either recognized question shape."""


def parse_intent(text: str) -> tuple[str, dict]:
    """Returns ("single_account", {"company_name": ...}) or
    ("bulk_window", {"period": "quarter"|"half"|"year"})."""
    text = (text or "").strip()

    # Bulk-window phrasing ("... at risk for this quarter") is checked first:
    # "this quarter/half/year" is a small, closed vocabulary, unlike a company
    # name, and the single-account fallback pattern's "risk (for|on|with) X"
    # shape would otherwise misread "at risk for this quarter" as a lookup
    # for a company literally named "this quarter".
    if "risk" in text.lower():
        match = _PERIOD_PATTERN.search(text)
        if match:
            period = PERIOD_ALIASES[match.group(1).lower()]
            return "bulk_window", {"period": period}

    for pattern in _SINGLE_ACCOUNT_PATTERNS:
        match = pattern.search(text)
        if match:
            name = match.group(1).strip(" '\"?.")
            if name:
                return "single_account", {"company_name": name}

    raise IntentParseError(
        "I couldn't tell if you're asking about one company or a time window. "
        "Try: \"is Acme Corp at risk\" or \"accounts at risk this quarter\"."
    )


def resolve_window(period: str, today: date | None = None) -> tuple[date, date]:
    """Calendar-based window relative to `today` (defaults to date.today()).
    quarter: Q1-Q4 (Jan-Mar/Apr-Jun/Jul-Sep/Oct-Dec). half: Jan-Jun/Jul-Dec.
    year: Jan-Dec. Raises ValueError on an unrecognized period."""
    today = today or date.today()
    if period == "quarter":
        q_index = (today.month - 1) // 3
        start_month = q_index * 3 + 1
        end_month = start_month + 2
    elif period == "half":
        start_month, end_month = (1, 6) if today.month <= 6 else (7, 12)
    elif period == "year":
        start_month, end_month = 1, 12
    else:
        raise ValueError(f"unknown period: {period!r}")

    start = date(today.year, start_month, 1)
    end_day = calendar.monthrange(today.year, end_month)[1]
    end = date(today.year, end_month, end_day)
    return start, end


def answer_single_account(company_name: str, client=None) -> dict:
    """Resolves a company name to an Account (free, no AI — just a name
    lookup), then runs the REAL Coordinator->Specialists->Synthesis pipeline
    on it. Raises prescreen.AccountLookupError on 0/ambiguous matches before
    any Anthropic call happens."""
    client = client or salesforce_client.get_client()
    account = prescreen.resolve_account_by_name(company_name, client)
    as_of = date.today().isoformat()
    result = agent.run_account(account["account_id"], as_of, client=client)
    agent.write_brief(result, account["name"])
    agent.append_briefs_csv(result, account["name"])
    a = result.assessment
    return {
        "account_id": account["account_id"],
        "account_name": account["name"],
        "score": a["score"],
        "band": a["band"],
        "abstained": a["abstained"],
        "drivers": a["drivers"],
        "rationale": a["rationale"],
        "citations": a["citations"],
        "leakage_violations": result.leakage_violations,
    }


def answer_bulk_window(period: str, max_accounts: int = 10, max_workers: int = 3) -> dict:
    """Screens every account renewing within the calendar window for `period`
    through the REAL pipeline — real cost, real time, per account.

    Finding which accounts are in the window (prescreen.list_renewing_accounts)
    stays free/unrelated. Each matching account then gets a full
    agent.run_account() call. The underlying Salesforce MCP connection
    serializes all reads through one lock (see salesforce_client.py) — so
    concurrency here doesn't speed up the Salesforce-query portion of each
    account, but the 4 real Anthropic calls per account (Coordinator, 2
    specialists, Synthesis) run genuinely in parallel across threads, which is
    where most of each account's ~15-30s actually goes. max_accounts caps
    real spend/latency (~$0.32/account placeholder rate) for one question."""
    start, end = resolve_window(period)
    client = salesforce_client.get_client()
    accounts = prescreen.list_renewing_accounts(end.isoformat(), start.isoformat(), client)
    total_matching = len(accounts)
    capped_accounts = accounts[:max_accounts]
    today = date.today().isoformat()

    def _run(acct: dict) -> dict | None:
        try:
            result = agent.run_account(acct["account_id"], today, client=client)
        except Exception as exc:  # noqa: BLE001 - Salesforce or Anthropic error; skip, keep going
            print(f"SKIPPED {acct['account_id']} ({acct['name']}): {exc}", file=sys.stderr)
            return None
        agent.write_brief(result, acct["name"])
        agent.append_briefs_csv(result, acct["name"])
        a = result.assessment
        return {
            **acct,
            "score": a["score"], "band": a["band"], "abstained": a["abstained"],
            "drivers": a["drivers"], "rationale": a["rationale"],
        }

    results = []
    if max_workers <= 1:
        for acct in capped_accounts:
            r = _run(acct)
            if r is not None:
                results.append(r)
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_run, acct): acct for acct in capped_accounts}
            for future in as_completed(futures):
                r = future.result()
                if r is not None:
                    results.append(r)

    results.sort(key=lambda r: -r["score"])
    return {
        "period": period,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "total_matching": total_matching,
        "screened_count": len(results),
        "capped": total_matching > max_accounts,
        "estimated_cost_usd": round(len(results) * 0.32, 2),
        "results": results,
    }
