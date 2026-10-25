"""
blocks.py — Block Kit rendering. Pure functions over the service's answer
dicts, so they are testable without Slack.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from ..core.signals import label

BAND_EMOJI = {"high": ":red_circle:", "medium": ":large_orange_circle:", "low": ":large_green_circle:"}
SEV_EMOJI = {"high": ":red_circle:", "medium": ":large_orange_circle:", "low": ":white_circle:"}
PRIORITY = {"high": ":small_red_triangle: High priority", "medium": ":small_orange_diamond: Medium priority",
            "low": ":small_blue_diamond: Low priority"}
MAX_TEXT = 2900  # section text limit is 3000 chars


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def cap(text: str) -> str:
    """Upper-case the first letter only ("SOW renewals" stays "SOW renewals")."""
    return text[:1].upper() + text[1:]


def _escape(text) -> str:
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _clip(text)}}


def context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": _clip(text, 1900)}]}


# ---- plain-language helpers -------------------------------------------------

_GAP = re.compile(r"^(\w+) not evaluated — (.+)$")


def _date(value, with_year: bool = True) -> str:
    try:
        d = date.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value or "")
    return f"{d.day} {d:%b %Y}" if with_year else f"{d.day} {d:%b}"


def _due(as_of: str, days) -> str:
    try:
        return _date((date.fromisoformat(str(as_of)[:10]) + timedelta(days=int(days))).isoformat(), with_year=False)
    except (TypeError, ValueError):
        return f"in {days} days"


def _source(ref: str, sources: dict | None) -> str:
    """crm:renewal/R001 -> "renewal R001 in Salesforce"."""
    system, _, rest = str(ref or "").partition(":")
    entity, _, rid = rest.partition("/")
    return f"{entity.replace('_', ' ')} {rid} in {(sources or {}).get(system) or label(system)}".strip()


def _gap(text: str) -> str | None:
    """A data gap in plain words; None for internal notes the reader cannot act on."""
    if text.startswith("dropped signal"):
        return None
    m = _GAP.match(text)
    return f"*{label(m.group(1))}* — {_escape(m.group(2))}" if m else _escape(text.replace("_", " "))


def _score_bar(score) -> str:
    filled = max(0, min(10, round(float(score or 0) / 10)))
    return "▰" * filled + "▱" * (10 - filled)


def _signal_line(s: dict) -> str:
    return f"{SEV_EMOJI.get(s['severity'], '')} *{label(s['name'])}* — {_escape(s['value'])}"


def _action_lines(view: dict, with_why: bool = False) -> list[str]:
    lines = []
    for i, a in enumerate((view.get("recommended_actions") or [])[:4], 1):
        line = (f"*{i}. {_escape(a['action'])}*\n"
                f"{PRIORITY.get(a['priority'], a['priority'])}  ·  :bust_in_silhouette: {_escape(a['owner_role'])}"
                f"  ·  :calendar: by {_due(view['as_of'], a['due_in_days'])}")
        if with_why and a.get("why"):
            line += f"\n_Why: {_escape(a['why'])}_"
        lines.append(line)
    return lines


def _summary_fields(view: dict) -> dict:
    score = "—  not scored" if view.get("abstained") else f"{_score_bar(view['score'])}  {view['score']:.0f}/100"
    fields = [f"*Risk score*\n{score}", f"*Checked on*\n{_date(view['as_of'])}"]
    cov = view.get("coverage") or {}
    if cov.get("total"):
        fields.append(f"*Warning signs checked*\n{cov['checked']} of {cov['total']}" + ("" if cov.get("enough", True) else " — not enough data"))
    return {"type": "section", "fields": [{"type": "mrkdwn", "text": f} for f in fields]}


def verdict(view: dict) -> str:
    """The one wording of an account's result: never "Low risk" when too little could be checked."""
    return "Not enough data" if view.get("abstained") else f"{cap(view['band'])} risk"


def _header(view: dict) -> dict:
    emoji = ":white_circle:" if view.get("abstained") else BAND_EMOJI.get(view["band"], "")
    return {"type": "header", "text": {"type": "plain_text", "emoji": True, "text": _clip(f"{emoji} {view['account_name']} — {verdict(view)}", 150)}}


# ---- answers ---------------------------------------------------------------

