# Architecture

## One assessment, end to end

```
account id + as-of date
  │
  ├─ gather     for every entity a specialist or signal needs, each source that provides it:
  │             linker → link value(s) → connector.fetch → mapping → standard records
  │             (as-of filtered, capped per entity, account_id unified; failures become gaps)
  │
  ├─ policy     refuse to send non-test sources to the model unless ZDR is confirmed
  │
  ├─ screen     signal engine → candidate signals per specialist (deterministic, free)
  │
  ├─ coordinate (optional) which specialists with data should run
  │
  ├─ specialists  commercial · relationship · financial · usage — in parallel, one structured
  │             call each, reading only their entities + their candidates; every signal must cite
  │             a `ref` it was given — anything else is dropped and recorded as fabricated
  │
  ├─ audit      Gate 0: any observed date after as-of → leakage violation
  │
  └─ synthesize score 0-100 + band, drivers, citations, rationale, recommended actions
                (citations and action refs re-checked against packet evidence; band derived from score)
  → RunResult → store (assessment + per-stage ledger) → Slack / API / brief
```

Code: [`core/pipeline.py`](../renewal_risk/core/pipeline.py). The REST API, Slack
and the scheduler all go through [`service/risk_service.py`](../renewal_risk/service/risk_service.py),
which is where access control is enforced.

## Why it is platform-neutral

* **Connectors only run requests.** Each one implements `execute(request, ctx,
  mode)`. Everything else is shared in
  [`core/connector.py`](../renewal_risk/core/connector.py): the request chosen
  per entity, the mapping, as-of filtering and the safe templating.
* **Mapping is config.** [`core/mapping.py`](../renewal_risk/core/mapping.py) turns
  raw records into [standard entities](../renewal_risk/core/model.py). Presets
  are the same config packaged under a name.
* **Linking joins sources to one account list.**
  [`core/linking.py`](../renewal_risk/core/linking.py) does this by shared id,
  domain, or reviewed fuzzy name.
* **Downstream code never sees a platform.** Signals, prompts, schemas, Slack
  and access control use standard entities only. An evidence ref
  (`<source>:<entity>/<id>`) is the only place a source's name appears.
* **Third-party connectors plug in.** They register under the entry-point group
  `renewal_risk.connectors`.

## Admin plane: setup in the UI, ready for SaaS

The admin UI ([`web/`](../web), served at `/admin/`) talks to
[`admin/api.py`](../renewal_risk/admin/api.py). Every save goes through a
`ConfigStore` into the same YAML the CLI uses, the previous version is kept, and
the running context rebuilds itself, so no restart is needed.

Each concern below sits behind an interface. The proof of concept uses the free
implementation; a hosted edition swaps in the later one without touching callers:

| Concern | Interface | Now (free) | Later |
|---|---|---|---|
| Config | `admin/config_store.ConfigStore` | YAML files | per-tenant database |
| Credentials | `auth/secrets.SecretStore` | Fernet-encrypted SQLite; config holds `${SECRET:key}` | Vault / cloud KMS |
| Connecting systems | `auth/oauth` | OAuth 2.0 auth code + PKCE with the customer's own app; token refresh at request time; API-token forms | vendor-registered apps (one click), or Nango |
| Tenancy | `tenant_id` on every store row, secret and setting | single tenant `default` | multi-tenant hosting |
| Admin login | `admin/auth.current_admin` | local admins (scrypt), signed SameSite=Strict cookie, first-run setup token, admin API keys | OIDC SSO |
| Data access | connectors | live fetch per assessment | scheduled sync into a local standard-model store |

