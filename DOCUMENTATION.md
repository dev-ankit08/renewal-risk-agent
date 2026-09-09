# Renewal Risk Agent — v3 — Documentation

## Part 0 — Plain-English overview: what this does, and what each file is for

**What problem this solves.** A CS team wants to know, for any customer
account, "is this renewal at risk, and why?" This project answers with a
real, AI-generated explanation (an actual paragraph of reasoning, backed by
citations, written by Claude) — plus a separate, free instant screen
(`run.py prescreen`) for quickly narrowing down which accounts are worth
that closer look, using fixed rules instead of a model call.

**The story, in order (the web page / `run.py --account`):**
1. You ask a question — either by typing a command (`run.py`) or by typing a
   plain-English sentence into the web page (`webapp.py`).
2. The tool goes to Salesforce — through a persistent connection to the
   official Salesforce MCP server (`@salesforce/mcp`), which reuses the `sf`
   CLI session already logged in on your machine — and pulls that account's
   real records: Opportunity, Contract, Case, Contact, Task, etc.
3. Every one of those records passes through a safety filter (`guard.py`)
   that strips out free-text fields (Case descriptions, comments) before
   anything else touches them, and blocks any accidental write.
4. Three real calls to Claude (a Coordinator, two Specialists, a Synthesis
   step) read the guarded records and produce an actual written explanation
   with a risk score.
5. The answer is shown on the web page or printed to your terminal, and
   saved as a Markdown brief in `briefs/` plus a row in `briefs.csv` and
   `runs.sqlite`.

Separately, `run.py prescreen` (CLI only, not on the web page) answers
instantly and for free: fixed rules (`heuristics.py`) check the numbers
directly — "is the deal amount down more than 15%?", "has nobody logged an
activity in 90+ days?" — no AI, no cost, no explanation.

**Every file, grouped by what it's for:**

*How you actually use it (entry points):*
- **`run.py`** — the command-line tool. `--account`/`--golden` run the real,
  paid, explained pipeline; `prescreen` runs the free instant check (CLI only).
- **`webapp.py`** — the local web page (one text box, plain English). Always
  calls the real, paid, explained pipeline for both question types — reuses
  `agent.py`'s functions directly, it's a second front door, not a separate
  system.
- **`run_webapp.bat`** — double-click this to start the web page and open
  your browser automatically, with zero typing.

*The real, paid, AI-explained pipeline (the "brain"):*
- **`agent.py`** — the conductor. Given one account, it calls the
  Coordinator, then the two Specialists, then Synthesis, in order, and
  writes the final brief.
- **`model_client.py`** — the only place that actually calls the Anthropic
  (Claude) API.
- **`prompts.py`** — the exact instructions sent to Claude at each step. If
  you want to change how the AI reasons or writes its explanation, this is
  the file to edit.
- **`schemas.py`** — the strict shape every AI response must match (a score,
  a band, cited drivers, a rationale) — rejects anything that doesn't fit.

*The free, instant, no-AI path (CLI only — `run.py prescreen`):*
- **`prescreen.py`** — looks up an account by name or by renewal date range
  (both also reused by the real, paid path below), and runs the free
  heuristic check via `run.py prescreen`.
- **`heuristics.py`** — the actual fixed rules `prescreen.py`'s free check
  uses (the thresholds — "more than 15% down", "90+ days quiet" — live here).

*Turning a typed question into an action:*
- **`nlquery.py`** — turns a typed sentence like "is Acme at risk" or
  "accounts at risk this quarter" into a structured request (pattern
  matching, no AI for the parsing itself), resolves the account name(s) via
  `prescreen.py`'s lookups, then calls `agent.run_account` — the real
  pipeline — for both question types.

*Talking to Salesforce, safely:*
- **`salesforce_client.py`** — the only file that talks to Salesforce: a
  persistent connection to the official Salesforce MCP server
  (`@salesforce/mcp`, launched via `npx`), reusing the `sf` CLI session
  already authenticated on this machine. Also refuses to touch any org
  except the known test sandbox unless you explicitly confirm you've handled
  data privacy (the ZDR gate — see Part 6).