def assessment_blocks(view: dict, show_actions_buttons: bool = True, sources: dict | None = None) -> list[dict]:
    """One account, at a glance: band, score, why, the top warning signs, what to do next."""
    blocks = [_header(view), _summary_fields(view), section(f"*Summary*\n{_escape(view['rationale'])}")]
    signals = view.get("signals") or []
    if signals:
        more = f"\n_…and {len(signals) - 3} more in the details_" if len(signals) > 3 else ""
        blocks.append(section("*Top warning signs*\n" + "\n".join(_signal_line(s) for s in signals[:3]) + more))
    else:
        note = "\n_Some checks could not run for lack of data — see the details._" if any(_gap(g) for g in view.get("gaps") or []) else ""
        blocks.append(section(":white_check_mark: *No warning signs found* in the data available." + note))
    actions = _action_lines(view)
    if actions:
        blocks.append(section("*What to do next*\n" + "\n".join(actions)))
    meta = []
    systems = sorted({(sources or {}).get(r.split(":", 1)[0]) or label(r.split(":", 1)[0])
                      for r in (s.get("evidence_ref", "") for s in signals) if ":" in r})
    if systems:
        meta.append("Based on " + ", ".join(systems))
    if view.get("mode") == "rules":
        meta.append("Rules-only check (AI off)")
    if view.get("from_cache"):
        meta.append("Saved result — press Refresh for a new check")
    if meta:
        blocks.append(context("  ·  ".join(meta)))
    if show_actions_buttons:
        value = json.dumps({"account_id": view["account_id"]})
        vote = json.dumps({"account_id": view["account_id"], "assessment_id": view.get("assessment_id")})
        blocks.append({
            "type": "actions",
            "elements": [
                {"type": "button", "style": "primary", "text": {"type": "plain_text", "text": "See full details"}, "action_id": "rr_view_evidence", "value": value},
                {"type": "button", "text": {"type": "plain_text", "text": "Refresh"}, "action_id": "rr_rerun", "value": value},
                {"type": "button", "text": {"type": "plain_text", "text": ":+1: Accurate", "emoji": True}, "action_id": "rr_feedback_agree", "value": vote},
                {"type": "button", "text": {"type": "plain_text", "text": ":-1: Not accurate", "emoji": True}, "action_id": "rr_feedback_disagree", "value": vote},
            ],
        })
    return blocks


def evidence_modal(view: dict, sources: dict | None = None) -> dict:
    """The full picture: every warning sign by area, with what it means, how it is
    calculated and where it came from; then actions and what could not be checked."""
    blocks = [_header(view), _summary_fields(view), section(f"*Summary*\n{_escape(view['rationale'])}")]
    counts = [f"{n} {entity.replace('_', ' ')}{'' if n == 1 else 's'}" for entity, n in sorted((view.get("record_counts") or {}).items()) if n]
    blocks.append(context(":mag: Looked at " + (", ".join(counts) if counts else "no records for this account")))

    by_area: dict[str, list[dict]] = {}
    for s in view.get("signals") or []:
        by_area.setdefault(s.get("specialist") or "general", []).append(s)
    blocks.append({"type": "divider"})
    if not by_area:
        blocks.append(section(":white_check_mark: *No warning signs found* in the data available."))
    for area, items in by_area.items():
        blocks.append(section(f"*{label(area)} warning signs* ({len(items)})"))
        for s in items[:15]:
            blocks.append(section(f"{_signal_line(s)}  ·  _{cap(s['severity'])}_"))
            notes = []
            if s.get("how_calculated"):
                notes.append(f":straight_ruler: *How it's calculated:* {_escape(s['how_calculated'])}")
            if s.get("evidence_ref"):
                notes.append(f":page_facing_up: *Source:* {_escape(_source(s['evidence_ref'], sources))}")
            if notes:
                blocks.append(context("\n".join(notes)))

    actions = _action_lines(view, with_why=True)
    if actions:
        blocks += [{"type": "divider"}, section("*What to do next*\n" + "\n\n".join(actions))]
    gaps = [g for g in (_gap(g) for g in view.get("gaps") or []) if g]
    if gaps:
        blocks += [{"type": "divider"},
                   section("*What we couldn't check*\n" + "\n".join(f"• {g}" for g in gaps[:12])
                           + ("\n_Fill in this data in the source system and the check will run next time._")),
                   ]
    footer = [f"Checked on {_date(view['as_of'])}"]
    if view.get("mode") == "rules":
        footer.append("rules-only check (AI off)")
    blocks.append(context("  ·  ".join(footer)))
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "Renewal risk details"[:24]},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": blocks[:100],
    }


