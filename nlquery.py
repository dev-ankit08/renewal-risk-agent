"""
nlquery.py — plain-English question parsing and answer orchestration for the
web UI (webapp.py). No Anthropic call, no NLP library — regex-based on
purpose, since intent parsing must stay in the free tier and be instant.

Two question shapes are recognized:
  "is <company> at risk"                    -> single_account
  "accounts at risk this quarter/half/year"  -> bulk_window

Everything here calls through prescreen.py's existing, already-guarded
functions — this module builds no SOQL itself.
"""
from __future__ import annotations

import calendar
import re
from datetime import date

import prescreen

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
    """Resolves a company name to an Account, then runs the free heuristic
    screen. Raises prescreen.AccountLookupError on 0/ambiguous matches."""
    account = prescreen.resolve_account_by_name(company_name, client)
    today = date.today().isoformat()
    result = prescreen.prescreen_account(account["account_id"], today, client)
    result["account_id"] = account["account_id"]
    result["account_name"] = account["name"]
    return result


def answer_bulk_window(period: str, max_accounts: int = 15, max_workers: int = 6) -> dict:
    """Screens accounts renewing within the calendar window for `period`.

    Each account needs ~9 sequential `sf` CLI subprocess calls (one per
    Salesforce object plus a join lookup), and each call has real, fixed
    overhead (confirmed live: ~3-4s, dominated by CLI/Node startup, not data
    size) — screening is genuinely slow at scale, not something a bigger
    thread pool fully solves. Defaults tuned from an observed live run: 25
    accounts at max_workers=4 took 4m29s; max_accounts=15/max_workers=6 is a
    deliberate trade-off between wait time and how many results a "this
    year" style query actually returns, not an arbitrary guess."""
    start, end = resolve_window(period)
    total_matching = len(prescreen.list_renewing_accounts(end.isoformat(), start.isoformat()))
    results = prescreen.prescreen_bulk(
        before_date=end.isoformat(),
        after_date=start.isoformat(),
        max_accounts=max_accounts,
        max_workers=max_workers,
    )
    return {
        "period": period,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "total_matching": total_matching,
        "screened_count": len(results),
        "capped": total_matching > max_accounts,
        "results": results,
    }