- **`guard.py`** — the safety net every single Salesforce read passes
  through: blocks writes, enforces "don't look at data from after this
  date" (so a backtest can't cheat), and strips free-text fields.

*Configuration:*
- **`config.py`** — model names, which Salesforce org to use, cost rates.
  The file (or `.env`) you actually edit.
- **`.env.example`** — copy this to `.env` and put your real Anthropic API
  key in it.
- **`requirements.txt`** — the Python packages this needs (`pip install -r
  requirements.txt`).

*Testing, sample data, and grading the AI's work:*
- **`test_guard.py`** — 12 automated tests for the safety-net file above.
  Should always pass.
- **`build_golden_set.py`** — a one-off script that pulls the 34 test
  accounts (each pre-labeled "should be healthy" / "should be churning"
  etc.) out of the sandbox org into a CSV, so you can check whether the
  AI's answers match what's actually true.
- **`golden_set.csv` / `golden_set.example.csv`** — that CSV (the real one
  is gitignored; the example is a small, safe-to-share sample of it).
- **`evaluate.py`** — grades a batch of runs against the golden set: is the
  AI actually better than just "flag the biggest accounts", and did it ever
  cite evidence it wasn't given (a fabrication check)?

*Reference data:*
- **`signals.yaml`** — a human-readable list of the named risk signals (like
  `amount_decrease`, `champion_departed`) and their severity thresholds,
  meant to be reviewed/owned by a CS ops person, not just a developer.

*Documentation (you are here):*
- **`README.md`** — the quick-start: setup and the exact commands to run.
- **`FLOW.md`** — a precise, code-level trace of exactly what calls what, in
  what order, for anyone who wants to follow the wiring.
- **`DOCUMENTATION.md`** — this file: the fuller design rationale, gates,
  and gotchas.

## Part 1 — What this is

A multi-agent pipeline that, given a Salesforce account and an as-of date,
produces a one-page risk brief: a churn/downgrade score, cited drivers, and a
plain-English explanation of why. It reads Salesforce, never writes to it.

```
TRIGGER  an account Id + as-of date  (or a golden-set CSV for backtesting)
   │
   ▼
COORDINATOR          claude-opus-5      no tools — planner only
   │                                    picks specialists, sets tool budget
   ├──────────────┬─────────────────────────────────────────────
   ▼              ▼
COMMERCIAL     RELATIONSHIP             claude-sonnet-5 (parallel)
Opportunity    Case
Contract       Contact, OpportunityContactRole
Asset/CPQ      Task, Event
   │              │
   └──────┬───────┘
          ▼
     soql_guard          ← as-of enforcement + DML block. Built first.
          ▼
   SALESFORCE          soql_query · get_record · describe_object
   (real Salesforce MCP) no writes exposed — no insert/update/delete method exists
          │
   2 × EvidencePacket    typed · cited · declares its own gaps
          ▼
SYNTHESIS            claude-opus-5      packets only, never raw records
                                        scores, cites, explains its reasoning
                                        in plain English, may abstain
          ▼
OUTPUT               briefs/*.md · briefs.csv · runs.sqlite
                     no Salesforce write-back, no Slack, no email
```

Every Coordinator/Specialist/Synthesis call is a real Anthropic API call.
Every Salesforce read goes through a persistent connection to the official
Salesforce MCP server (`@salesforce/mcp`), which reuses the `sf` CLI session
already authenticated on this machine — see README.md's "Setup" for exactly
what to configure (an Anthropic key, plus Node.js/`npx` on PATH; no separate
Salesforce token/client id/secret). This replaced an earlier design that
shelled out to the `sf` CLI directly, once per query — that was measurably
slow (confirmed live: ~3-4s of fixed process-startup overhead per query,
versus ~0.6s per query through the persistent MCP connection) — see Part 11.

