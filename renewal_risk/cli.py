"""
cli.py — `renewal-risk` (or `python -m renewal_risk`).

Admin / setup
  presets                         list the connector catalog
  setup                           guided setup: pick systems, enter credentials, link, write config/
  setup check [--account ID]      validate config, test every source, show link coverage + active signals
  validate-config                 static config checks only
  test-connector [SOURCE]         connect to one / every source
  link --preview [--review]       build account links, show coverage, approve fuzzy matches
  mapping suggest --source S --entity E   LLM-suggested field mapping from sample records

Use
  assess ACCOUNT_ID [--as-of D]   full assessment (paid) — writes briefs/<id>.md
  assess --golden golden.csv      batch, for evaluation
  prescreen --account ID | --renewals-before D     free heuristic screen
  ask "question" --as EMAIL       answer a question as that access.yaml user
  scan [--dry-run]                one scheduled scan (+ Slack alerts unless --dry-run)
  evaluate golden.csv [--k 20]    Gate 0 / Gate 1 / fabrication check

Run
  serve [--host H --port P] [--no-slack]   REST API + Slack app + scheduler
  slack                                    Slack app only

Global: --config DIR (default ./config or RENEWAL_RISK_CONFIG_DIR)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import yaml

for _stream in (sys.stdout, sys.stderr):  # Windows consoles default to a legacy codepage
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _settings(args):
    from .core import settings as settings_mod

    if args.config:
        os.environ["RENEWAL_RISK_CONFIG_DIR"] = args.config
    return settings_mod.get_settings(reload=True)


def _ctx(args):
    from .service.context import build_context, set_context

    ctx = build_context(_settings(args))
    set_context(ctx)
    return ctx


def _ok(flag: bool) -> str:
    return "OK " if flag else "FAIL"


# ---------------------------------------------------------------------------
# presets / setup
# ---------------------------------------------------------------------------

def cmd_presets(args) -> int:
    from .core.settings import load_presets

    presets = load_presets(Path(args.config) if args.config else None)
    print(f"{'preset':<12} {'type':<11} entities / description")
    for name, p in sorted(presets.items()):
        print(f"{name:<12} {p.get('type', ''):<11} {', '.join(p.get('entities', {}))}")
        print(f"{'':<24} {p.get('description', '')}")
    print("\nGeneric types (no preset): csv, sql, rest, mcp — see examples/ for complete configs.")
    return 0


def _ask(prompt: str, default: str | None = None, required: bool = True) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        value = input(f"{prompt}{suffix}: ").strip()
        if not value and default is not None:
            return default
        if value or not required:
            return value
        print("  (required)")


def cmd_setup(args) -> int:
    from .core.settings import config_dir, load_presets

    if args.setup_action == "check":
        return cmd_setup_check(args)
    cfg_dir = Path(args.config).resolve() if args.config else config_dir()
    target = cfg_dir / "connectors.yaml"
    if target.exists() and _ask(f"{target} exists. Overwrite? (y/n)", "n").lower() != "y":
        return 1
    presets = load_presets(cfg_dir)
    print("Renewal Risk Agent setup — pick the systems your data lives in.\n")
    print("Available presets: " + ", ".join(sorted(presets)) + "  (or a generic type: csv, sql, rest, mcp)\n")
    sources: dict[str, dict] = {}
    env_lines: list[str] = []
    while True:
        choice = _ask("Add a system (preset name, or blank to finish)", required=False)
        if not choice:
            if sources:
                break
            print("  add at least one system")
            continue
        if choice not in presets and choice not in ("csv", "sql", "rest", "mcp"):
            print(f"  unknown: {choice}")
            continue
        name = _ask("  Name for this source (e.g. crm, billing, support)", {"salesforce": "crm", "hubspot": "crm", "stripe": "billing",
                                                                          "zendesk": "support", "freshdesk": "support"}.get(choice, choice))
        if choice in presets:
            entry = {"preset": choice}
            print(f"  {presets[choice].get('description', '')}")
            for line in str(presets[choice].get("setup_notes", "")).strip().splitlines():
                print(f"    {line}")
            for param, spec in (presets[choice].get("params") or {}).items():
                spec = spec or {}
                secret = any(w in param for w in ("key", "token", "secret", "password"))
                if secret:
                    env = f"{name.upper()}_{param.upper()}"
                    entry[param] = f"${{ENV:{env}}}"
                    env_lines.append(f"{env}=")
                    print(f"  {param}: stored as ${{ENV:{env}}} — put the real value in .env, never in YAML")
                else:
                    value = _ask(f"  {param} ({spec.get('description', '')})", spec.get("default"), spec.get("required", False))
                    if value:
                        entry[param] = value
        else:
            entry = {"type": choice, "entities": {}}
            print(f"  Generic '{choice}' source: fill in `entities:` in {target} (see examples/).")
        sources[name] = entry
        print(f"  added '{name}'\n")

    primary = _ask("Which source holds the master account list (and renewals)?", next(iter(sources)))
    linking = {}
    for name in sources:
        if name == primary:
            continue
        print(f"\nHow do '{name}' customers match '{primary}' accounts?")
        print("  1) they store the account id (Stripe metadata / Zendesk external_id)  2) email/web domain  "
              "3) both, then similar names for review  4) same ids already")
        pick = _ask("  choice", "3")
        linking[name] = {
            "1": [{"match": "key_account_id = account.id"}],
            "2": [{"match": "domain"}],
            "3": [{"match": "key_account_id = account.id"}, {"match": "domain"}, {"match": "fuzzy_name", "threshold": 0.88}],
            "4": [{"match": "same_id"}],
        }.get(pick, [{"match": "domain"}])

    cfg_dir.mkdir(parents=True, exist_ok=True)
    doc = {"sources": sources, "primary": primary, "linking": linking}
    target.write_text("# Written by `renewal-risk setup`. Secrets live in .env, never here.\n" + yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    for fname, body in (("access.yaml", ACCESS_TEMPLATE), ("slack.yaml", SLACK_TEMPLATE), ("agent.yaml", AGENT_TEMPLATE)):
        if not (cfg_dir / fname).exists():
            (cfg_dir / fname).write_text(body, encoding="utf-8")
    print(f"\nWrote {target} (+ access.yaml, slack.yaml, agent.yaml templates if missing).")
    if env_lines:
        print("Add these to .env with real values:\n  " + "\n  ".join(env_lines))
    print("\nNext: edit access.yaml (who sees what), then run `renewal-risk setup check`.")
    return 0


ACCESS_TEMPLATE = """# Who may see which accounts. Unknown users get nothing.
# Scope keys are standard account fields (owner_email, region, segment, portfolio, ...).
users:
  - email: admin@example.com
    role: admin                 # sees everything, can run setup checks
  # - email: pd.emea@example.com
  #   role: director
  #   scope: {region: [Germany, France, United Kingdom]}
  # - email: csm@example.com
  #   role: csm                 # no scope = only accounts they own
