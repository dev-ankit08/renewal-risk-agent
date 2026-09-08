# Execution Flow — from `run.py` to a rendered brief

This is a code-level trace: which file runs first, what calls what, and
exactly where each prompt and data source comes from. For the architecture
rationale and design gates, see [DOCUMENTATION.md](DOCUMENTATION.md); for
launch commands as a quick reference, see [README.md](README.md). This doc
answers "if I run this, what actually executes, in what order, from where."

## 1. How to launch

| You want... | Command | What it needs |
|---|---|---|
| See it work, no credentials | `py run.py --dry-run` | nothing |
| Real Claude calls, mock Salesforce data | `py run.py --account ACC-CHURNING-004` + `ANTHROPIC_API_KEY` + `RENEWAL_AGENT_USE_STUB_MODEL=false` | Anthropic key only |
| Fully real (Salesforce too) | same + `SALESFORCE_TOKEN`, `SALESFORCE_MCP_URL`, `RENEWAL_AGENT_USE_STUB=false` | **not runnable yet** — see §7 |

Every invocation, regardless of mode, starts at [run.py](run.py) — that is
the one and only entry point. Nothing else in this repo has a `__main__`
block wired for the pipeline itself except [evaluate.py](evaluate.py) and
[test_guard.py](test_guard.py), which are separate, standalone tools you run
*after* `run.py` has produced data ([evaluate.py](evaluate.py) reads
`runs.sqlite`; it does not re-run the pipeline).

## 2. Entry point — [run.py](run.py)

`run.py` does argument parsing and dispatch only — it contains no pipeline
logic itself:

```
main()
 ├─ --dry-run          → config.USE_STUB = True, then run_dry_run_fixtures()
 │                        (loops the 4 accounts in mock_salesforce.ACCOUNTS)
 ├─ --account ID       → _run_one(ID, as_of)   (single account)
 └─ --golden path.csv  → run_golden(path, concurrency)  (many accounts, threaded)
```