`OpportunityFieldHistory`/`CaseHistory` are not queried: this org's
bulk-loaded test data has no populated field-change tracking, so
`opp_stalled` (which depends on stage-change history) is reported as a
declared gap rather than faked. If your org has real field-history tracking
enabled, re-add `OpportunityFieldHistory` to `agent.COMMERCIAL_OBJECTS` and
wire the corresponding check back into `prompts.py`'s Commercial instructions.

## Part 2 — The two hard gates

- **precision@20 ≥ 2× baseline** (baseline = your existing health score, or
  ARR descending — this build uses ARR descending since no health score is
  wired in).
- **faithfulness ≥ 95%** — manual. A CS ops analyst opens sampled
  `evidence_ref`s in Salesforce and confirms each driver. `evaluate.py`
  prepares the sample (`faithfulness_worksheet.csv`); the confirmation itself
  is not automatable and shouldn't be — it's the check on whether the model
  is telling the truth about what it read.

**Gate 0** runs before both: zero leakage violations
(`guard.audit_leakage`). If it finds anything, accuracy numbers from that run
are meaningless — `evaluate.py` refuses to print Gate 1 or the fabrication
check when Gate 0 fails.

`evaluate.py` also runs a **fabrication check**: every citation in a
RiskAssessment must resolve to an `evidence_ref` one of that account's own
packets actually emitted. A citation that doesn't is the model claiming a
source it was never given.

## Part 3 — Golden set

`golden_set.csv` columns: `account_id, name, arr, renewal_date, as_of, outcome`.

- `outcome ∈ churned / downgraded / renewed / expanded`
- Target: 80–120 accounts, ~30% churn/downgrade, for a real production golden set.

`golden_set.example.csv` in this repo is regenerated by `build_golden_set.py`
from the real `renewalRiskSandbox` test org — one representative account per
test risk profile (Healthy/Stalled/Escalating/Churning/Commercial Risk
Only/Relationship Risk Only). Run `python3 build_golden_set.py` after
authenticating `sf` against your org to regenerate both `golden_set.csv` (the
full 34-account set, gitignored) and this example file.

`build_golden_set.py` is the **one sanctioned place** in this codebase that
reads `Account.Renewal_Risk_Profile__c`/`Account.Expected_Outcome__c` — these
are ground-truth test labels for building/checking this golden set, never an
input to the agent's own risk detection. `agent.py`'s `gather_records` must
never query `Account` fields beyond `Id`/`AccountId` joins.

`as_of` is set to today for every row in this synthetic dataset: it has no
genuine historical staging to derive a different cut from, and every
record's own `CreatedDate` (an audit field the API can't backdate) is
literally today — a computed "120 days before renewal" `as_of` would exclude
almost every record from `guard.enforce_as_of`'s `CreatedDate <= as_of`
filter. A real production golden set built from genuine historical accounts
can and should use a real 120-day-before-renewal `as_of` per row.

## Part 4 — Order of work

1. `test_guard.py` green.
2. `sf org list` shows your target org as Connected, and that user can
   actually **see** your target accounts. Record-level sharing bites here,
   and it looks like the agent missing data.
3. `signals.yaml` thresholds reviewed with a CS ops analyst (this build ships
   a starter set of 8 signals across the two specialists — treat it as a
   draft, not a final registry).