api_keys: []
  # - {name: dashboard, key: "${ENV:RISK_API_KEY_DASHBOARD}", as: admin@example.com}
"""

SLACK_TEMPLATE = """# Slack app. Create it from slack_manifest.yaml (repo root), install it, then set the env vars.
mode: socket
bot_token: ${ENV:SLACK_BOT_TOKEN:-}
app_token: ${ENV:SLACK_APP_TOKEN:-}
command: /renewal-risk
reply_visibility: ephemeral
alerts:
  channel: ""                   # e.g. "#renewal-risk"
  dm_owner: true
  dm_roles: [director]
  min_band: high
"""

AGENT_TEMPLATE = """# Overrides for renewal_risk/defaults/agent.yaml — only list what you change.
policy:
  zdr_confirmed: false          # set true once a Zero Data Retention agreement with Anthropic is confirmed
schedule:
  enabled: false
  cron: "0 6 * * 1-5"
  lookahead_days: 120
"""


def cmd_validate(args) -> int:
    from .core.validate import validate_settings

    settings = _settings(args)
    errors, warnings = validate_settings(settings)
    print(f"Config: {settings.dir}")
    listed = ", ".join("%s (%s)" % (n, s.get("preset") or s.get("type")) for n, s in settings.sources.items())
    print(f"Sources: {listed or 'none'}; primary: {settings.primary}")
    for e in errors:
        print(f"  ERROR   {e}")
    for w in warnings:
        print(f"  warning {w}")
    if not errors:
        print("Config is valid." + (" (with warnings)" if warnings else ""))
    return 1 if errors else 0


def cmd_test_connector(args) -> int:
    ctx = _ctx(args)
    source = getattr(args, "source", None)
    names = [source] if source else list(ctx.settings.sources)
    failed = 0
    for name in names:
        try:
            result = ctx.engine.pool.get(name).health()
            ok, detail = result.ok, result.detail
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        failed += not ok
        print(f"  [{_ok(ok)}] {name}: {detail}")
    ctx.engine.close()
    return 1 if failed else 0


def _print_links(report, linker, verbose: bool = False) -> None:
    total = len(report.account_ids)
    print(f"Accounts in primary source: {total}")
    for name, s in report.sources.items():
        if s.error:
            print(f"  [FAIL] {name}: {s.error}")
            continue
        if s.method == "same_id":
            print(f"  [OK ] {name}: uses the primary account ids directly")
            continue
        linked = sum(1 for a in report.account_ids if s.links.get(a))
        pct = (linked / total * 100) if total else 0
        print(f"  [{'OK ' if pct >= 80 else 'LOW'}] {name}: {pct:.0f}% of accounts linked ({linked}/{total}) from {s.customer_count} customers; "
              f"{len(s.review)} to review, {len(s.unmatched_customers)} customers unmatched")
        if verbose:
            for u in s.unmatched_customers[:15]:
                print(f"         unmatched: {u['id']} {u.get('name') or ''}")


def cmd_link(args) -> int:
    from .core.linking import read_manual_links, write_manual_links

    ctx = _ctx(args)
    linker = ctx.engine.linker
    report = linker.build(only=[args.source] if args.source else None)
    _print_links(report, linker, verbose=args.verbose)
    if args.review:
        rows = read_manual_links(linker.manual_path)
        decided = {(r["source"], r["account_id"], r.get("link_value")) for r in rows}
        for name, s in report.sources.items():
            for item in s.review:
                key = (name, item["account_id"], item["customer_id"])
                if key in decided:
                    continue
                score = f" (similarity {item['score']})" if item.get("score") else ""
                answer = _ask(f"[{name}] '{item.get('customer_name')}' -> account '{item.get('account_name')}'{score} — {item['reason']}. Link? (y/n/skip)", "skip")
                if answer.lower() in ("y", "n"):
                    rows.append({"source": name, "account_id": item["account_id"], "link_value": item["customer_id"],
                                 "status": "approved" if answer.lower() == "y" else "rejected", "note": item["reason"]})
        write_manual_links(linker.manual_path, rows)
        print(f"Saved decisions to {linker.manual_path}; rebuilding…")
        _print_links(linker.build(), linker)
    elif any(s.review for s in report.sources.values()):
        print("Run `renewal-risk link --review` to approve or reject the suggested matches.")
    ctx.engine.close()
    return 0


def cmd_setup_check(args) -> int:
    from .core import signals as signals_mod

    rc = cmd_validate(args)
    if rc:
        print("Fix the errors above, then re-run.")
        return rc
    print("\nConnections:")
    rc = cmd_test_connector(args)
    ctx = _ctx(args)
    print("\nAccount linking:")
    try:
        report = ctx.engine.linker.report(refresh=True)
        if report is None:
            print("  every source shares the primary account ids — nothing to link")
        else:
            _print_links(report, ctx.engine.linker)
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {type(exc).__name__}: {exc}")
        rc = 1
    available = ctx.engine.available_entities()
    print("\nSignals:")
    for sig in ctx.settings.signals:
        needed = signals_mod.required_entities(sig)
        on = bool(needed & available)
        print(f"  {'on ' if on else 'off'} {sig['name']:<24} {sig.get('specialist', ''):<13}" + ("" if on else f"(no source provides {', '.join(sorted(needed))})"))
    if args.account:
        print(f"\nSample screen for {args.account} (free, no AI):")
        screen = ctx.engine.prescreen(args.account)
        for s in screen["signals"]:
            print(f"  - {s['name']} ({s['severity']}): {s['value']}  [{s['evidence_ref']}]")
        for g in screen["gaps"]:
            print(f"  gap: {g}")
        if not screen["signals"]:
            print("  no signals fired")
    ctx.engine.close()
    return rc


def cmd_mapping(args) -> int:
    from .admin.api import _sample_link_value
    from .admin.mapping_agent import MappingAgent

    ctx = _ctx(args)
    connector = ctx.engine.pool.get(args.source)
    described = connector.describe(args.entity, link_value=_sample_link_value(ctx, args.source))
    spec = (ctx.settings.sources[args.source].get("entities") or {}).get(args.entity) or {}
    current = dict(((spec[0] if isinstance(spec, list) else spec) or {}).get("fields") or {})
    print(f"Read {described['sample_count']} sample record(s) and {len(described['fields'])} field(s); asking the model (free text never sent)…")
    result = MappingAgent(None if args.no_model else ctx.engine.llm, ctx.settings.signals).suggest(args.entity, described, current)
    fields = {s["field"]: s["expr"] for s in result["suggestions"]}
    print(f"\n# Suggested — review, then save in the admin UI or paste into {ctx.settings.dir / 'mappings' / (args.source + '.overrides.yaml')}:")
    print(yaml.safe_dump({args.entity: {"fields": fields}}, sort_keys=False, allow_unicode=True))
    for s in result["suggestions"]:
        flag = "auto" if s["auto_accept"] else "REVIEW"
        print(f"#  [{flag} {s['confidence']:.2f}] {s['field']}: {s['reason']}" + (f"  ({'; '.join(s['warnings'])})" if s["warnings"] else ""))
    if result["required_missing"]:
        print(f"#  still unmapped and required: {', '.join(result['required_missing'])}")
    for sig, missing in result["signals_affected"].items():
        print(f"#  signal {sig} needs: {', '.join(missing)}")
    ctx.engine.close()
    return 0


# ---------------------------------------------------------------------------
# use
# ---------------------------------------------------------------------------

def cmd_assess(args) -> int:
    from . import report

    ctx = _ctx(args)
    out_dir = Path(args.out)

    def one(account_id: str, as_of: str | None, name: str | None):
        account = ctx.service.account(account_id) if not name else None
        result = ctx.engine.assess(account_id, as_of=as_of, account_name=name or (account or {}).get("name"))
        data = result.to_dict()
        report.write_brief(data, out_dir)
        report.append_csv(data, out_dir / "briefs.csv")
        return result

    results = []
    if args.golden:
        with open(args.golden, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {pool.submit(one, r["account_id"], r.get("as_of") or None, r.get("name")): r for r in rows}
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception as exc:  # noqa: BLE001
                    print(f"FAILED {futures[fut]['account_id']}: {exc}", file=sys.stderr)
    elif args.account:
        results.append(one(args.account, args.as_of, None))
    else:
        print("give an ACCOUNT_ID or --golden CSV", file=sys.stderr)
        return 2
    print(f"\n{'account':<22} {'name':<28} {'score':>5} {'band':<7} {'actions':>7} {'cost':>8}")
    for r in results:
        a = r.assessment
        print(f"{r.account_id:<22} {(r.account_name or '')[:28]:<28} {a['score']:>5} {a['band']:<7} {len(a['recommended_actions']):>7} {r.cost_usd:>8.4f}")
    print(f"\nBriefs in {out_dir}/, ledger in the store ({ctx.store.path}).")
    ctx.engine.close()
    return 0 if results else 1


def cmd_prescreen(args) -> int:
    ctx = _ctx(args)
    if args.account:
        rows = [{"account_id": args.account, "name": (ctx.service.account(args.account) or {}).get("name", ""), "close_date": ""}]
    elif args.renewals_before:
        rows = ctx.engine.list_renewals(args.renewals_before, date.today().isoformat())
    else:
        print("prescreen needs --account or --renewals-before", file=sys.stderr)
        return 2
    flagged = 0
    for row in rows:
        screen = ctx.engine.prescreen(row["account_id"])
        flagged += screen["flag"]
        print(f"\n{row['account_id']} {row.get('name') or ''} {row.get('close_date') or ''} — {'FLAGGED' if screen['flag'] else 'no flag'} "
              f"({screen['high_count']} high, {screen['medium_count']} medium)")
        for s in screen["signals"]:
            print(f"  - {s['name']} ({s['severity']}): {s['value']}")
        if args.account:
            for g in screen["gaps"]:
                print(f"  gap: {g}")
    print(f"\n{flagged} of {len(rows)} flagged. Free heuristic screen — run `renewal-risk assess <id>` for an explained assessment.")
    ctx.engine.close()
    return 0


def cmd_ask(args) -> int:
    ctx = _ctx(args)
    principal = ctx.policy.principal_for_email(args.as_user)
    if principal is None:
        print(f"{args.as_user} is not in access.yaml", file=sys.stderr)
        return 2
    print(json.dumps(ctx.service.ask(principal, args.question), indent=2, default=str))
    ctx.engine.close()
    return 0


def cmd_scan(args) -> int:
    from .service.scheduler import scan

    ctx = _ctx(args)
    notifier = None
    if not args.dry_run and (ctx.settings.slack or {}).get("bot_token"):
        from .slack.app import SlackNotifier
        notifier = SlackNotifier(ctx)
    result = scan(ctx, notifier, dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))
    ctx.engine.close()
    return 0


def cmd_evaluate(args) -> int:
    from . import evaluate

    ctx = _ctx(args)
    return evaluate.run(args.golden, args.db or ctx.store.path, args.k)


def cmd_serve(args) -> int:
    import threading

    import uvicorn

    from .service import scheduler
    from .service.api import app

    ctx = _ctx(args)
    notifier = None
    if not args.no_slack and (ctx.settings.slack or {}).get("bot_token"):
        from .slack import app as slack_app
        # block=False: socket mode's blocking start() installs a SIGINT handler, which only works on the main thread
        threading.Thread(target=slack_app.run, args=(ctx,), kwargs={"block": False}, daemon=True, name="slack").start()
        notifier = slack_app.SlackNotifier(ctx)
        print("Slack app started (socket mode)." if ctx.settings.slack.get("mode", "socket") == "socket" else "Slack app started.")
    if scheduler.start_background(ctx, notifier):
        print(f"Scheduled scan: {ctx.settings.agent['schedule'].get('cron')}")
    from .admin.api import secret_store_for
    from .admin.auth import setup_token
    token = setup_token(secret_store_for(ctx), ctx.store)
    print(f"Admin UI: http://{args.host}:{args.port}/admin/")
    if token:
        print(f"First-run setup token (needed to create the first admin): {token}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_slack(args) -> int:
    from .service import scheduler
    from .slack import app as slack_app

    ctx = _ctx(args)
    scheduler.start_background(ctx, slack_app.SlackNotifier(ctx))
    slack_app.run(ctx)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="renewal-risk", description="Platform-neutral renewal risk agent.")
    parser.add_argument("--config", help="config directory (default ./config or RENEWAL_RISK_CONFIG_DIR)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("presets", help="list the connector catalog").set_defaults(fn=cmd_presets)
    p = sub.add_parser("setup", help="guided setup, or `setup check`")
    p.add_argument("setup_action", nargs="?", choices=["check"])
    p.add_argument("--account", help="with `check`: run a sample free screen on this account id")
    p.set_defaults(fn=cmd_setup)
    sub.add_parser("validate-config", help="static config checks").set_defaults(fn=cmd_validate)
    p = sub.add_parser("test-connector", help="connect to sources")
    p.add_argument("source", nargs="?")
    p.set_defaults(fn=cmd_test_connector)
    p = sub.add_parser("link", help="build account links across sources")
    p.add_argument("--preview", action="store_true", help="(default) show coverage")
    p.add_argument("--review", action="store_true", help="approve / reject suggested matches")
    p.add_argument("--source")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_link)
    p = sub.add_parser("mapping", help="field mapping helpers")
    p.add_argument("mapping_action", choices=["suggest"])
    p.add_argument("--source", required=True)
    p.add_argument("--entity", required=True)
    p.add_argument("--no-model", action="store_true", help="rule-based suggestions only (no API call)")
    p.set_defaults(fn=cmd_mapping)

    p = sub.add_parser("assess", help="full assessment (paid)")
    p.add_argument("account", nargs="?")
    p.add_argument("--as-of")
    p.add_argument("--golden")
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--out", default="briefs")
    p.set_defaults(fn=cmd_assess)
    p = sub.add_parser("prescreen", help="free heuristic screen")
    p.add_argument("--account")
    p.add_argument("--renewals-before")
    p.set_defaults(fn=cmd_prescreen)
    p = sub.add_parser("ask", help="ask a question as a user")
    p.add_argument("question")
    p.add_argument("--as", dest="as_user", required=True)
    p.set_defaults(fn=cmd_ask)
    p = sub.add_parser("scan", help="one scheduled scan")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_scan)
    p = sub.add_parser("evaluate", help="evaluation gates")
    p.add_argument("golden")
    p.add_argument("--k", type=int, default=20)
    p.add_argument("--db")
    p.set_defaults(fn=cmd_evaluate)
    p = sub.add_parser("serve", help="REST API + Slack + scheduler")
    p.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    p.add_argument("--no-slack", action="store_true")
    p.set_defaults(fn=cmd_serve)
    sub.add_parser("slack", help="Slack app only").set_defaults(fn=cmd_slack)

    args = parser.parse_args(argv)
    if args.command == "setup" and args.setup_action == "check":
        return cmd_setup_check(args)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
