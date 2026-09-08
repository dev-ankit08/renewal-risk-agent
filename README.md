# Renewal Risk Agent — v3 (simplified POC)

Coordinator → 2 specialists in parallel (Commercial, Relationship) → Synthesis
→ one-page brief. One data source (Salesforce), one MCP connection, no
billing connector. See `DOCUMENTATION.md` for the full design, gates, and
order of work — see `FLOW.md` for a code-level trace of exactly what runs,
in what order, and where each prompt comes from.

```
TRIGGER  renewal_date − 120d  (or a golden-set CSV for backtesting)
   ▼
COORDINATOR (claude-opus-5, no tools) — picks specialists, sets tool budget
   ├── COMMERCIAL   (claude-sonnet-5)  Opportunity, OppFieldHistory, Contract, Asset/CPQ
   └── RELATIONSHIP (claude-sonnet-5)  Case, CaseHistory, Contact, OpportunityContactRole, Task, Event
        (parallel; every read goes through soql_guard — as-of enforcement + DML block)
   ▼
2 × EvidencePacket (typed, cited, declares its own gaps)
   ▼
SYNTHESIS (claude-opus-5) — packets only, never raw records; scores, cites, may abstain
   ▼
OUTPUT  briefs/*.md · briefs.csv · runs.sqlite — no Salesforce write-back, no Slack, no email
```

## Run it now, without credentials

```
pip install -r requirements.txt
python3 test_guard.py                              # 12 guard tests — must pass
python3 run.py --dry-run                           # full pipeline, stub model + mock Salesforce
cat briefs/*.md
python3 run.py --dry-run --golden golden_set.example.csv
python3 evaluate.py golden_set.example.csv --k 5
```

Dry-run uses `stub_model.py` (schema-valid canned responses) and
`salesforce_client.MockSalesforce` (canned fixture accounts in
`mock_salesforce.py`) — no API key, no Salesforce org, no MCP connection
needed. It verifies orchestration, schemas, guard, and brief rendering. It
says nothing about quality.

**Expect `evaluate.py` to FAIL Gate 1 on the stub run** — every account
scores 81, so there's no discrimination. That's the harness doing its job,
not a bug. See `DOCUMENTATION.md` for what a passing run requires.

## Run the real model against mock Salesforce data

`USE_STUB_MODEL` is a separate switch from `USE_STUB` — set only this one to
call the real Anthropic API while `mock_salesforce.py`'s fixture accounts are
still used for records:

```
export ANTHROPIC_API_KEY=sk-ant-...
export RENEWAL_AGENT_USE_STUB_MODEL=false

python3 run.py --account ACC-CHURNING-004
```

## Run it for real

Once you supply Salesforce org / MCP connection details:

```
export ANTHROPIC_API_KEY=sk-ant-...
export SALESFORCE_TOKEN=<short-lived OAuth access token>
export SALESFORCE_MCP_URL=https://api.salesforce.com/platform/mcp/v1/...
export RENEWAL_AGENT_USE_STUB=false
export RENEWAL_AGENT_USE_STUB_MODEL=false

python3 run.py --account 0018c00002LmNqRAAZ --as-of 2026-05-01
python3 run.py --golden golden_set.csv --concurrency 4
python3 evaluate.py golden_set.csv --k 20
```

No code changes needed for that switch — `config.py` reads the env vars and
`model_client.py` routes to the real Anthropic API. `salesforce_client.MCPSalesforce`
is not wired up yet, though (see its docstring) — its three methods raise
`NotImplementedError`, so a real run will fail at the first Salesforce fetch
until those calls are implemented against your MCP connection.
`salesforce_client.MCPSalesforce`'s three methods currently raise
`NotImplementedError` with a pointer back to this file — wiring the actual
MCP tool calls there is the one piece of real integration work left once
credentials arrive.

## Files

| File | What it is |
|---|---|
| `guard.py` | Build/verify first. As-of enforcement, DML block, banned fields, leakage audit. |
| `test_guard.py` | 12 guard tests. |
| `schemas.py` | EvidencePacket + RiskAssessment JSON Schemas. The contract everything depends on. |
| `signals.yaml` | Signal registry with severity thresholds. A CS ops analyst owns this, not an engineer. |
| `prompts.py` | The four prompts. Tune these, not the model. |
| `agent.py` | Orchestrator, ledger, brief rendering. |
| `config.py` | Models, rates, MCP toolset, mock-vs-real switch. The file you edit. |
| `model_client.py` | Anthropic wrapper + stub swap seam. |
| `stub_model.py` | Dry-run fake. Delete once you have a key. |
| `salesforce_client.py` | Salesforce data interface: `MockSalesforce` + `MCPSalesforce`. |
| `mock_salesforce.py` | Canned fixture accounts (4 risk profiles) for `MockSalesforce`. |
| `evaluate.py` | Precision@k vs baseline, fabrication check, faithfulness worksheet. |
| `run.py` | CLI. |
| `golden_set.example.csv` | 4-row illustrative example. The real golden set (80–120 accounts) is your next step — see `DOCUMENTATION.md`. |

## Deliberately absent

Stripe/PayPal · warehouse · identity resolution · Postgres · scheduler ·
secrets manager · Slack · email · Salesforce writes · rate-limit backoff.
