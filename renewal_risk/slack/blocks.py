"""
blocks.py — Block Kit rendering. Pure functions over the service's answer
dicts, so they are testable without Slack.
"""
from __future__ import annotations

import json

BAND_EMOJI = {"high": ":red_circle:", "medium": ":large_orange_circle:", "low": ":large_green_circle:"}
SEV_EMOJI = {"high": ":red_circle:", "medium": ":large_orange_circle:", "low": ":white_circle:"}
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


def assessment_blocks(view: dict, show_actions_buttons: bool = True) -> list[dict]:
    band = view["band"]
    header = f"{BAND_EMOJI.get(band, '')} *{_escape(view['account_name'])}* — *{band.upper()} risk* ({view['score']}/100)"
    if view.get("abstained"):
        header += "  _low confidence: not enough data_"
    blocks = [section(header), section(_escape(view["rationale"]))]
    if view.get("drivers"):
        blocks.append(section("*Drivers*\n" + "\n".join(f"• {_escape(d)}" for d in view["drivers"][:5])))
    actions = view.get("recommended_actions") or []
    if actions:
        lines = [f"• *{_escape(a['action'])}* — {_escape(a['owner_role'])}, {a['priority']} priority, within {a['due_in_days']} days" for a in actions[:4]]
        blocks.append(section("*Recommended actions*\n" + "\n".join(lines)))
    meta = [f"As of {view['as_of']}"]
    if view.get("from_cache"):
        meta.append("stored assessment")
    sources = sorted({s["evidence_ref"].split(":", 1)[0] for s in view.get("signals", []) if ":" in s.get("evidence_ref", "")})
    if sources:
        meta.append("evidence from " + ", ".join(sources))
    blocks.append(context(" · ".join(meta)))
    if show_actions_buttons:
        value = json.dumps({"account_id": view["account_id"]})
        blocks.append({
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "View evidence"}, "action_id": "rr_view_evidence", "value": value},
                {"type": "button", "text": {"type": "plain_text", "text": "Re-run now"}, "action_id": "rr_rerun", "value": value},
                {"type": "button", "text": {"type": "plain_text", "text": ":+1: Right"}, "action_id": "rr_feedback_agree",
                 "value": json.dumps({"account_id": view["account_id"], "assessment_id": view.get("assessment_id")})},
                {"type": "button", "text": {"type": "plain_text", "text": ":-1: Wrong"}, "action_id": "rr_feedback_disagree",
                 "value": json.dumps({"account_id": view["account_id"], "assessment_id": view.get("assessment_id")})},
            ],
        })
    return blocks


def evidence_modal(view: dict) -> dict:
    blocks = []
    for s in view.get("signals", [])[:30]:
        blocks.append(section(f"{SEV_EMOJI.get(s['severity'], '')} *{_escape(s['name'])}* ({s['severity']}, {s.get('specialist', '')})\n"
                              f"{_escape(s['value'])}\n`{_escape(s['evidence_ref'])}`"
                              + (f"\n_How it's calculated:_ {_escape(s['how_calculated'])}" if s.get("how_calculated") else "")))
    if not blocks:
        blocks.append(section("No signals were found."))
    gaps = view.get("gaps") or []
    if gaps:
        blocks.append({"type": "divider"})
        blocks.append(section("*Not assessed / data gaps*\n" + "\n".join(f"• {_escape(g)}" for g in gaps[:15])))
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "Risk evidence"[:24]},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": blocks[:100],
    }


def portfolio_blocks(result: dict, terms: dict | None = None) -> list[dict]:
    rows = result["rows"]
    noun = cap((terms or {}).get("renewals", "renewals"))
    head = (f"*{noun} {_escape(result['label'])}* ({result['window_start']} – {result['window_end']}): "
            f"{result['renewals_in_window']} in window, {len(rows)} shown")
    blocks = [section(head)]
    if not rows:
        blocks.append(section("No matching renewals in this window."))
    for row in rows[:15]:
        score = f" ({row['score']}/100)" if row.get("score") is not None else ""
        source = "" if row.get("band_source") == "assessment" else "  _screen only_"
        why = ""
        if row.get("assessment"):
            why = (row["assessment"].get("drivers") or [""])[0]
        elif row.get("screen"):
            why = "; ".join(s["value"] for s in row["screen"]["signals"][:2])
        amount = f" · {row['amount']:,.0f}" if isinstance(row.get("amount"), (int, float)) else ""
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": _clip(
                f"{BAND_EMOJI.get(row.get('band'), ':white_circle:')} *{_escape(row['name'])}* — {row.get('band') or 'n/a'}{score}{source}\n"
                f"Renews {row['close_date']}{amount}" + (f"\n_{_escape(why)}_" if why else ""))},
            "accessory": {"type": "button", "text": {"type": "plain_text", "text": "Details"}, "action_id": "rr_details",
                          "value": json.dumps({"account_id": row["account_id"]})},
        })
    if len(rows) > 15:
        blocks.append(context(f"…and {len(rows) - 15} more. Use the API or a narrower window for the full list."))
    notes = []
    if result.get("assessed_now"):
        notes.append(f"{result['assessed_now']} assessed just now (${result.get('cost_usd', 0):.2f})")
    if result.get("not_assessed_flagged"):
        notes.append(f"{result['not_assessed_flagged']} more flagged accounts screened only (per-question cap)")
    if notes:
        blocks.append(context(" · ".join(notes)))
    return blocks[:50]


def alert_blocks(view: dict, previous_band: str | None, terms: dict | None = None) -> list[dict]:
    change = f"{previous_band} → {view['band']}" if previous_band else f"new: {view['band']}"
    noun = cap((terms or {}).get("renewal", "renewal"))
    blocks = [section(f":rotating_light: *{noun} risk alert* — {change}")]
    return blocks + assessment_blocks(view)


def message_blocks(text: str) -> list[dict]:
    return [section(_escape(text))]


def answer_blocks(answer: dict, terms: dict | None = None) -> tuple[str, list[dict]]:
    """-> (fallback text, blocks) for any service.ask() answer. `terms` is the
    business-type pack's wording (e.g. "SOW renewals" for IT services)."""
    kind = answer.get("type")
    if kind == "single_account":
        return f"{answer['account_name']}: {answer['band']} risk ({answer['score']}/100)", assessment_blocks(answer)
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
    head = f"*{_escape(action['action'])}*\nPriority {action['priority']} · due in {action['due_in_days']} days"
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
        blocks.append(context(f"{SEV_EMOJI.get(s['severity'], '')} {_escape(s['value'])}"
                              + (f" — _{_escape(s['how_calculated'])}_" if s.get("how_calculated") else "")))
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
