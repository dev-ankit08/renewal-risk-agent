"""
scheduler.py — the nightly scan and proactive alerts.

  1. list open renewals closing within `schedule.lookahead_days`
  2. free screen every one of them
  3. full (paid) assessment for the flagged ones — or all / none, per
     `schedule.deep_assess` — up to `schedule.max_deep_assessments`
  4. when an account's band rises to `slack.alerts.min_band` or above, alert
     once (channel + DMs); the alert state resets when it drops back below

Run inside the service (`renewal-risk serve` with schedule.enabled: true), or
once from any external scheduler: `renewal-risk scan`.
"""
from __future__ import annotations

import sys
import time
from datetime import date, timedelta

from .context import AppContext
from .risk_service import BAND_ORDER, assessment_view


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
    return summary


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