def portfolio_blocks(result: dict, terms: dict | None = None) -> list[dict]:
    rows = result["rows"]
    noun = cap((terms or {}).get("renewals", "renewals"))
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": _clip(f"{noun} {result['label']}", 150)}},
        context(f":calendar: {_date(result['window_start'])} – {_date(result['window_end'])}  ·  "
                f"{result['renewals_in_window']} in this period, {len(rows)} shown"),
    ]
    if not rows:
        blocks.append(section(":white_check_mark: No matching renewals in this period."))
    for row in rows[:15]:
        band = row.get("band")
        score = f" · {row['score']:.0f}/100" if row.get("score") is not None else ""
        quick = "" if row.get("band_source") == "assessment" else "  _(quick check)_"
        why = ""
        if row.get("assessment") and row["assessment"].get("signals"):
            why = label(row["assessment"]["signals"][0]["name"]) + ": " + row["assessment"]["signals"][0]["value"]
        elif row.get("screen") and row["screen"]["signals"]:
            why = "; ".join(f"{label(s['name'])}: {s['value']}" for s in row["screen"]["signals"][:2])
        amount = f"  ·  {row['amount']:,.0f}" if isinstance(row.get("amount"), (int, float)) else ""
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": _clip(
                f"{BAND_EMOJI.get(band, ':white_circle:')} *{_escape(row['name'])}* — "
                f"{cap(band) + ' risk' if band else 'Not enough data' if row.get('band_source') == 'not_enough_data' else 'not checked'}{score}{quick}\n"
                f":calendar: Renews {_date(row['close_date'])}{amount}" + (f"\n_{_escape(why)}_" if why else ""))},
            "accessory": {"type": "button", "text": {"type": "plain_text", "text": "Details"}, "action_id": "rr_details",
                          "value": json.dumps({"account_id": row["account_id"]})},
        })
    if len(rows) > 15:
        blocks.append(context(f"…and {len(rows) - 15} more. Ask about a shorter period to see them all."))
    notes = []
    if result.get("assessed_now"):
        notes.append(f"{result['assessed_now']} checked in depth just now")
    if result.get("not_assessed_flagged"):
        notes.append(f"{result['not_assessed_flagged']} more flagged accounts had a quick check only")
    if notes:
        blocks.append(context("  ·  ".join(notes)))
    return blocks[:50]


def alert_blocks(view: dict, previous_band: str | None, terms: dict | None = None, sources: dict | None = None) -> list[dict]:
    change = f"{previous_band} → {view['band']}" if previous_band else f"new: {view['band']}"
    noun = cap((terms or {}).get("renewal", "renewal"))
    blocks = [section(f":rotating_light: *{noun} risk alert* — {change}")]
    return blocks + assessment_blocks(view, sources=sources)


def message_blocks(text: str) -> list[dict]:
    return [section(_escape(text))]


def answer_blocks(answer: dict, terms: dict | None = None, sources: dict | None = None) -> tuple[str, list[dict]]:
    """-> (fallback text, blocks) for any service.ask() answer. `terms` is the
    business-type pack's wording (e.g. "SOW renewals" for IT services)."""
    kind = answer.get("type")
    if kind == "single_account":
        return f"{answer['account_name']}: {verdict(answer)}", assessment_blocks(answer, sources=sources)
    if kind == "bulk_window":
        return f"{cap((terms or {}).get('renewals', 'renewals'))} {answer['label']}", portfolio_blocks(answer, terms)
    if kind == "ambiguous":
        names = "\n".join(f"• {_escape(c['name'])}" for c in answer.get("candidates", [])[:10])
        return answer["message"], [section(f"{_escape(answer['message'])}\n{names}")]
    return answer.get("message", "Something went wrong."), message_blocks(answer.get("message", "Something went wrong."))


ACTION_STATUS = {"done": ":white_check_mark: Marked done", "snoozed": ":zzz: Snoozed", "not_relevant": ":no_entry_sign: Marked not relevant"}


def action_blocks(view: dict, action: dict, play: dict | None, item_id: int, status: str | None = None) -> list[dict]:
    """The DM an action owner receives. With `status`, the buttons are replaced
    by what the owner chose."""
    band = view["band"]
    blocks = [section(f"{BAND_EMOJI.get(band, '')} *{_escape(view['account_name'])}* is *{band.upper()} risk* ({view['score']}/100) — "
                      f"you are the owner of this action")]
    head = (f"*{_escape(action['action'])}*\n{PRIORITY.get(action['priority'], action['priority'])}"
            f"  ·  :calendar: by {_due(view['as_of'], action['due_in_days'])}")
    if action.get("why"):
        head += f"\n_Why:_ {_escape(action['why'])}"
    blocks.append(section(head))
    if play:
        extra = [f"From your playbook: *{_escape(play['name'])}*"]
        if play.get("description"):
            extra.append(_escape(play["description"]))
        extra += [f"{i}. {_escape(step)}" for i, step in enumerate(play.get("steps") or [], 1)]
        if play.get("link"):
            extra.append(f"<{play['link']}|Open the runbook>")
        blocks.append(section("\n".join(extra)))
    refs = set(action.get("evidence_refs") or [])
    evidence = [s for s in view.get("signals", []) if s["evidence_ref"] in refs][:4]
    for s in evidence:
        blocks.append(context(_signal_line(s) + (f"\n:straight_ruler: _{_escape(s['how_calculated'])}_" if s.get("how_calculated") else "")))
    if status:
        blocks.append(context(ACTION_STATUS.get(status, status)))
        return blocks
    value = json.dumps({"item_id": item_id, "account_id": view["account_id"]})
    blocks.append({"type": "actions", "elements": [
        {"type": "button", "style": "primary", "text": {"type": "plain_text", "text": "Done"}, "action_id": "rr_action_done", "value": value},
        {"type": "button", "text": {"type": "plain_text", "text": "Snooze 7 days"}, "action_id": "rr_action_snooze", "value": value},
        {"type": "button", "text": {"type": "plain_text", "text": "Not relevant"}, "action_id": "rr_action_not_relevant", "value": value},
    ]})
    return blocks
