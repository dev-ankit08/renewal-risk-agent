# Renewal Risk Agent — v3 — Documentation

## Part 0 — Plain-English overview: what this does, and what each file is for

**What problem this solves.** A CS team wants to know, for any customer
account, "is this renewal at risk, and why?" This project answers that two
ways: an instant free check (just counts and flags things like an escalated
support case or a shrinking deal size), and a slower, real AI-generated
explanation (an actual paragraph of reasoning, backed by citations, written
by Claude) for the accounts worth a closer look.

**The story, in order:**
1. You ask a question — either by typing a command (`run.py`) or by typing a
   plain-English sentence into the web page (`webapp.py`).
2. The tool goes to Salesforce (through the `sf` CLI, already logged in on
   your machine) and pulls that account's real records — Opportunity,
   Contract, Case, Contact, Task, etc.
3. Every one of those records passes through a safety filter (`guard.py`)
   that strips out free-text fields (Case descriptions, comments) before
   anything else touches them, and blocks any accidental write.
4. Then one of two things happens:
   - **Free path**: fixed rules (`heuristics.py`) check the numbers directly
     — "is the deal amount down more than 15%?", "has nobody logged an
     activity in 90+ days?" — and answer instantly, no AI involved.
   - **Paid path**: three real calls to Claude (a Coordinator, two
     Specialists, a Synthesis step) read the same guarded records and
     produce an actual written explanation with a risk score.
5. The answer is either printed to your terminal, shown on the web page, or
   (for the paid path) saved as a Markdown brief in `briefs/` plus a row in
   `briefs.csv` and `runs.sqlite`.

**Every file, grouped by what it's for:**

*How you actually use it (entry points):*
- **`run.py`** — the command-line tool. `--account`/`--golden` run the real,
  paid, explained pipeline; `prescreen` runs the free instant check.
- **`webapp.py`** — the local web page (one text box, plain English). Reuses
  the exact same functions as `run.py` underneath — it's a second front
  door, not a separate system.
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

*The free, instant, no-AI path:*
- **`prescreen.py`** — looks up an account by name or by renewal date range,
  then runs the free check on it.
- **`heuristics.py`** — the actual fixed rules the free check uses (the
  thresholds — "more than 15% down", "90+ days quiet" — live here).
- **`nlquery.py`** — turns a typed sentence like "is Acme at risk" or
  "accounts at risk this quarter" into a structured request the code above
  can act on. No AI used for this — just pattern matching.

*Talking to Salesforce, safely:*
- **`salesforce_client.py`** — the only file that actually shells out to the
  `sf` CLI to run a Salesforce query. Also refuses to touch any org except
  the known test sandbox unless you explicitly confirm you've handled data
  privacy (the ZDR gate — see Part 6).
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
   (real `sf` CLI)      no writes exposed — no insert/update/delete method exists
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
Every Salesforce read goes through the `sf` CLI, already authenticated on
your machine — see README.md's "Setup" for exactly what to configure (an
Anthropic key; nothing else — there is no MCP connection in this design).

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
- **`sf: command not found` / `SalesforceQueryError` about `sf` not on
  PATH** — install the Salesforce CLI
  (https://developer.salesforce.com/tools/salesforcecli) and confirm
  `sf org list` shows your target alias as Connected.
- **Garbled characters (`â€”` instead of `—`) in a value pulled from
  Salesforce, on Windows** — already fixed in `salesforce_client.py` (the
  `sf` CLI subprocess call now explicitly decodes as UTF-8 instead of the
  system locale's default codepage) and in `run.py`/`evaluate.py`/
  `build_golden_set.py` (stdout is reconfigured to UTF-8 at startup). If you
  see this after modifying those files, you likely removed one of those
  fixes — see FLOW.md §9.
- **A bulk `prescreen --renewals-before` run prints `SKIPPED <account_id>: ...
  timed out`** — this is expected, occasional real-world `sf` CLI flakiness
  under sustained sequential load, not a code bug; that account was skipped,
  not silently dropped, and the rest of the screen still completes. Re-run
  `prescreen --account <id>` on the skipped account alone if you need it.

## Part 6 — Two things not to defer

**PII.** The `verbatim_excluded` rule is enforced in `schemas.py` and
`guard.strip_banned_fields` — Case bodies never enter a packet, only counts
and dates. It's also your prompt-injection containment: Case text is
customer-authored, specialists emit structured fields only, so injected
instructions have no path to the synthesis agent. `test_guard.py`'s
`test_strips_free_text_fields` pins this.

**ZDR (zero-data-retention).** There is no MCP server in this design, so the
old MCP-specific ZDR caveat doesn't apply — but the underlying concern does.
Every Coordinator/Specialist/Synthesis call sends real Salesforce field data
(Amounts, Stage names, Contact names/titles, Case priorities and counts — not
free text, which `guard.strip_banned_fields` removes first) to the Anthropic
API in the specialist prompt. For the synthetic `renewalRiskSandbox` test
data, that's a non-issue. The moment you point this at a real production org,
it isn't: **confirm your Anthropic API organization has a Zero Data Retention
agreement in place before running against real customer data** — this is a
contractual/account-level arrangement with Anthropic, not something this
codebase can configure. `salesforce_client.get_client()` enforces a checkpoint
for this: it refuses to query any org alias other than the known synthetic
test sandbox unless you set `RENEWAL_AGENT_ZDR_CONFIRMED=true`. That env var
only records that you've made the decision — it can't verify a ZDR agreement
actually exists.

Separately, the `sf` CLI's own data-handling terms (Salesforce-side data in
transit/cached locally) are governed by Salesforce's standard CLI/platform
terms, not by anything in this project — worth an independent check if that
matters for your org's compliance posture.

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
- **`prescreen_bulk`'s sequential `sf` CLI calls** — screening N accounts
  issues on the order of 9×N subprocess calls, one at a time; at real-world
  volume (confirmed live at 34 accounts) this takes a few minutes and one
  call occasionally times out under sustained load. Handled by
  per-account try/except (skip and continue, see FLOW.md §4), not by making
  the calls faster or parallel — a straightforward follow-up if `prescreen`
  needs to run against hundreds of accounts routinely.

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

Not yet run end-to-end: a full `--account`/`--golden` invocation (the real
Anthropic-calling path) — `prescreen`'s heuristic path was verified instead
to avoid incurring real API cost during this pass. The Anthropic call itself
(`model_client._call_structured`) is unchanged from before this pivot except
for the removed stub branch, so it's lower-risk, but hasn't been exercised
live.