Every one of those three paths funnels into the same
[`_run_one()`](run.py#L23-L27), which is two calls:
```python
result = agent.run_account(account_id, as_of)   # the whole pipeline
agent.write_brief(result, account_name)         # briefs/<id>.md
agent.append_briefs_csv(result, account_name)   # briefs.csv rollup row
```
So **[agent.py](agent.py)'s `run_account()` is the real pipeline** — `run.py`
is just the CLI shell around it.

**Import-time note:** `run.py` imports `agent`, which imports `model_client`,
which unconditionally imports `stub_model` at module load — *even if you
never use stub mode*. `stub_model.py` in turn imports `yaml` and reads
`signals.yaml` at import time. That's why `pyyaml` is a hard dependency of
this repo even for a fully-real, non-dry-run invocation ([requirements.txt](requirements.txt#L1)).

## 3. The pipeline, segment by segment — [agent.py `run_account()`](agent.py#L88-L125)

```
run_account(account_id, as_of)
  │
  ├─ 1. salesforce_client.get_client(config.USE_STUB, ...)      [agent.py:94]
  │      USE_STUB=True  → MockSalesforce()      (mock_salesforce.py fixtures)
  │      USE_STUB=False → MCPSalesforce(...)    (NOT implemented — see §7)
  │
  ├─ 2. Ledger() opens/creates runs.sqlite                       [agent.py:95]
  │
  ├─ 3. COORDINATOR                                              [agent.py:97]
  │      model_client.call_coordinator(account_id)
  │      → decides which specialists to dispatch (usually both)
  │      → logged to runs.sqlite as stage "coordinator"
  │
  ├─ 4. for each specialist in the coordinator's plan:           [agent.py:103-109]
  │      a. gather_records()                    [agent.py:67-75]
  │         one SOQL query per object this specialist owns
  │         (COMMERCIAL_OBJECTS / RELATIONSHIP_OBJECTS, agent.py:26-27)
  │         → client.soql_query(soql, as_of)  — see §5, the guard chokepoint
  │      b. model_client.call_specialist(role, account_id, as_of, records)
  │         → produces one EvidencePacket (schemas.EVIDENCE_PACKET_SCHEMA)
  │         → logged to runs.sqlite as stage "specialist:<role>"
  │
  ├─ 5. Gate 0 — guard.audit_leakage(all_records, as_of)         [agent.py:112]
  │      post-hoc scan: did any record's date fall after as_of?
  │      → logged to runs.sqlite as stage "leakage_audit"
  │
  ├─ 6. SYNTHESIS                                                 [agent.py:115]
  │      model_client.call_synthesis(account_id, packets)
  │      → produces the RiskAssessment (schemas.RISK_ASSESSMENT_SCHEMA)
  │      → logged to runs.sqlite as stage "synthesis"
  │
  └─ 7. returns RunResult(account_id, as_of, coordinator, packets,
                           assessment, leakage_violations)
```

Back in `run.py`'s `_run_one()`, that `RunResult` becomes:
- [`render_brief()`](agent.py#L128-L168) → a markdown string
- [`write_brief()`](agent.py#L171-L175) → `briefs/<account_id>.md`
- [`append_briefs_csv()`](agent.py#L178-L195) → one row appended to `briefs.csv`

## 4. Where each prompt actually comes from

This is the part worth being precise about — the stub path and the real path
get their content from **completely different places**.

| Stage | Stub path (`USE_STUB_MODEL=true`, default) | Real path (`USE_STUB_MODEL=false`) |
|---|---|---|
| Coordinator | [`stub_model.generate_coordinator()`](stub_model.py#L42-L47) — hardcoded dict, ignores input entirely | [`prompts.coordinator_prompt()`](prompts.py#L22-L23) wraps the hardcoded `COORDINATOR_SYSTEM` string ([prompts.py:13-19](prompts.py#L13-L19)) + the account id |
| Specialist | [`stub_model.generate_commercial_packet()`](stub_model.py#L52-L104) / [`generate_relationship_packet()`](stub_model.py#L109-L167) — Python if/else heuristics over the real (mock) records | [`prompts.specialist_prompt()`](prompts.py#L54-L60) wraps `SPECIALIST_SYSTEM["commercial"\|"relationship"]` ([prompts.py:26-51](prompts.py#L26-L51)) + the full JSON-dumped records dict |
| Synthesis | [`stub_model.generate_synthesis()`](stub_model.py#L172-L189) — **constant** score 81/high, always | [`prompts.synthesis_prompt()`](prompts.py#L73-L78) wraps `SYNTHESIS_SYSTEM` ([prompts.py:63-70](prompts.py#L63-L70)) + the full JSON-dumped packets list |

So: **[prompts.py](prompts.py) is only ever touched on the real path.**
`--dry-run` (or any run with `USE_STUB_MODEL=true`) never imports its prompt
text into an actual API call — it's pure Python logic in
[stub_model.py](stub_model.py) instead. This is intentional per that file's
own docstring ("delete once you have an ANTHROPIC_API_KEY").

### ⚠️ `signals.yaml` is loaded but not actually used at runtime

[signals.yaml](signals.yaml) documents 8 signals with named severity
thresholds (e.g. "opp_stalled: high if no stage change in 60+ days"), and its
own header comment says "changing a threshold here changes behavior with no
code change." **That is not currently true.** Tracing the actual code:

- [`stub_model.py:39`](stub_model.py#L39) loads it into `_SIGNALS` at import
  time — but `_SIGNALS` is never referenced anywhere else in the file. The
  heuristics in `generate_commercial_packet`/`generate_relationship_packet`
  hardcode their own threshold numbers directly (e.g. `pct > 15`,
  `days > 60` — [stub_model.py:76](stub_model.py#L76),
  [stub_model.py:91](stub_model.py#L91)), duplicating what's in the YAML
  rather than reading it.
- On the real path, [prompts.py](prompts.py)'s `SPECIALIST_SYSTEM` strings
  just name the signals as literal text ("amount_decrease, opp_stalled,
  contract_downgrade") — the file's content (descriptions, thresholds) is
  never read into the prompt sent to Claude.

Net effect: editing `signals.yaml` today changes nothing in either path
except that a YAML syntax error would break the *import* (since `stub_model`
loads it eagerly). If you want `signals.yaml` to actually drive behavior,
that's a real gap to close — happy to wire it up if you want.

## 5. The guard chokepoint (every Salesforce read passes through this)

```
agent.gather_records()
  → client.soql_query(soql, as_of)          (MockSalesforce or MCPSalesforce)
      → guard.soql_guard(soql, as_of)       [guard.py:179-181]
          → guard.enforce_as_of(...)        injects/merges a WHERE <= as_of clause
              → guard.block_dml(...)        raises if INSERT/UPDATE/DELETE/UPSERT/MERGE found
      → (MockSalesforce only) guard.strip_banned_fields(record) per record
        — drops any field whose name contains description/subject/body/
        comment/note/verbatim/textbody/htmlbody (guard.py:85-94)
```
Then, once per full run, [agent.py:112](agent.py#L112) calls
`guard.audit_leakage()` across every record gathered — the post-hoc Gate 0
safety net described in [DOCUMENTATION.md Part 2](DOCUMENTATION.md).

## 6. Output artifacts

| File | Written by | Contents |
|---|---|---|
| `runs.sqlite` | `agent.Ledger.log()`, called after every stage | one row per (account, as_of, stage) — coordinator / specialist:commercial / specialist:relationship / leakage_audit / synthesis, each with the full JSON detail |
| `briefs/<account_id>.md` | `agent.write_brief()` | the rendered one-page brief (score, drivers, evidence by specialist, citations, rationale) |
| `briefs.csv` | `agent.append_briefs_csv()` | rollup row per run: account_id, name, as_of, score, band, abstained, leakage_violations count |

`evaluate.py` reads `runs.sqlite` afterward (via `_latest_detail`/`_latest_packets`,
[evaluate.py:44-65](evaluate.py#L44-L65)) — it does not re-run anything.

## 7. Switches — full reference

| Env var | Config name | Default | Controls |
|---|---|---|---|
| `RENEWAL_AGENT_USE_STUB` | `config.USE_STUB` | `true` | Salesforce: Mock vs MCP ([salesforce_client.get_client](salesforce_client.py#L134-L137)) |
| `RENEWAL_AGENT_USE_STUB_MODEL` | `config.USE_STUB_MODEL` | mirrors `USE_STUB` unless set explicitly | Model calls: `stub_model` vs real Anthropic API ([model_client.py](model_client.py)) — added so you can run real model + mock Salesforce independently |
| `ANTHROPIC_API_KEY` | `config.ANTHROPIC_API_KEY` | unset | Required only when `USE_STUB_MODEL=false` |
| `SALESFORCE_TOKEN` / `SALESFORCE_MCP_URL` | `config.SALESFORCE_TOKEN` / `SALESFORCE_MCP_URL` | unset | Required only when `USE_STUB=false` — **but see below** |

**Current hard limit:** [`salesforce_client.MCPSalesforce`](salesforce_client.py#L97-L131)'s
three methods (`soql_query`, `get_record`, `describe_object`) all
`raise NotImplementedError` — the real Salesforce path has never been wired
to an actual MCP call. Setting `USE_STUB=false` today will fail on the very
first record fetch, immediately after the coordinator's (real or stub) call
succeeds. `USE_STUB` should stay `true` until that wiring is implemented.