4. Golden set assembled (`build_golden_set.py` for this test org; a real
   golden set against your own org's actual outcomes for production use).
5. One real account end to end. Read the packets by hand — this build's
   `runs.sqlite` ledger and rendered `briefs/*.md` are built for exactly this
   step. This is where you find that CSAT isn't a field in your org, or that
   `Case.AccountId` is null for 15% of cases because they're contact-linked
   only.
6. Full backtest → leakage audit → gates.
7. Shadow 30 accounts with 5 CSMs, 30% held back.

## Part 5 — Setup

See README.md's "Setup" section for the exact commands. In short:

```
pip install -r requirements.txt
cp .env.example .env    # paste in ANTHROPIC_API_KEY
sf org list              # confirm your org alias is Connected
node --version           # confirm Node.js/npx is available — needed to launch the Salesforce MCP server
python3 test_guard.py
```

Common errors:

- **`'python3' is not recognized`** — use `py` (or `python`) on Windows.
- **`ModuleNotFoundError: No module named 'yaml'`** — `pip install -r
  requirements.txt` wasn't run in the same environment `py` resolves to.
  Confirm with `py -m pip show pyyaml`.
- **PowerShell blocks `.venv\Scripts\activate`** — run
  `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` first, or use
  `cmd.exe` instead.
- **`ANTHROPIC_API_KEY` not picked up** — confirm `.env` is in the repo root
  (same directory as `config.py`) and `python-dotenv` is installed; or, if
  using a real shell env var on Windows, remember `set VAR=value` only lasts
  the current shell session — use `setx VAR value` for persistence, then open
  a new shell.
- **`sf: command not found`** — install the Salesforce CLI
  (https://developer.salesforce.com/tools/salesforcecli) and confirm
  `sf org list` shows your target alias as Connected.
- **`SalesforceQueryError: Could not start the Salesforce MCP server...`** —
  confirm Node.js is installed and `npx --version` works in the same shell
  environment this runs in. The first query in a fresh process is slower
  while `npx` downloads/caches `@salesforce/mcp` (a one-time cost per
  machine, not per run) — if it fails on a slow/unreliable network, just
  retry; `RENEWAL_AGENT_SF_MCP_STARTUP_TIMEOUT_SECONDS` can be raised if
  needed.
- **Garbled characters (`â€”` instead of `—`) in a value pulled from
  Salesforce, on Windows** — already fixed in `run.py`/`evaluate.py`/
  `build_golden_set.py` (stdout is reconfigured to UTF-8 at startup). If you
  see this after modifying those files, you likely removed that fix — see
  FLOW.md §10.
- **A bulk `prescreen --renewals-before` run prints `SKIPPED <account_id>: ...
  timed out`** — expected, occasional real-world flakiness under sustained
  load (see `config.SF_MCP_CALL_TIMEOUT_SECONDS`), not a code bug; that
  account was skipped, not silently dropped, and the rest of the screen
  still completes. Re-run `prescreen --account <id>` on it alone if needed.

## Part 6 — Two things not to defer

**PII.** The `verbatim_excluded` rule is enforced in `schemas.py` and
`guard.strip_banned_fields` — Case bodies never enter a packet, only counts
and dates. It's also your prompt-injection containment: Case text is
customer-authored, specialists emit structured fields only, so injected
instructions have no path to the synthesis agent. `test_guard.py`'s
`test_strips_free_text_fields` pins this.

**ZDR (zero-data-retention) — two separate concerns, not one.**

*1. Anthropic.* Every Coordinator/Specialist/Synthesis call sends real
Salesforce field data (Amounts, Stage names, Contact names/titles, Case
priorities and counts — not free text, which `guard.strip_banned_fields`
removes first) to the Anthropic API in the specialist prompt. For the
synthetic `renewalRiskSandbox` test data, that's a non-issue. The moment you
point this at a real production org, it isn't: **confirm your Anthropic API
organization has a Zero Data Retention agreement in place before running
against real customer data** — a contractual/account-level arrangement with
Anthropic, not something this codebase can configure.
`salesforce_client.get_client()` enforces a checkpoint for this: it refuses
to query any org alias other than the known synthetic test sandbox unless you
set `RENEWAL_AGENT_ZDR_CONFIRMED=true`. That env var only records that you've
made the decision — it can't verify a ZDR agreement actually exists.

*2. Salesforce's own MCP server.* `@salesforce/mcp` (the package
`salesforce_client.py` launches) displays its own data-collection disclosure
on startup, confirmed live: *"You acknowledge and agree that the MCP server
may collect usage information, user environment, and crash reports for the
purposes of providing services or functions that are relevant to use of the
MCP server and product improvements."* This is Salesforce's own telemetry,
separate from both the Anthropic ZDR concern above and the `sf` CLI's own
terms — worth an independent read
(https://github.com/salesforcecli/mcp) if that matters for your org's
compliance posture, especially before pointing this at a real production org.
This project doesn't configure or suppress that disclosure; it's the
server's own default behavior.

## Part 7 — Deliberately absent

Stripe/PayPal · warehouse · identity resolution · Postgres · scheduler ·
secrets manager · Slack · email · Salesforce writes · rate-limit backoff.

Add a billing source in phase 2 only if you're monthly-billed or self-serve —
then payment behaviour is the churn event. For annual enterprise contracts,
CPQ amendments (this build's `Asset` records) cover the downgrade signal, and
payment failures arrive too late to act on.

## Part 8 — Cost

~$0.32/account uncached · ~$0.15 with prompt caching · ~$0.08 with Batch API.
100 accounts × 3 tuning passes ≈ $100 for the whole POC.

Rates in `config.py` are **placeholders** — verify at
https://claude.com/pricing before any cost slide. `config.RATES_PER_MILLION_TOKENS`
is defined but not currently wired into any actual per-run cost calculation —
wiring it into `agent.Ledger` to log real token usage per run would be a
natural next step now that every call is real.

## Part 9 — Known gaps and non-enforced fields (audited, not guessed at)

- **`config.DEFAULT_TOOL_BUDGET_PER_SPECIALIST`** — the coordinator emits a
  `tool_budget` field, but nothing downstream reads or enforces it. Purely
  decorative today.
- **`config.DEFAULT_LOOKAHEAD_DAYS`** — documents the "120 days before
  renewal" trigger concept but isn't computed anywhere; `run.py` always
  requires an explicit `--as-of`. Can't be safely applied to the current
  sandbox dataset (see Part 3's `as_of` note) but is correct for real
  production accounts with genuine historical `CreatedDate`s.
- **`SalesforceClient.get_record`/`describe_object`** — implemented but not
  called anywhere in the current pipeline (only `soql_query` is used); kept
  for the documented three-method tool surface and direct-lookup/debugging use.
- **`opp_stalled`** — not evaluable against this org's bulk-loaded test data
  (see Part 1). Always reported as a declared gap, never guessed at.
- **`salesforce_client.McpSalesforceClient` serializes every Salesforce read
  through one lock** — there's no confirmed evidence yet that the MCP SDK +
  `@salesforce/mcp` combination handles truly concurrent tool calls on one
  shared session correctly, so `prescreen_bulk`'s `max_workers` doesn't speed
  up the Salesforce-query portion of a bulk screen (it does still help the
  web UI's real-pipeline bulk path, where the concurrent piece is the
  Anthropic calls, not Salesforce reads — see Part 11). Revisiting the lock
  would need evidence of a real bottleneck plus a targeted concurrency test,
  not a guess.
- **A single account's `sf`-CLI-launched `@salesforce/mcp` server process is
  shared for the whole Python process's lifetime** (`get_client()` memoizes
  by org alias) — this is what makes repeated queries fast, but it also
  means a long-lived `webapp.py` process holds one persistent `npx` child
  process open for as long as it runs. `McpSalesforceClient.close()` +
  an `atexit` hook tear it down on normal process exit.

## Part 10 — Validated against the real sandbox (not just designed)

This pivot was verified end-to-end against the real `renewalRiskSandbox` org,
not just written and assumed correct:

- `test_guard.py`: 12/12 pass, unchanged.
- `run.py prescreen --account <id>` run against one account from each of the
  6 test risk profiles: Healthy correctly returns zero signals; Churning
  correctly returns all 6 expected signals (`amount_decrease`,
  `contract_downgrade`, `support_escalation`, `champion_departed`,
  `low_engagement`, `no_recent_activity`) with accurate, specific values (e.g.
  "Opportunity Amount ($70,300) is 26% below the current contract ARR
  ($95,000)"); Escalating correctly returns exactly `case_volume_spike` +
  `support_escalation` and nothing else — confirming the commercial/
  relationship signal isolation the test data was designed to exercise
  actually holds.
- `run.py prescreen --renewals-before <date>` (bulk) run against the full set
  of real open renewals in the org.
- The `OpportunityContactRole` two-step join, the `FIELDS(ALL)` + `LIMIT 200`
  SOQL shape, the guard-injected as-of clause combined with both of those,
  and the `Account.Renewal_Risk_Profile__c`/`Expected_Outcome__c` query
  `build_golden_set.py` uses were all independently confirmed working via
  direct `sf data query` calls before being wired into the Python code.

Not yet run end-to-end at that point: a full `--account`/`--golden`
invocation (the real Anthropic-calling path) — `prescreen`'s heuristic path
was verified instead to avoid incurring real API cost during that pass. See
Part 11 for what changed since, and what has now actually been exercised.

## Part 11 — Salesforce MCP migration + web UI real-pipeline change (validated)

Two changes, made together: (1) `salesforce_client.py` was rewritten to talk
to the real, official Salesforce MCP server (`@salesforce/mcp`) instead of
shelling out to the `sf` CLI per query — done specifically because the `sf`
CLI approach was measurably slow; (2) `webapp.py`/`nlquery.py` now call the
real pipeline for both question types (single-account *and* bulk-window),
not just single-account.

**A real, non-obvious blocker found and fixed along the way**: this machine
had no system-wide Node.js/npm/npx at all — only a private `node.exe` bundled
inside the `sf` CLI's own install, invisible to everything else including
`sf`'s own plugin installer (`sf plugins install @salesforce/mcp` failed with
`CannotFindNodeExecutable`). Installed Node.js LTS via `winget` to fix this —
not something the earlier `sf`-CLI-only design ever needed, since `sf` itself
never required a system-wide Node.

**Also found and corrected before shipping** (the public `@salesforce/mcp`
docs don't specify these — confirmed by actually connecting and calling
`list_tools()`, not assumed):
- `run_soql_query` requires **three** parameters, not just the query string:
  `query`, `usernameOrAlias` (yes, even though the server was already started
  with `--orgs <alias>`), and `directory` (a real filesystem path — any valid
  one works for a plain data query, confirmed live).
- Its result is a text content block whose text is the literal string `"SOQL
  query results:\n\n"` followed by a JSON object shaped `{"records": [...],
  "totalSize": N, "done": true}` — records directly, unlike the old `sf data
  query --json` CLI output, which nested this under a `"result"` key.

**Validated, measured, not assumed:**
- Full regression parity: `gather_records` for both specialists, the
  `OpportunityContactRole` join, `guard.strip_banned_fields` stripping
  `Description` from real records, `resolve_account_by_name`'s exact/fuzzy/
  ambiguous/not-found branches (including an apostrophe in the input,
  correctly escaped, no crash), and the `build_golden_set.py`-style tag query
  — all re-run against the new MCP client with identical results to the old
  `sf`-CLI-based client.
- `test_guard.py`: 12/12, unchanged (`guard.py` untouched by this migration).
- Concurrency: 5 accounts screened simultaneously via a thread pool, each
  result's `account_id` matched what was asked for — no cross-talk between
  threads sharing one locked MCP session.
- **The actual performance claim**: the same "this year" bulk-screen
  benchmark used to justify this migration, re-measured on the new client —
  53 accounts, fully sequential (`max_workers=1`, matching `run.py
  prescreen`'s default), took 4m57s, i.e. **~5.6s/account**. The old
  `sf`-CLI-based client took ~9-11s/account *even with* 4-6-way thread
  concurrency (15 accounts/6 workers → 2m16s; 25 accounts/4 workers →
  4m29s). The new client is faster fully sequential than the old one was
  with concurrency — because the win comes from eliminating ~3-4s of
  per-query process-startup overhead, not from parallelism (see Part 9's
  note on why the shared session is lock-serialized).
- The single-account web UI path (`/ask` → `agent.run_account`) was
  exercised end-to-end through to a correct, clean error — `ANTHROPIC_API_KEY
  not set` — confirming the wiring is correct up to the point that needs a
  real key, which wasn't available during this pass (added by the project
  owner afterward, per their own request to keep that credential manual).
