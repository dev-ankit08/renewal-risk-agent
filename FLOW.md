# Execution Flow — from `run.py` to a rendered brief

This is a code-level trace: which file runs first, what calls what, and
exactly where each prompt and data source comes from. For the architecture
rationale and design gates, see [DOCUMENTATION.md](DOCUMENTATION.md); for
launch commands as a quick reference, see [README.md](README.md). This doc
answers "if I run this, what actually executes, in what order, from where."

## 1. How to launch

| You want... | Command | What it needs |
|---|---|---|
| Full, explained pipeline, one account | `py run.py --account <id> --as-of <date>` | `ANTHROPIC_API_KEY` + an authenticated `sf` CLI session |
| Full pipeline, batch/backtest | `py run.py --golden golden_set.csv --concurrency 4` | same |
| Free heuristic screen, no Anthropic call | `py run.py prescreen --renewals-before <date>` or `--account <id>` | just an authenticated `sf` CLI session |

There is no mock/stub mode — every invocation is real. `run.py` is the one
and only pipeline entry point (`evaluate.py` and `test_guard.py` are
separate, standalone tools you run around it: `evaluate.py` reads
`runs.sqlite` after `run.py` has produced data, it does not re-run the
pipeline; `test_guard.py` tests `guard.py` in isolation and needs neither
Anthropic nor Salesforce).

## 2. Entry point — [run.py](run.py)

`run.py` does argument parsing and dispatch only — it contains no pipeline
logic itself:

```
main()
 ├─ prescreen (subcommand)  → _handle_prescreen()  (heuristics.py, no Anthropic call)
 ├─ --account ID --as-of D  → _run_one(ID, D)       (single account, full pipeline)
 └─ --golden path.csv       → run_golden(path, concurrency)  (many accounts, threaded)
```

The two full-pipeline paths funnel into the same
[`_run_one()`](run.py), which is three calls:
```python
result = agent.run_account(account_id, as_of)   # the whole pipeline
agent.write_brief(result, account_name)         # briefs/<id>.md
agent.append_briefs_csv(result, account_name)   # briefs.csv rollup row
```
So **[agent.py](agent.py)'s `run_account()` is the real pipeline** — `run.py`
is just the CLI shell around it. `prescreen` is a materially different,
lighter path — see §4.

## 3. The full pipeline, segment by segment — [agent.py `run_account()`](agent.py)

```
run_account(account_id, as_of)
  │
  ├─ 1. salesforce_client.get_client(config.SALESFORCE_ORG_ALIAS)
  │      → SfCliSalesforce, shells out to the `sf` CLI. Refuses to run
  │        against any org alias other than the known synthetic test
  │        sandbox unless config.ZDR_CONFIRMED is set (see DOCUMENTATION.md
  │        Part 6).
  │
  ├─ 2. Ledger() opens/creates runs.sqlite
  │
  ├─ 3. COORDINATOR
  │      model_client.call_coordinator(account_id)   — real Anthropic call
  │      → decides which specialists to dispatch (usually both)
  │      → logged to runs.sqlite as stage "coordinator"
  │
  ├─ 4. for each specialist in the coordinator's plan:
  │      a. gather_records()
  │         one SOQL query per object this specialist owns
  │         (COMMERCIAL_OBJECTS / RELATIONSHIP_OBJECTS, agent.py)
  │         → client.soql_query(soql, as_of)  — see §5, the guard chokepoint
  │         OpportunityContactRole has no AccountId field in real Salesforce —
  │         it's reached via a two-step join through this account's own
  │         Opportunity Ids, inside gather_records.
  │      b. model_client.call_specialist(role, account_id, as_of, records) — real Anthropic call
  │         → produces one EvidencePacket (schemas.EVIDENCE_PACKET_SCHEMA)
  │         → logged to runs.sqlite as stage "specialist:<role>"
  │
  ├─ 5. Gate 0 — guard.audit_leakage(all_records, as_of)
  │      post-hoc scan: did any record's date fall after as_of?
  │      → logged to runs.sqlite as stage "leakage_audit"
  │
  ├─ 6. SYNTHESIS
  │      model_client.call_synthesis(account_id, packets)   — real Anthropic call
  │      → produces the RiskAssessment (schemas.RISK_ASSESSMENT_SCHEMA),
  │        including a plain-English `rationale` (see prompts.py's
  │        SYNTHESIS_SYSTEM for exactly what's required of it)
  │      → logged to runs.sqlite as stage "synthesis"
  │
  └─ 7. returns RunResult(account_id, as_of, coordinator, packets,
                           assessment, leakage_violations)
```

Back in `run.py`'s `_run_one()`, that `RunResult` becomes:
- [`render_brief()`](agent.py) → a markdown string
- [`write_brief()`](agent.py) → `briefs/<account_id>.md`
- [`append_briefs_csv()`](agent.py) → one row appended to `briefs.csv`

## 4. The free pre-screen path — [prescreen.py](prescreen.py)

`run.py prescreen` never touches `agent.run_account`, `model_client`, or
`runs.sqlite` — it's a deliberately separate, cheap path:

```
prescreen_bulk(before_date) / prescreen_account(account_id, as_of)
  │
  ├─ salesforce_client.get_client(...)         — same real client, same guard chokepoint
  ├─ agent.gather_records(...)                  — reused as-is from the full pipeline
  └─ heuristics.commercial_signals() / relationship_signals()
       → fixed-threshold Python logic, no Anthropic call, no rationale
```

`heuristics.py` is a deliberate, separate module from `prompts.py`/`model_client.py`
— the real Specialist model call does its own reasoning over raw records and
never calls into `heuristics.py`. Use `prescreen` to narrow down which
accounts are worth a real, explained pipeline run; use `--account`/`--golden`
for the actual explained assessment.

## 5. Where each prompt comes from — [prompts.py](prompts.py)

`prompts.py` is the **only** prompt source in the repo — there's no
alternate path. `model_client.py` builds each prompt and forces structured
output via Anthropic tool use:

| Stage | Builder | System text |
|---|---|---|
| Coordinator | `prompts.coordinator_prompt()` | `COORDINATOR_SYSTEM` |
| Specialist | `prompts.specialist_prompt(role, ...)` | `SPECIALIST_SYSTEM["commercial"\|"relationship"]` |
| Synthesis | `prompts.synthesis_prompt()` | `SYNTHESIS_SYSTEM` |

`SYNTHESIS_SYSTEM` explicitly requires `rationale` to be a 2-4 sentence,
plain-English explanation naming the specific signals driving the score and
why — not just a restated number. Both `SPECIALIST_SYSTEM` entries similarly
ask for a specific, readable `value` string per signal rather than a bare
number. Tune these, not the model, if brief quality needs adjusting.

### `signals.yaml` documents signal names/thresholds; two different consumers read it differently

- `heuristics.py` (the free pre-screen path) loads it via `_load_signals()`
  at import time into `_SIGNALS` — but, like the deleted stub before it,
  never actually reads `_SIGNALS` for its thresholds; the threshold
  constants (`AMOUNT_DECREASE_HIGH_PCT`, etc.) are hardcoded directly in the
  module. Editing `signals.yaml`'s numbers today doesn't change
  `heuristics.py`'s behavior — only the module-level constants do.
- The real Specialist prompts just name the signals as literal text — the
  file's descriptions/thresholds are never read into the prompt sent to Claude.

If you want `signals.yaml` to actually drive behavior in either path, that's
a real gap to close, same as before this pivot.

## 6. The guard chokepoint (every Salesforce read passes through this)

```
agent.gather_records() / prescreen.py / build_golden_set.py
  → client.soql_query(soql, as_of)          (SfCliSalesforce)
      → guard.soql_guard(soql, as_of)       [guard.py]
          → guard.enforce_as_of(...)        injects/merges a WHERE <= as_of clause
              → guard.block_dml(...)        raises if INSERT/UPDATE/DELETE/UPSERT/MERGE found
      → guard.strip_banned_fields(record) per record
        — drops any field whose name contains description/subject/body/
        comment/note/verbatim/textbody/htmlbody (guard.py)
```
Then, once per full run, `agent.run_account` calls `guard.audit_leakage()`
across every record gathered — the post-hoc Gate 0 safety net described in
[DOCUMENTATION.md Part 2](DOCUMENTATION.md). `guard.py` itself is untouched
by the real-Salesforce pivot — it was already data-source-agnostic.

## 7. Output artifacts

| File | Written by | Contents |
|---|---|---|
| `runs.sqlite` | `agent.Ledger.log()`, called after every stage of a full pipeline run | one row per (account, as_of, stage) — coordinator / specialist:commercial / specialist:relationship / leakage_audit / synthesis, each with the full JSON detail |
| `briefs/<account_id>.md` | `agent.write_brief()` | the rendered one-page brief (score, drivers, evidence by specialist, citations, rationale) |
| `briefs.csv` | `agent.append_briefs_csv()` | rollup row per run: account_id, name, as_of, score, band, abstained, leakage_violations count |

`prescreen` writes none of these — it's a read-only, stateless query.
`evaluate.py` reads `runs.sqlite` afterward (via `_latest_detail`/`_latest_packets`)
— it does not re-run anything.

## 8. Configuration — full reference

| Env var | Config name | Default | Controls |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | `config.ANTHROPIC_API_KEY` | unset | Required for every Coordinator/Specialist/Synthesis call — no fallback. |
| `RENEWAL_AGENT_SF_ORG_ALIAS` | `config.SALESFORCE_ORG_ALIAS` | `renewalRiskSandbox` | Which `sf`-authenticated org alias to query. |
| `RENEWAL_AGENT_ZDR_CONFIRMED` | `config.ZDR_CONFIRMED` | `false` | Required to query any org alias other than the known test sandbox — see DOCUMENTATION.md Part 6. |

`.env` (copied from `.env.example`) is loaded automatically via
`python-dotenv`; real environment variables always take precedence over it.
There is no Salesforce token/URL/MCP setting anywhere in this design — auth
is entirely inherited from the `sf` CLI session on whatever machine runs this
code.
