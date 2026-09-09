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
| Free heuristic screen, no Anthropic call (CLI only) | `py run.py prescreen --renewals-before <date>` or `--account <id>` | just an authenticated `sf` CLI session + Node/npx |
| Plain-English question, no CLI at all | `py webapp.py` (or `run_webapp.bat`), then a browser | `ANTHROPIC_API_KEY` — both question types always call the real pipeline |

There is no mock/stub mode — every invocation is real. `run.py` is the CLI
pipeline entry point; `webapp.py` is a second, independent entry point (see
§9) that reuses the exact same `prescreen.py`/`agent.py` functions rather
than calling `run.py` — there is no subprocess/HTTP hop between them.
`evaluate.py` and `test_guard.py` are separate, standalone tools you run
around all of this (`evaluate.py` reads `runs.sqlite` after a run has
produced data, it does not re-run the pipeline; `test_guard.py` tests
`guard.py` in isolation and needs neither Anthropic nor Salesforce).

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
  │      → McpSalesforceClient, a persistent connection to the official
  │        Salesforce MCP server (memoized per org alias — the same
  │        connection is reused across calls/threads, see §9). Refuses to
  │        run against any org alias other than the known synthetic test
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

## 4. The free pre-screen path (CLI only) — [prescreen.py](prescreen.py)

`run.py prescreen` never touches `agent.run_account`, `model_client`, or
`runs.sqlite` — it's a deliberately separate, cheap path. **This is CLI-only**
— `webapp.py`/`nlquery.py` no longer call `prescreen_account`/`prescreen_bulk`
at all (see §9); they only reuse `prescreen.py`'s account-lookup helpers.

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
(or the web UI) for the actual explained assessment.

**Per-account resilience:** `prescreen_bulk` screens accounts sequentially by
default — each one issues several Salesforce reads, and at real-world volume
a single call can occasionally time out (`config.SF_MCP_CALL_TIMEOUT_SECONDS`).
`prescreen_bulk` catches `salesforce_client.SalesforceQueryError` per
account, prints `SKIPPED <account_id> (<name>): <error>` to stderr, and
continues — one flaky account never aborts the whole screen. This mirrors
`run.py`'s `run_golden`, which already does the same thing for full-pipeline
batch runs. Note: `max_workers > 1` here doesn't meaningfully speed up the
Salesforce-query portion, since `McpSalesforceClient` serializes reads
through one lock (see §9) — unlike the web UI's bulk-window path, where the
concurrent piece is real Anthropic calls, not Salesforce reads.

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
  → client.soql_query(soql, as_of)          (McpSalesforceClient)
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
| `RENEWAL_AGENT_SF_MCP_TOOLSETS` | `config.SF_MCP_TOOLSETS` | `data` | Toolsets passed to `npx @salesforce/mcp --toolsets`. |
| `RENEWAL_AGENT_SF_MCP_STARTUP_TIMEOUT_SECONDS` | `config.SF_MCP_STARTUP_TIMEOUT_SECONDS` | `90` | First-run `npx` download/cache time. |
| `RENEWAL_AGENT_SF_MCP_CALL_TIMEOUT_SECONDS` | `config.SF_MCP_CALL_TIMEOUT_SECONDS` | `45` | Per `soql_query`/`get_record` call, once warm. |

`.env` (copied from `.env.example`) is loaded automatically via
`python-dotenv`; real environment variables always take precedence over it.
There is no separate Salesforce token/client id/secret anywhere in this
design — auth is entirely inherited from the `sf` CLI session on whatever
machine runs this code, via the Salesforce MCP server (`@salesforce/mcp`)
that `salesforce_client.py` launches.

## 9. The web UI — [webapp.py](webapp.py) / [nlquery.py](nlquery.py)

Both question types call the **real** pipeline — there is no free tier on
this page (that's `run.py prescreen`'s job, CLI-only, §4):

```
GET  /          -> INDEX_HTML (one text box, vanilla JS, no framework/CDN)
POST /ask       -> nlquery.parse_intent(question)
                     -> "single_account" -> nlquery.answer_single_account()
                          -> prescreen.resolve_account_by_name()  [free — company name -> Account Id]
                          -> agent.run_account()                   [REAL pipeline — Coordinator/Specialists/Synthesis]
                     -> "bulk_window"    -> nlquery.answer_bulk_window()
                          -> nlquery.resolve_window(period)         [calendar-based, pure]
                          -> prescreen.list_renewing_accounts(before, after)  [free — which accounts are in the window]
                          -> agent.run_account() per account, via a ThreadPoolExecutor
                                                                     [max_accounts=10/max_workers=3 by default —
                                                                      capped for cost, concurrent because the
                                                                      Anthropic calls (not Salesforce reads) are
                                                                      the parallelizable part — see below]
```