**Business-type packs** ([`packs/`](../renewal_risk/packs)) are pure YAML. Each
one defines the setup categories (a system is offered in a category when it
provides the category's lead entity), the specialists, the enabled signals,
default playbooks and wording. `saas` and `it_services` share every line of
engine code.

**AI field mapping** ([`admin/mapping_agent.py`](../renewal_risk/admin/mapping_agent.py))
runs only at setup:

1. `Connector.describe()` collects field metadata and sampled example values,
   with free text excluded.
2. Deterministic scoring ranks candidates against each standard field's
   description and aliases ([`core/model.py`](../renewal_risk/core/model.py)).
3. One structured Claude call per entity proposes fields, expressions, value
   maps and confidences.
4. Validation and a preview check the result before the admin saves it as
   ordinary mapping YAML.

## Model calls

[`core/llm.py`](../renewal_risk/core/llm.py) handles every model call:

* **Structured output.** Each call uses JSON-schema structured output
  (`output_config.format`). Results are validated locally, with one retry.
* **Models.** Opus 5.5 does coordination and synthesis; Sonnet 5.5 runs the
  specialists. Both are set in `agent.yaml`, with effort `medium` by default.
* **Refusal fallback.** Server-side refusal fallback (`fallbacks: "default"`) is
  enabled. It switches itself off if a proxy or platform rejects it.
* **Refusals and caching.** A refusal stop reason is reported, never parsed as
  data. System prompts are marked for caching.
* **Cost.** Token usage is converted to dollars per run and stored with each
  assessment.

[`core/prompts.py`](../renewal_risk/core/prompts.py) holds every prompt. Records,
candidates and packets are fenced as data and declared never to be instructions.

## Safety rules

| Rule | Where |
|---|---|
| Read-only sources | Salesforce `block_dml` (SELECT only; keywords inside string literals ignored, so "Merge Labs" is searchable) · SQL `check_read_only` + rolled-back transaction · REST GET/POST only · MCP calls only named tools |
| No injection into queries | values templated into query strings must match an id/date/email pattern; SQL uses bind params; Salesforce search text is SOQL-escaped incl. LIKE wildcards; REST path segments URL-quoted |
| No free text to the model | the standard model has no free-text field; strings clipped to 120 chars; the mapping agent never samples or sends free-text fields |
| Credentials | entered in the UI → encrypted secret store; config files only reference them; OAuth state single-use with a 10-minute expiry; PKCE |
| Admin plane | first admin needs the console setup token; scrypt password hashes; signed HttpOnly SameSite=Strict session; JSON-only API; uploads limited to named .csv/.xlsx ≤ 50 MB in the source's folder |
| No future data in backtests | as-of injected into Salesforce SOQL; `created_at > as_of` dropped for every source; Gate 0 audit over `created_at/updated_at/activity_date/opened_at/paid_at/period_start` |
| No invented evidence | specialist refs ⊆ refs given; synthesis citations and action refs ⊆ packet evidence_refs; violations dropped and recorded in `fabricated_refs` |
| Data-retention policy | `policy.zdr_confirmed` / `RENEWAL_AGENT_ZDR_CONFIRMED`; sources marked `test_data: true` exempt |
| Need-to-know | `access.yaml` roles + scopes on standard account fields; out-of-scope = not found |

## Evaluation

`renewal-risk assess --golden golden.csv` followed by
`renewal-risk evaluate golden.csv` runs the gates
([`evaluate.py`](../renewal_risk/evaluate.py)):

* **Gate 0, leakage.** Any future-dated record in any run means the accuracy
  numbers are withheld.
* **Gate 1, precision@k.** The agent must reach at least 2x the precision of
  "rank by ARR".
* **Fabrication.** Every citation and action ref must be backed by packet
  evidence.
* **Faithfulness worksheet.** A sample of signals for a person who did not build
  the agent to verify in the source system (target ≥ 95%).

For the Salesforce test sandbox, `scripts/build_golden_set.py` builds the golden
set from its answer-key fields. That script is the only code that reads those
fields.

## What changed from the first proof of concept

| First version | Now |
|---|---|
| Salesforce only, raw SOQL in the pipeline | any number of sources through connectors, presets and mapping |
| Hard-coded Salesforce field names in heuristics and prompts | standard model; signals in YAML with thresholds actually applied |
| `signals.yaml` loaded but unused; opp_stalled never evaluated | signal engine drives the prescreen and the specialists' candidates |
| 2 specialists, run in sequence | specialists configured per company, run in parallel, skipped when there's no data |
| rationale only | rationale plus recommended actions (owner, priority, due date, evidence) and optional playbooks |
| forced tool use (rejected by current Opus/Sonnet) | structured outputs, refusal fallback, cost tracking |
| local Flask page, no auth | FastAPI with API keys, Slack app, alerts, role/scope access control |
| YAML-only setup | admin UI: category → system dropdown → OAuth / token → AI field mapping → account matching → signals → access & Slack → health & feedback |
| one business type | packs for B2B SaaS and IT services (projects, milestones, timesheets, escalations) |
| `block_dml` blocked account names like "Merge Labs" | literals ignored |
| 12 guard tests | 86 tests covering mapping, connectors (including a live stub MCP server), OAuth and the Salesforce REST connector against mock servers, the secret store, the mapping agent, the admin API, both packs, linking, signals, pipeline, access, Slack, API, scheduler, examples, plus a Playwright browser test of the admin UI |
