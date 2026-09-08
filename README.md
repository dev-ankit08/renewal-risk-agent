# Renewal Risk Agent — v3

Coordinator → 2 specialists in parallel (Commercial, Relationship) → Synthesis
→ one-page brief, with a real, explained rationale. One real data source
(Salesforce, via the `sf` CLI), one real model (Anthropic), no billing
connector, no mock/stub mode. See `DOCUMENTATION.md` for the full design and
gates — see `FLOW.md` for a code-level trace of exactly what runs, in what
order, and where each prompt comes from.

```
TRIGGER  an account Id + as-of date (or a golden-set CSV for backtesting)
   ▼
COORDINATOR (claude-opus-5, no tools) — picks specialists, sets tool budget
   ├── COMMERCIAL   (claude-sonnet-5)  Opportunity, Contract, Asset/CPQ
   └── RELATIONSHIP (claude-sonnet-5)  Case, Contact, OpportunityContactRole, Task, Event
        (parallel; every read goes through soql_guard — as-of enforcement + DML block)
   ▼
2 × EvidencePacket (typed, cited, declares its own gaps)
   ▼
SYNTHESIS (claude-opus-5) — packets only, never raw records; scores, cites,
explains its reasoning in plain English, may abstain
   ▼
OUTPUT  briefs/*.md · briefs.csv · runs.sqlite — no Salesforce write-back, no Slack, no email
```

## Setup

```
pip install -r requirements.txt
cp .env.example .env        # then paste in a real ANTHROPIC_API_KEY
sf org list                 # confirm your target org alias shows "Connected"
python3 test_guard.py       # 12 guard tests — must pass, no credentials needed
```

Two things to configure, and nothing else:

1. **`ANTHROPIC_API_KEY`** in `.env` (or exported in your shell). Required for
   every Coordinator/Specialist/Synthesis call — there is no stub fallback.
2. **A `sf` CLI session already authenticated** against your target org. This
   project has **no Salesforce MCP connection and no Salesforce token/URL to
   configure** — auth is inherited entirely from whatever org your `sf` CLI
   session already points at (default alias: `renewalRiskSandbox`; override
   with `RENEWAL_AGENT_SF_ORG_ALIAS`). If `sf org list` doesn't show your org
   as Connected, run `sf org login web --alias <your-alias>` once.

If you plan to point this at a real (non-test) org, read the ZDR note in
`DOCUMENTATION.md` first — real Salesforce field data reaches the Anthropic
API in specialist prompts, and `salesforce_client.get_client()` will refuse
to run against any org alias other than the known synthetic test sandbox
until you explicitly confirm that with `RENEWAL_AGENT_ZDR_CONFIRMED=true`.

## Run it

```
python3 run.py --account 0018c00002LmNqRAAZ --as-of 2026-09-08   # one account, full explained pipeline
python3 run.py --golden golden_set.csv --concurrency 4             # batch, for backtesting
python3 evaluate.py golden_set.csv --k 20                           # Gate 0/1 + fabrication check
```

Every run above calls the real Anthropic API (costs real tokens) and reads
real Salesforce data. For a free, instant, no-Anthropic-call screen to narrow
down which accounts are worth a real run:

```
python3 run.py prescreen --renewals-before 2026-12-31   # rank all open renewals by heuristic signal severity
python3 run.py prescreen --account 0018c00002LmNqRAAZ    # heuristic check on one account
```

`prescreen` uses `heuristics.py` — fixed thresholds over real records, no
model call, no rationale. It's a triage tool, not a replacement for the real
explained assessment `--account`/`--golden` produce.

## Files

| File | What it is |
|---|---|
| `guard.py` | Build/verify first. As-of enforcement, DML block, banned fields, leakage audit. |
| `test_guard.py` | 12 guard tests. |
| `schemas.py` | EvidencePacket + RiskAssessment JSON Schemas. The contract everything depends on. |
| `signals.yaml` | Signal registry with severity thresholds. A CS ops analyst owns this, not an engineer. |
| `prompts.py` | The four prompts. Tune these, not the model. |
| `agent.py` | Orchestrator, ledger, brief rendering. |
| `config.py` | Models, org alias, rates, ZDR gate. The file you edit (or `.env`). |
| `model_client.py` | Anthropic wrapper — always real, no stub. |
| `salesforce_client.py` | The one real Salesforce client — shells out to the `sf` CLI. |
| `heuristics.py` | Free, zero-cost signal detection used only by `prescreen.py`. |
| `prescreen.py` | Bulk/single-account free risk screen — no Anthropic call. |
| `build_golden_set.py` | One-off script: regenerates `golden_set.csv` from the real org's tagged test accounts. |
| `evaluate.py` | Precision@k vs baseline, fabrication check, faithfulness worksheet. |
| `run.py` | CLI. |
| `golden_set.example.csv` | A representative example (one account per test risk profile). |

## Deliberately absent

Stripe/PayPal · warehouse · identity resolution · Postgres · scheduler ·
secrets manager · Slack · email · Salesforce writes · rate-limit backoff.
