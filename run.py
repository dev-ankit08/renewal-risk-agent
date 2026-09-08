#!/usr/bin/env python3
"""
run.py — CLI entry point.

    python3 run.py --account 0018c00002LmNqRAAZ --as-of 2026-09-08   # full real pipeline, one account
    python3 run.py --golden golden_set.csv --concurrency 4             # full real pipeline, batch
    python3 run.py prescreen --renewals-before 2026-12-31              # free heuristic bulk screen, no Anthropic call
    python3 run.py prescreen --account 0018c00002LmNqRAAZ              # free heuristic check, one account

--account/--golden run the full Coordinator->Specialists->Synthesis pipeline
against real Anthropic + real Salesforce (see config.py for required setup)
and produce a genuine, explained risk assessment. `prescreen` is a free,
instant, heuristic-only screen (heuristics.py) meant to narrow down which
accounts are worth spending a real pipeline run on — it never calls Anthropic.
"""
from __future__ import annotations

import argparse
import csv
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import agent
import config
import prescreen

# Some Windows terminals default stdout/stderr to the legacy console codepage
# (e.g. cp1252) instead of UTF-8, which silently mis-encodes non-ASCII output
# (em-dashes, the leakage-violation warning glyph in agent.render_brief) and
# would raise UnicodeEncodeError on characters outside that codepage. Force
# UTF-8 explicitly rather than depending on the environment's default.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _run_one(account_id: str, as_of: str, account_name: str | None = None) -> agent.RunResult:
    result = agent.run_account(account_id, as_of)
    agent.write_brief(result, account_name)
    agent.append_briefs_csv(result, account_name)
    return result


def _print_summary(results: list[agent.RunResult]) -> None:
    print(f"\n{'account_id':<22} {'score':>6} {'band':<8} {'abstained':<10} {'leakage':<8}")
    for r in results:
        a = r.assessment
        print(f"{r.account_id:<22} {a['score']:>6} {a['band']:<8} {str(a['abstained']):<10} {len(r.leakage_violations):<8}")


def run_golden(csv_path: str, concurrency: int) -> list[agent.RunResult]:
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    results = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(_run_one, row["account_id"], row["as_of"], row.get("name")): row
            for row in rows
        }
        for future in as_completed(futures):
            row = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - surface per-account failure, keep going
                print(f"FAILED {row['account_id']}: {exc}", file=sys.stderr)
    return results


def _handle_prescreen(args) -> int:
    if args.renewals_before:
        results = prescreen.prescreen_bulk(args.renewals_before)
        if not results:
            print(f"No open renewals found on or before {args.renewals_before}.")
            return 0
        print(f"\n{'account_id':<20} {'name':<32} {'close_date':<12} {'flag':<6} signals")
        for r in results:
            signal_names = ", ".join(s["name"] for s in r["signals"]) or "none"
            print(f"{r['account_id']:<20} {r.get('name', ''):<32} {r.get('close_date') or '':<12} {str(r['flag']):<6} {signal_names}")
        flagged = sum(1 for r in results if r["flag"])
        print(f"\n{flagged} of {len(results)} flagged (free heuristic screen — no explanation, no Anthropic call).")
        print(f"For a full, explained risk assessment on any account, run:\n  py run.py --account <id> --as-of {date.today().isoformat()}")
        return 0
    elif args.account:
        as_of = date.today().isoformat()
        result = prescreen.prescreen_account(args.account, as_of)
        status = "FLAGGED" if result["flag"] else "no flags"
        print(f"\n{args.account} — {status} ({result['high_severity_count']} high, {result['medium_severity_count']} medium)")
        for s in result["signals"]:
            print(f"  - {s['name']} ({s['severity']}): {s['value']}")
        if result["gaps"]:
            print("  Gaps: " + "; ".join(result["gaps"]))
        print(f"\nThis is a free heuristic screen, not a full explanation. For a real, explained risk assessment, run:\n  py run.py --account {args.account} --as-of {as_of}")
        return 0
    else:
        print("prescreen requires --renewals-before or --account", file=sys.stderr)
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Renewal Risk Agent — run a renewal-risk pipeline for one or more accounts.")
    subparsers = parser.add_subparsers(dest="command")

    prescreen_parser = subparsers.add_parser("prescreen", help="Free heuristic risk screen — no Anthropic call.")
    prescreen_parser.add_argument("--renewals-before", help="List and screen accounts with an open Opportunity renewing on/before this date (YYYY-MM-DD).")
    prescreen_parser.add_argument("--account", help="Screen a single account Id.")

    parser.add_argument("--account", help="Single Salesforce account Id to run through the full real pipeline.")
    parser.add_argument("--as-of", help="As-of date (YYYY-MM-DD). Required with --account.")
    parser.add_argument("--golden", help="Path to a golden-set CSV (account_id,name,arr,renewal_date,as_of,outcome).")
    parser.add_argument("--concurrency", type=int, default=4, help="Max parallel accounts for --golden runs.")
    args = parser.parse_args()

    if args.command == "prescreen":
        return _handle_prescreen(args)

    if args.golden:
        results = run_golden(args.golden, args.concurrency)
    elif args.account:
        if not args.as_of:
            parser.error("--as-of is required with --account")
        results = [_run_one(args.account, args.as_of)]
    else:
        parser.error("specify --account (with --as-of), --golden, or the prescreen subcommand")
        return 2

    _print_summary(results)
    print(f"\nBriefs written to {config.BRIEFS_DIR}/, rollup in {config.BRIEFS_CSV}, ledger in {config.RUNS_DB}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