There is no `/explain` route — it was removed once both `/ask` paths started
returning full real explanations directly (a second "get the explanation"
step had no remaining purpose). `webapp.py` calls straight into
`prescreen.py`/`nlquery.py`/`agent.py` — no separate server process or HTTP
call between the web layer and the pipeline; same Python process, same
functions, same `salesforce_client.get_client()` ZDR gate, same guard
chokepoint as the CLI path. Account name resolution
(`resolve_account_by_name`) is the first place in this codebase where
free-typed user text reaches a SOQL string literal — see
`prescreen._escape_soql_string`.

**Why bulk concurrency still helps here but not in §4's `prescreen_bulk`**:
`salesforce_client.McpSalesforceClient` serializes every Salesforce read
through one lock (see §10), so concurrent threads don't speed up the
Salesforce-query portion of each account's work either way. But each
`agent.run_account()` call also makes 4 sequential **real Anthropic calls**
(Coordinator, 2 specialists, Synthesis) — those aren't locked, so N accounts'
Anthropic calls genuinely overlap across threads, which is where most of
each account's ~15-30s goes. `max_accounts` caps real spend/latency for one
bulk question (~$0.32/account placeholder rate — shown in the UI's cost banner).

## 10. Salesforce MCP: real bugs found, and Windows encoding fixes

`salesforce_client.py` connects to the official Salesforce MCP server
(`@salesforce/mcp`, launched via `npx`) instead of shelling out to the `sf`
CLI per query. **Two real, non-obvious things confirmed only by actually
connecting** (the public docs don't specify them):

- `run_soql_query` requires **three** parameters — `query`, `usernameOrAlias`
  (yes, even though the server was already started with `--orgs <alias>`),
  and `directory` (a real filesystem path; any valid one works for a plain
  data query). Missing any of the three fails schema validation.
- Its result is a text content block whose text is the literal string `"SOQL
  query results:\n\n"` followed by a JSON object shaped `{"records": [...],
  "totalSize": N, "done": true}` — records directly, not nested under a
  `"result"` key the way the old `sf data query --json` CLI output was.
  `salesforce_client._extract_json_from_text`/`_extract_records` handle both
  the text-prefix-stripping and this shape.

**A real environment blocker, also found only by trying it**: this machine
had no system-wide Node.js/npm/npx — only a private `node.exe` bundled
inside the `sf` CLI's own install, invisible to everything else (even `sf`'s
own plugin installer failed the same way). Installing Node.js LTS fixed it;
this wasn't a requirement under the old `sf`-CLI-only design.

**All Salesforce reads through `McpSalesforceClient` are serialized through
one `asyncio.Lock`** on a single persistent session (started once, reused
for the process's lifetime, memoized per org alias in `get_client()`) —
there's no confirmed evidence yet that concurrent tool calls on one shared
MCP session are safe, and the actual speed win over the old `sf`-CLI-per-call
design comes from eliminating ~3-4s of process-startup overhead per query
(confirmed: ~0.6s/query on the new client vs. ~3-4s/query on the old one),
not from query-level parallelism.

Two more Windows encoding bugs were found and fixed earlier (while this
project still used the `sf` CLI directly), and remain fixed now — confirmed
live, not theoretical:

- **Console output** (`print()` in `run.py`/`evaluate.py`/`build_golden_set.py`)
  — Windows terminals often default `sys.stdout` to the legacy console
  codepage, not UTF-8. All three entry points call
  `sys.stdout.reconfigure(encoding="utf-8")` (guarded by
  `hasattr(stream, "reconfigure")`) at import time to force UTF-8 regardless
  of the environment; `webapp.py` does the same for its own process log.
- **File writes** (`agent.write_brief`, `agent.append_briefs_csv`,
  `evaluate.load_golden`/`write_faithfulness_worksheet`,
  `build_golden_set.py`'s CSV writes, `run.py`'s golden-set CSV read) all
  pass `encoding="utf-8"` explicitly rather than relying on the platform
  default — `agent.render_brief` includes a `⚠` glyph in its leakage-violation
  section, which would otherwise raise `UnicodeEncodeError` on a Windows
  default encoding that can't represent it.

Whether the `mcp` SDK's stdio transport to the `npx`-launched server has any
Windows-specific encoding quirk of its own was checked live, not assumed:
a `Contact.Title` value containing an em-dash (the same field that exposed
the old `subprocess.run` bug) came back correctly through the new client in
the regression test — see DOCUMENTATION.md Part 11. No mojibake observed.
