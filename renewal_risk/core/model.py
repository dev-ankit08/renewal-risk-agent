"""
model.py — the standard data model every source is mapped into.

Records are plain dicts, not dataclasses: the field mapping is admin config,
and a dict keeps unknown-but-harmless gaps (a source that has no `priority`)
cheap. What makes this the model is ENTITIES below — the only fields that can
exist on a standard record. Anything a source returns that is not mapped onto
one of these fields is dropped, which is also the PII rule: there is no free-
text field (no description, subject, body, comment) anywhere in the model, so
customer-authored text can never reach a prompt.

Every field carries a type, a description and aliases (names systems commonly
use for it). The descriptions are the target the AI mapping agent maps source
fields onto, and what the admin UI shows next to each field.

Every standard record also carries:
  _source   the source name from connectors.yaml (e.g. "billing")
  _entity   the entity name (e.g. "invoice")
  ref       "<source>:<entity>/<id>" — the only citation format the pipeline
            accepts as evidence.
"""
from __future__ import annotations

# Field spec: "type | description | alias, alias, ..."
# Field types: str | num | bool | date | list. Dates are normalised to YYYY-MM-DD.

_COMMON = {
    "id": "str | Unique id of the record in the source system | Id, key, number",
    "created_at": "date | When the record was created in the source system | CreatedDate, created, createdate, creation date",
    "updated_at": "date | When the record was last modified | LastModifiedDate, updated, modified, hs_lastmodifieddate",
}
_ACCOUNT_LINK = {"account_id": "str | Id of the customer account this record belongs to | AccountId, customer id, company id, client id"}

