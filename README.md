# Renewal Risk Agent

A self-hosted renewal-risk agent that plugs into whatever systems a company
already uses. It reads accounts and renewals from the CRM, money from billing
or the ERP, tickets from the support desk, delivery data from project tools,
and adoption from a product database or any MCP server. It then explains which
renewals are at risk, why, and what to do next. Admins set it up in a web UI.
Project Directors ask in Slack. Other tools use the REST API.

It ships with two business-type packs:

* **B2B SaaS:** subscription renewals, with risk drawn from the CRM, billing,
  support and product usage.
* **IT services & consulting:** SOW and managed-services renewals, with risk
  drawn from the CRM, projects (RAG status, budget burn, slipping milestones),
  timesheets, escalations and invoices.

```
Salesforce / HubSpot / Dynamics ─┐
Stripe / Chargebee / NetSuite ───┤   connectors      standard       signal     Claude
Zendesk / Freshdesk / Jira ──────┼─► (preset or  ─► data model ─► engine ─► specialists ─► score, why,
Postgres / Snowflake / CSV ──────┤   generic)        + linking     (free)     → synthesis    next actions
any MCP server ──────────────────┘                                                         │
                                       Slack (/renewal-risk, @mention, alerts) · REST API · CLI ◄┘
```

* **Not tied to any platform.** Sources are configured, not coded. A built-in
  catalog of presets (Salesforce, HubSpot, Stripe, Zendesk, Freshdesk) means the
  admin only enters credentials. Anything else connects through the generic
  REST, SQL, CSV or MCP connectors.
* **Admin UI for setup, no YAML needed.** For each kind of data (customers,
  support, billing, projects...), the admin picks a system from a dropdown and
  signs in (OAuth or an API token). Credentials are kept encrypted and never
  written to config files. The admin then reviews a field mapping drafted by an
  **AI mapping agent**, which reads each system's field names, labels,
  descriptions and sample values. Remaining screens cover approving account
  matches across systems, tuning signals and playbooks, granting access, and
  connecting Slack.
* **Config as code still works.** The UI writes the same YAML the CLI uses, so
  `renewal-risk setup check`, `link --review` and `mapping suggest` work from the
  command line too.
* **Explained, cited, actionable.** Every signal cites the record it came from,
  e.g. `billing:invoice/in_123`. Any citation the model invents is dropped.
  Actions name an owner role, a priority and a due date, and companies can
  supply their own playbook.
* **Built for Project Directors in Slack.** Ask with `/renewal-risk Acme` or
  `@Renewal Risk which renewals this quarter are at risk?`. The bot DMs an alert
  when an account rises into high risk. Each user only sees the accounts their
  role and scope allow.
* **Safe by default.** Every connector is read-only. The standard model has no
  free-text field, so ticket bodies never reach a prompt. Backtests are filtered
  by as-of date and audited for leaks. Real customer data is not sent to the
  model until the data-retention policy is confirmed.

## Try it offline (2 minutes, no credentials)

```bash
pip install -e ".[all]"
python examples/csv-demo/generate.py                 # SaaS demo: CRM, billing, support, usage
renewal-risk --config examples/csv-demo/config setup check --account A001
renewal-risk --config examples/csv-demo/config prescreen --renewals-before 2027-03-01

python examples/services-demo/generate.py            # IT services demo: CRM, PSA, escalations, finance
renewal-risk --config examples/services-demo/config setup check --account C001

cd web && npm install && npm run build && cd ..     # build the admin UI once
renewal-risk --config examples/csv-demo/config serve # http://127.0.0.1:8080/admin/ (setup token in the log)
```

`setup check` reports four connected sources and account matching per system.
It shows billing matched 4 of 6 accounts, with one similar-name match waiting
for review. A001 has 13 signals drawn from all four systems.

With an `ANTHROPIC_API_KEY` in `.env`, run a full explained assessment:

```bash
renewal-risk --config examples/csv-demo/config assess A001          # writes briefs/A001.md
renewal-risk --config examples/csv-demo/config ask "high risk renewals this quarter" --as admin@demo.example
DEMO_API_KEY=demo-key-change-me renewal-risk --config examples/csv-demo/config serve   # http://127.0.0.1:8080
```

## Set it up for a company

1. **Run it:** `docker compose up` (the image builds the admin UI), or install
   with `pip install ".[all]"`, then run `cd web && npm install && npm run build`,
   then `renewal-risk serve`.
