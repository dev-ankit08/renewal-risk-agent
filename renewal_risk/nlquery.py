"""
nlquery.py — plain-English questions -> an intent.

Regex first (instant, free). When nothing matches and an LLM is available,
one small structured call classifies the question. Two intents:

  ("single_account", {"company_name": ...})
  ("bulk_window", {"start": date, "end": date, "label": str, "band_filter": any|medium_or_high|high})

Examples that resolve without the LLM:
  "Acme"                               (a bare name — the Slack slash-command case)
  "is Acme Corp at risk"  "why is Globex high risk"  "risk for Initech"  "how risky is Umbrella"
  "accounts at risk this quarter"  "renewals at risk next 90 days"  "high risk renewals in Q4"
  "at-risk before 2026-12-31"  "which renewals next month"
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from .core import prompts, schemas


class IntentParseError(Exception):
    pass


HELP_TEXT = (
    "Ask about one company — e.g. `Acme`, `is Acme at risk`, `why is Acme high risk` — or a renewal window — "
    "e.g. `at risk this quarter`, `high risk renewals next 90 days`, `renewals in Q1 2027`, `at risk before 2026-12-31`."
)

_SINGLE = [
    re.compile(r"\b(?:is|are)\s+['\"]?(.+?)['\"]?\s+(?:at[\s-]risk|a\s+risk|risky|likely to churn|going to churn|safe)\b", re.I),
    re.compile(r"\bwhy\s+is\s+['\"]?(.+?)['\"]?\s+(?:(?:high|medium|low)[\s-]risk|at[\s-]risk|flagged|risky)\b", re.I),
    re.compile(r"\bhow\s+risky\s+is\s+['\"]?(.+?)['\"]?\s*[?.!]*$", re.I),
    re.compile(r"\b(?:risk|status|health)\s+(?:for|on|with|of)\s+['\"]?(.+?)['\"]?\s*[?.!]*$", re.I),
    re.compile(r"^\s*(?:assess|check|analy[sz]e|score)\s+['\"]?(.+?)['\"]?\s*[?.!]*$", re.I),
]

_BULK_WORDS = re.compile(r"\b(accounts|renewals|customers|portfolio|which|list|all|top)\b", re.I)
_BAND_HIGH = re.compile(r"\bhigh[\s-]risk\b", re.I)
_AT_RISK = re.compile(r"\b(at[\s-]risk|risky|risk)\b", re.I)


def _quarter_bounds(year: int, q: int) -> tuple[date, date]:
    start_month = (q - 1) * 3 + 1
    end_month = start_month + 2
    return date(year, start_month, 1), date(year, end_month, calendar.monthrange(year, end_month)[1])


def parse_window(text: str, today: date) -> tuple[date, date, str] | None:
    t = text.lower()
    m = re.search(r"\b(?:this|current)\s+(quarter|qtr|quater|half|half[\s-]?year|year|fiscal year|month)\b", t)
    if m:
        unit = m.group(1)
        if unit in ("quarter", "qtr", "quater"):
            s, e = _quarter_bounds(today.year, (today.month - 1) // 3 + 1)
            return s, e, "this quarter"
        if unit.startswith("half"):
            s, e = (date(today.year, 1, 1), date(today.year, 6, 30)) if today.month <= 6 else (date(today.year, 7, 1), date(today.year, 12, 31))
            return s, e, "this half"
        if unit == "month":
            return date(today.year, today.month, 1), date(today.year, today.month, calendar.monthrange(today.year, today.month)[1]), "this month"
        return date(today.year, 1, 1), date(today.year, 12, 31), "this year"
    m = re.search(r"\bnext\s+(quarter|month|year)\b", t)
    if m:
        if m.group(1) == "quarter":
            q = (today.month - 1) // 3 + 2
            year = today.year + (1 if q > 4 else 0)
            s, e = _quarter_bounds(year, q - 4 if q > 4 else q)
            return s, e, "next quarter"
        if m.group(1) == "month":
            first = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
            return first, first.replace(day=calendar.monthrange(first.year, first.month)[1]), "next month"
        return date(today.year + 1, 1, 1), date(today.year + 1, 12, 31), "next year"
    m = re.search(r"\b(?:next|within|in the next)\s+(\d{1,3})\s+(day|week|month)s?\b", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = n * {"day": 1, "week": 7, "month": 30}[unit]
        return today, today + timedelta(days=days), f"next {n} {unit}{'s' if n != 1 else ''}"
    m = re.search(r"\bq([1-4])(?:\s*(?:fy)?\s*'?(\d{2,4}))?\b", t)
    if m:
        year = int(m.group(2)) if m.group(2) else today.year
        year = year + 2000 if year < 100 else year
        s, e = _quarter_bounds(year, int(m.group(1)))
        return s, e, f"Q{m.group(1)} {year}"
    m = re.search(r"\b(?:before|by|until|through)\s+(\d{4}-\d{2}-\d{2})\b", t)
    if m:
        return today, date.fromisoformat(m.group(1)), f"before {m.group(1)}"
    return None


def parse_intent(text: str, today: date | None = None, llm=None) -> tuple[str, dict]:
    today = today or date.today()
    text = (text or "").strip().strip("`")
    if not text or text.lower() in ("help", "?"):
        raise IntentParseError(HELP_TEXT)

    band = "high" if _BAND_HIGH.search(text) else "medium_or_high" if _AT_RISK.search(text) else "any"
    window = parse_window(text, today)
    if window and (_BULK_WORDS.search(text) or _AT_RISK.search(text) or len(text.split()) <= 4):
        start, end, label = window
        return "bulk_window", {"start": start, "end": end, "label": label, "band_filter": band}

    for pattern in _SINGLE:
        m = pattern.search(text)
        if m:
            name = m.group(1).strip(" '\"?.!")
            if name and not _BULK_WORDS.fullmatch(name):
                return "single_account", {"company_name": name}

    # A short phrase with no question words is a bare company name: `/renewal-risk Acme Corp`.
    if len(text.split()) <= 5 and not re.search(r"\b(what|which|why|how|when|who|show|list|risk)\b", text, re.I):
        return "single_account", {"company_name": text.strip(" ?.!")}

    if llm is not None:
        system, user = prompts.intent_prompt(text, today.isoformat())
        data = llm.structured("utility", system, user, schemas.INTENT_SCHEMA)
        if data["intent"] == "single_account" and data["company_name"].strip():
            return "single_account", {"company_name": data["company_name"].strip()}
        if data["intent"] == "bulk_window":
            try:
                start, end = date.fromisoformat(data["window_start"]), date.fromisoformat(data["window_end"])
            except ValueError:
                start, end = today, today + timedelta(days=90)
            return "bulk_window", {"start": start, "end": end, "label": f"{start} to {end}", "band_filter": data["band_filter"]}
    raise IntentParseError("I couldn't tell which company or renewal window you mean. " + HELP_TEXT)