_SPECS: dict[str, dict] = {
    "account": {
        "description": "A customer account (client) — the unit every assessment is about.",
        "allow_key_fields": True,
        "fields": {
            "name": "str | Customer / company name | Name, company name, client, organisation",
            "domain": "list | Web or email domain(s) of the company, used to match it across systems | Website, domain, domains, url",
            "owner_email": "str | Email of the account owner / account manager responsible for the customer | Owner.Email, account manager, CSM email",
            "owner_name": "str | Name of the account owner | Owner.Name, account manager",
            "region": "str | Sales region or country of the customer, used to scope who can see it | BillingCountry, territory, geo, country",
            "segment": "str | Customer segment or tier (Enterprise, Mid-market, SMB) | Type, tier, segment",
            "portfolio": "str | Portfolio / practice / business unit the account belongs to | practice, business unit, vertical",
            "industry": "str | Customer's industry | Industry, sector",
            "arr": "num | Annual recurring revenue / annual contract value of the account | ARR, ACV, annual revenue",
        },
    },
    "customer": {
        "description": "A non-primary source's own customer object (Stripe customer, Zendesk organization, Jira project...). Only used to link that source to accounts.",
        "allow_key_fields": True,
        "fields": {
            "name": "str | Customer name in this system | name, organization, company",
            "domain": "list | Customer email or web domain(s) | email, domain_names, domains",
        },
    },
    "renewal": {
        "description": "An upcoming or past renewal: a renewal opportunity / deal, or a SOW or managed-services extension.",
        "fields": {
            "account_name": "str | Name of the customer the renewal is for | Account.Name",
            "name": "str | Name of the renewal deal | Name, dealname, opportunity name",
            "amount": "num | Value of the renewal deal | Amount, deal value, ACV, TCV",
            "prior_amount": "num | Value of the contract being renewed (previous term) | previous amount, prior ACV",
            "close_date": "date | Expected or actual renewal / close date | CloseDate, closedate, renewal date, expected close",
            "stage": "str | Sales stage of the renewal deal | StageName, dealstage, status",
            "is_open": "bool | True while the renewal is still undecided (not won or lost) | IsClosed (inverted), hs_is_closed (inverted), open",
            "stage_changed_at": "date | When the deal last changed stage | LastStageChangeDate, stage change date",
        },
    },
    "contract": {
        "description": "A contract, subscription, SOW or MSA currently in force.",
        "fields": {
            "arr": "num | Annual value of the contract | ARR__c, annual value, contract value",
            "status": "str | Contract status (Activated, Draft, Expired...) | Status",
            "start_date": "date | Contract start date | StartDate, start",
            "end_date": "date | Contract end date / term end | EndDate, end, expiry, current_period_end",
            "term_months": "num | Contract length in months | ContractTerm, term",
            "cancel_scheduled": "bool | True if the customer has asked to cancel at the end of the term | cancel_at_period_end, non-renewal notice",
        },
    },
    "line_item": {
        "description": "A product, service line or seat count on a contract or subscription.",
        "fields": {
            "product": "str | Product or service name | Product2.Name, Name, plan, sku",
            "quantity": "num | Licensed quantity / seats / units | Quantity, seats, licenses",
            "prior_quantity": "num | Quantity in the previous term | previous quantity",
            "status": "str | Line status (Active, Cancelled, Obsolete...) | Status",
            "is_active": "bool | True while the line is in force | active",
        },
    },
    "contact": {
        "description": "A person at the customer, optionally with a role on the renewal.",
        "fields": {
            "name": "str | Contact's name | Name, full name, firstname",
            "title": "str | Job title | Title, jobtitle, role title",
            "role": "str | Role on the deal / account (Decision maker, Champion...) | Role, contact role",
            "is_primary": "bool | True for the main contact / champion / sponsor | IsPrimary, primary, champion",
            "is_active": "bool | False when the person has left the customer | active, inactive (inverted), status",
        },
    },
    "activity": {
        "description": "A logged touchpoint with the customer: meeting, call, email, task, QBR.",
        "fields": {
            "type": "str | Kind of activity (meeting, call, email, task) | Type, TaskSubtype, activity type",
            "activity_date": "date | When the activity happened | ActivityDate, hs_timestamp, date",
        },
    },
    "ticket": {
        "description": "A support ticket / case.",
        "fields": {
            "status": "str | Ticket status (new, open, pending, solved, closed) | Status, State, hs_pipeline_stage",
            "is_open": "bool | True while the ticket is unresolved | IsClosed (inverted), open",
            "priority": "str | Priority (low, normal/medium, high, urgent) | Priority, hs_ticket_priority, severity",
            "is_escalated": "bool | True if the ticket was escalated | IsEscalated, escalated tag",
            "opened_at": "date | When the ticket was opened | CreatedDate, created_at",
            "closed_at": "date | When the ticket was resolved | ClosedDate, closed_date, solved_at",
            "satisfaction": "num | Customer satisfaction score on a 1-5 scale | CSAT, satisfaction_rating",
        },
    },
    "invoice": {
        "description": "An invoice raised to the customer.",
        "fields": {
            "amount": "num | Invoice total | total, amount, invoice amount",
            "amount_due": "num | Amount still unpaid | amount_remaining, balance, amount due",
            "status": "str | Invoice status (draft, open, paid, void...) | status",
            "is_paid": "bool | True once the invoice is settled | paid",
            "due_date": "date | Payment due date | due_date, duedate",
            "paid_at": "date | When it was paid | paid_at, payment date",
        },
    },
    "payment": {
        "description": "A payment attempt / charge.",
        "fields": {
            "amount": "num | Payment amount | amount",
            "status": "str | Payment status (succeeded, failed, pending) | status",
            "is_failed": "bool | True for a failed payment | failed",
            "is_disputed": "bool | True for a disputed / charged-back payment | disputed, chargeback",
        },
    },
    "usage_metric": {
        "description": "A product-usage measurement for one period.",
        "fields": {
            "metric": "str | Name of the usage metric (e.g. active_users) | metric, event",
            "period_start": "date | Start of the measured period | day, week, date, period",
            "value": "num | Measured value | value, count, active users",
        },
    },
    # ------------------------------------------------------------ IT services
    "project": {
        "description": "A delivery project / engagement for the customer (IT services).",
        "fields": {
            "name": "str | Project name | Name, project name, engagement",
            "status": "str | Project status (Planned, In progress, On hold, Closed) | Status, pse__Stage__c",
            "rag": "str | Health status: red, amber or green | RAG, health, project health, status colour",
            "rag_changed_at": "date | When the health status last changed | health changed date",
            "budget": "num | Approved budget (money or hours) | Budget, planned cost, approved hours",
            "budget_consumed": "num | Budget used so far, in the same unit as budget | actual cost, burned, hours logged",
            "start_date": "date | Project start date | StartDate, start",
            "planned_end": "date | Originally planned end date | planned end, baseline end, EndDate",
            "forecast_end": "date | Current forecast end date | forecast end, expected end",
            "is_active": "bool | True while the project is running | active",
        },
    },
    "milestone": {
        "description": "A project milestone, deliverable or release.",
        "fields": {
            "project_id": "str | Id of the project the milestone belongs to | project",
            "name": "str | Milestone name | name, version",
            "status": "str | Milestone status | status",
            "due_date": "date | When the milestone is due | due date, releaseDate, target date",
            "completed_at": "date | When it was completed | completed date, actual date",
            "is_completed": "bool | True once delivered / released | released, done, complete",
        },
    },
    "time_entry": {
        "description": "Logged effort on the customer's work (timesheets).",
        "fields": {
            "project_id": "str | Id of the project | project",
            "hours": "num | Hours logged | hours, duration, effort",
            "is_billable": "bool | True for billable time | billable",
            "work_date": "date | Day the work was done | date, work date, timesheet date",
        },
    },
    "change_request": {
        "description": "A change request / scope change on a project.",
        "fields": {
            "project_id": "str | Id of the project | project",
            "status": "str | Status (submitted, approved, rejected) | status",
            "amount": "num | Value of the change | amount, value",
        },
    },
    "escalation": {
        "description": "A formal customer escalation (delivery or relationship), distinct from routine support tickets.",
        "fields": {
            "severity": "str | Severity (low, medium, high, critical) | severity, priority",
            "status": "str | Escalation status | status",
            "is_open": "bool | True while the escalation is unresolved | open",
            "opened_at": "date | When it was raised | created, raised date",
            "closed_at": "date | When it was resolved | resolutiondate, closed",
        },
    },
}