2. **First run:** open `http://<host>:8080/admin/`. Enter the one-time setup
   token printed in the server log, create the admin account, and choose the
   business type.
3. **Connect data:** on the Data sources screen, choose a system for each
   category and sign in. Salesforce, HubSpot and Zendesk use OAuth with your own
   free OAuth app, and the screen shows the callback URL to register. Stripe,
   Freshdesk and Jira use API tokens. Any other system can be a CSV/Excel upload.
4. **Map fields:** click **Suggest with AI**, check the preview, and save.
5. **Match accounts:** approve or reject the similar-name matches.
6. **Grant access and connect Slack:** add people (by Slack email) with a role
   and scope, and paste the Slack app's tokens. Create the app from
   [`slack_manifest.yaml`](slack_manifest.yaml).
7. **Confirm data retention.** Once your Anthropic org has Zero Data Retention
   (or an equivalent agreement), set `policy.zdr_confirmed: true` in `agent.yaml`.

Prefer files? Copy an example such as
[`examples/salesforce-stripe-zendesk`](examples/salesforce-stripe-zendesk/config)
into `./config`, put secrets in `.env`, and run `renewal-risk setup check`.

The full walkthrough is in **[docs/ADMIN_GUIDE.md](docs/ADMIN_GUIDE.md)**. How it
works inside, along with the safety and evaluation gates, is in
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

## Example: Salesforce + Stripe + Zendesk

```yaml
# config/connectors.yaml
primary: crm
sources:
  crm:     {preset: salesforce, org_alias: prodOrg}
  billing: {preset: stripe,  api_key: "${ENV:STRIPE_API_KEY}"}
  support: {preset: zendesk, subdomain: acme, email: ops@acme.com, api_token: "${ENV:ZENDESK_API_TOKEN}"}
linking:
  billing: [{match: "key_account_id = account.id"}, {match: domain}, {match: fuzzy_name}]
  support: [{match: "key_account_id = account.id"}, {match: domain}]
```

That is the whole connector setup. If the company moves from Zendesk to
Freshdesk, change one line (`preset: freshdesk`). Signals, prompts, Slack and
access rules stay the same, because they all work on the standard model.

## Commands

| | |
|---|---|
| `renewal-risk presets` | the connector catalog |
| `renewal-risk setup` / `setup check [--account ID]` | guided setup / validate + test + coverage report |
| `renewal-risk link --review` | approve or reject suggested account matches |
| `renewal-risk mapping suggest --source S --entity E` | draft a field mapping from sample records |
| `renewal-risk prescreen --renewals-before DATE` | free screen of every renewal in a window |
| `renewal-risk assess ID` / `assess --golden CSV` | full assessment(s); markdown briefs |
| `renewal-risk ask "…" --as EMAIL` | answer as a given user (tests access rules) |
| `renewal-risk scan [--dry-run]` | one scheduled scan + alerts |
| `renewal-risk evaluate CSV` | leakage, precision@k vs baseline, fabrication check |
| `renewal-risk serve` | admin UI + REST API + Slack app + scheduler |

## Repository layout

```
renewal_risk/
  core/         standard model, mapping, connectors base, linking, signals, prompts, LLM client, pipeline, validation
  connectors/   salesforce (sf CLI / MCP), salesforce_rest (OAuth), rest, sql, csv, mcp (generic)
  presets/      salesforce, hubspot, stripe, zendesk, freshdesk, jira  (YAML; add more without code)
  packs/        saas, it_services — setup categories, signals, specialists, playbooks, wording
  defaults/     agent.yaml, signals.yaml (merged under the pack and a company's config)
  admin/        admin API, AI mapping agent, pack catalog, config store, admin login
  auth/         encrypted secret store, OAuth 2.0 (PKCE + refresh)
  access/       role/scope access control
  service/      risk service, REST API, SQLite store, scheduler, web page
  slack/        Bolt app, Block Kit rendering, alert notifier
web/            admin UI (React + Vite; `npm run build` puts it in renewal_risk/admin/static)
examples/       csv-demo, services-demo (offline), salesforce-only, salesforce-stripe-zendesk, hubspot-freshdesk-postgres, erp-via-mcp
scripts/        build_golden_set.py (Salesforce test sandbox evaluation helper)
tests/          pytest suite (fake model client; no network)
```

Run the tests with `pytest`. They need no network or API key. The browser test
of the admin UI runs when the UI is built and Chromium is installed
(`python -m playwright install chromium`).
