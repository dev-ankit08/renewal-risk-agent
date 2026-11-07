"""
scheduler.py — the nightly scan and proactive alerts.

  1. list open renewals closing within `schedule.lookahead_days`
  2. free screen every one of them
  3. full (paid) assessment for the flagged ones — or all / none, per
     `schedule.deep_assess` — up to `schedule.max_deep_assessments`
  4. when an account's band rises to `slack.alerts.min_band` or above, alert
     once (channel + DMs); the alert state resets when it drops back below
  5. every quick check is saved as a prediction; renewals that closed since the
     last run are compared with what was predicted, and posted once to the channel

Run inside the service (`renewal-risk serve` with schedule.enabled: true), or
once from any external scheduler: `renewal-risk scan`.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, timedelta

from ..core import backtest
from ..core.connector import ConnectorError
from ..core.settings import deep_merge
from .context import AppContext
from .risk_service import BAND_ORDER, assessment_view

FOLLOWUP_DAYS = 60  # renewals that closed this recently are checked against their saved predictions


def scan(ctx: AppContext, notifier=None, dry_run: bool = False) -> dict:
    cfg = ctx.settings.agent.get("schedule") or {}
    min_band = ((ctx.settings.slack or {}).get("alerts") or {}).get("min_band", "high")
    today = date.today()
    renewals = ctx.engine.list_renewals((today + timedelta(days=int(cfg.get("lookahead_days", 120)))).isoformat(), today.isoformat())
    mode = cfg.get("deep_assess", "flagged")
    cap = int(cfg.get("max_deep_assessments", 25))
    summary = {"renewals": len(renewals), "screened": 0, "assessed": 0, "alerts": [], "errors": [], "cost_usd": 0.0}

    candidates = []
    for row in renewals:
        try:
            screen = ctx.engine.prescreen(row["account_id"], today.isoformat())
            if not dry_run:
                ctx.service.record_screen(row, screen, "scan")
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"{row['account_id']}: screen failed: {exc}")
            continue
        summary["screened"] += 1
        if mode == "all" or (mode == "flagged" and screen["flag"]):
            candidates.append((row, screen))
    candidates.sort(key=lambda c: (-c[1]["high_count"], -c[1]["medium_count"], c[0]["close_date"]))

    for row, _screen in candidates[:cap]:
        account_id = row["account_id"]
        started = time.time()
        try:
            result = ctx.engine.assess(account_id, account_name=row.get("name"))
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"{account_id}: assessment failed: {exc}")
            continue
        summary["assessed"] += 1
        summary["cost_usd"] += result.cost_usd
        view = assessment_view(result.to_dict()) if dry_run else ctx.service.after_assessment(result, getattr(notifier, "_dm", None))
        band = result.assessment["band"]
        if result.assessment["abstained"]:
            continue  # not enough data to judge: never alert on it
        previous = ctx.store.previous_band(account_id, before=started)
        last_alert = ctx.store.last_alert(account_id)
        if BAND_ORDER[band] < BAND_ORDER[min_band]:
            if last_alert:
                ctx.store.clear_alert(account_id)  # dropped back below: a later rise alerts again
            continue
        if last_alert and BAND_ORDER[last_alert["band"]] >= BAND_ORDER[band]:
            continue  # already alerted at this level or higher
        alert = {"account_id": account_id, "name": row.get("name"), "band": band, "previous_band": previous}
        if not dry_run and notifier is not None:
            account = ctx.service.account(account_id) or {"id": account_id}
            alert["delivered_to"] = notifier.send_alert(view, account, previous)
        if not dry_run:
            ctx.store.record_alert(account_id, band, result.assessment["score"])
        summary["alerts"].append(alert)
    summary["cost_usd"] = round(summary["cost_usd"], 4)
    if not dry_run:
        summary["outcomes"] = outcome_followup(ctx, notifier, today)
    return summary


def outcome_followup(ctx: AppContext, notifier=None, today: date | None = None) -> dict | None:
    """How the saved predictions turned out for renewals that closed recently — once per renewal,
    posted to the alert channel. None when nothing new closed with a prediction behind it."""
    today = today or date.today()
    seen_path = ctx.settings.state_dir / "outcomes_reported.json"
    seen = set(json.loads(seen_path.read_text(encoding="utf-8"))) if seen_path.exists() else set()
    try:
        closed = ctx.engine.list_renewals(today.isoformat(), (today - timedelta(days=FOLLOWUP_DAYS)).isoformat(), closed=True)
    except ConnectorError:
        return None
    new = [c for c in closed if c["renewal_ref"] not in seen]
    saved = ctx.store.predictions_for([c["renewal_ref"] for c in new])
    cfg = deep_merge(backtest.DEFAULTS, ctx.settings.agent.get("backtest") or {})
    outcomes_csv = ctx.settings.dir / "outcomes.csv"
    uploaded = backtest.read_outcomes(outcomes_csv.read_text(encoding="utf-8")) if outcomes_csv.exists() else {}
    counts, lines = {"caught": 0, "missed": 0, "false_alarms": 0, "quiet_correct": 0}, []
    for c in new:
        preds = [p for p in saved.get(c["renewal_ref"], []) if p["predicted_on"] < c["close_date"][:10]]
        outcome = backtest.outcome_of(c, None, cfg, uploaded) if preds else None
        if not outcome:
            continue  # no prediction behind it, or the outcome is not known yet (checked again tomorrow)
        seen.add(c["renewal_ref"])
        flagged = [p for p in preds if p["enough"] and p["band"] in ("medium", "high")]
        lost = outcome in backtest.POSITIVE
        if lost and flagged:
            counts["caught"] += 1
            ahead = (date.fromisoformat(c["close_date"][:10]) - date.fromisoformat(flagged[0]["predicted_on"])).days
            lines.append(f"• {c['name']}: {outcome}, flagged {ahead} days ahead")
        elif lost:
            counts["missed"] += 1
            lines.append(f"• {c['name']}: {outcome}, not flagged")
        elif flagged:
            counts["false_alarms"] += 1
            lines.append(f"• {c['name']}: renewed, but was flagged {flagged[-1]['band']} risk")
        else:
            counts["quiet_correct"] += 1
    seen_path.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    total = sum(counts.values())
    if not total:
        return None
    losses = counts["caught"] + counts["missed"]
    text = (f"How earlier predictions turned out: {total} renewal(s) closed. {counts['caught']} of {losses} loss(es) were flagged "
            f"in advance; {counts['false_alarms']} false alarm(s).\n" + "\n".join(lines[:10])
            + "\nAdmin > Accuracy suggests thresholds from these outcomes.")
    posted = bool(notifier is not None and hasattr(notifier, "post_channel") and notifier.post_channel(text))
    return {**counts, "renewals": total, "posted": posted, "text": text}


def start_background(ctx: AppContext, notifier=None):
    cfg = ctx.settings.agent.get("schedule") or {}
    if not cfg.get("enabled"):
        return None
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    def job():
        try:
            from .context import get_context
            result = scan(get_context(), notifier)  # the live configuration, not the one at start-up
            print(f"scheduled scan: {result['screened']} screened, {result['assessed']} assessed, "
                  f"{len(result['alerts'])} alert(s), ${result['cost_usd']}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - never kill the scheduler thread
            print(f"scheduled scan failed: {exc}", file=sys.stderr)

    scheduler = BackgroundScheduler()
    scheduler.add_job(job, CronTrigger.from_crontab(cfg.get("cron", "0 6 * * 1-5")), max_instances=1, coalesce=True)
    scheduler.start()
    return scheduler