def _parse(spec: str) -> dict:
    parts = [p.strip() for p in spec.split("|")]
    aliases = [a.strip() for a in parts[2].split(",")] if len(parts) > 2 and parts[2] else []
    return {"type": parts[0], "description": parts[1] if len(parts) > 1 else "", "aliases": aliases}


ENTITIES: dict[str, dict] = {}
FIELD_DOCS: dict[str, dict[str, dict]] = {}


def _add(name: str, spec: dict) -> None:
    raw = {"id": _COMMON["id"]}
    if name not in ("account", "customer"):
        raw.update(_ACCOUNT_LINK)
    raw.update(spec["fields"])
    raw["created_at"] = _COMMON["created_at"]
    if name != "customer":
        raw["updated_at"] = _COMMON["updated_at"]
    FIELD_DOCS[name] = {f: _parse(s) for f, s in raw.items()}
    ENTITIES[name] = {
        "description": spec["description"],
        "fields": {f: d["type"] for f, d in FIELD_DOCS[name].items()},
        "allow_key_fields": spec.get("allow_key_fields", False),
    }


for _name, _spec in _SPECS.items():
    _add(_name, _spec)
BUILT_IN = frozenset(ENTITIES)
_custom_dates: set[str] = set()


def register_entities(custom: dict[str, dict]) -> None:
    """Replace the admin-defined data types (config/sections.yaml):
    {name: {description, fields: {field: "type | description"}, date_field}}.
    Every reader holds ENTITIES / FIELD_DOCS by reference, so they are changed
    in place. Process-wide: one company configuration per process."""
    for name in [n for n in ENTITIES if n not in BUILT_IN]:
        ENTITIES.pop(name)
        FIELD_DOCS.pop(name)
    _custom_dates.clear()
    for name, spec in custom.items():
        _add(name, spec)
        if spec.get("date_field"):
            _custom_dates.add(spec["date_field"])

# Fields that record when something was actually observed. The leakage audit
# checks these against as_of. Forward-looking dates (close_date, due_date,
# end_date, planned_end) are deliberately absent: a renewal's close date is
# *expected* to be after as_of.
OBSERVED_DATE_FIELDS = ("created_at", "updated_at", "activity_date", "opened_at", "paid_at", "period_start", "work_date")


def observed_date_fields() -> tuple[str, ...]:
    """The built-in observed dates plus each custom data type's "when it happened" field."""
    return OBSERVED_DATE_FIELDS + tuple(sorted(_custom_dates - set(OBSERVED_DATE_FIELDS)))

# The field a record is as-of-filtered on when a source cannot filter itself.
AS_OF_FIELD = "created_at"

# Standard string values are clipped to this length. Model fields are names,
# titles, enums and ids — anything longer is free text that slipped through
# a mapping and should not reach a prompt whole.
MAX_STRING_LENGTH = 120


def field_type(entity: str, field: str) -> str | None:
    spec = ENTITIES.get(entity)
    if spec is None:
        return None
    if field in spec["fields"]:
        return spec["fields"][field]
    if spec.get("allow_key_fields") and field.startswith("key_"):
        return "str"
    return None


def field_docs(entity: str) -> dict[str, dict]:
    """{field: {type, description, aliases}} — for the mapping agent and the UI."""
    return FIELD_DOCS.get(entity, {})


def make_ref(source: str, entity: str, record_id) -> str:
    return f"{source}:{entity}/{record_id}"
