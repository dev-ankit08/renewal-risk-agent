"""
heuristics.py — deterministic, zero-cost signal detection over real Salesforce
records. No Anthropic call, no sqlite ledger write.

Used only by prescreen.py's free bulk risk screen (and available for a human
to run standalone as a sanity check). The real Specialist model call
(model_client.call_specialist) does its own reasoning over raw guarded
records per prompts.py's instructions and does NOT call into this module —
the whole point of paying for a real model call is that it isn't limited to
these canned thresholds.

Field values here are calibrated against the real org schema (renewalRiskSandbox,
Sept 2026), which differs from the fictional schema the old mock/stub code used:
  - Case "open/active":  Status != "Closed"      (real values: Escalated/Closed/New/Working)
  - Case "critical":     Priority == "High"       (real values: High/Medium/Low, no "Critical")
  - Champion departed:   Contact.Title starts with "(Former)"  (no Contact.IsActive field exists)
  - Asset downgrade:     Status == "Obsolete" or Quantity == 0 (real values have no "Cancelled")
  - opp_stalled: NOT evaluated — OpportunityFieldHistory has no populated data for
    bulk-loaded records in this org (no real field-change tracking occurred).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml

_SIGNALS_PATH = Path(__file__).parent / "signals.yaml"


def _load_signals() -> dict:
    with open(_SIGNALS_PATH) as f:
        data = yaml.safe_load(f)
    return {s["name"]: s for s in data["signals"]}


_SIGNALS = _load_signals()

# Thresholds mirror signals.yaml's documented values.
AMOUNT_DECREASE_HIGH_PCT = 15
AMOUNT_DECREASE_MEDIUM_PCT = 5
CASE_VOLUME_HIGH = 3
CASE_VOLUME_MEDIUM = 2
NO_ACTIVITY_HIGH_DAYS = 90
NO_ACTIVITY_MEDIUM_DAYS = 60


def _signal(name: str, severity: str, value: str, evidence_ref: str) -> dict:
    return {"name": name, "severity": severity, "value": value, "evidence_ref": evidence_ref}


def _packet(specialist: str, account_id: str, as_of: str, signals: list[dict], gaps: list[str], evidence_refs: list[str]) -> dict:
    return {
        "specialist": specialist,
        "account_id": account_id,
        "as_of": as_of,
        "signals": signals,
        "gaps": gaps,
        "evidence_refs": sorted(set(evidence_refs)),
        "verbatim_excluded": True,
    }


def _days_between(earlier: str, later: str) -> int:
    d1 = datetime.strptime(str(earlier)[:10], "%Y-%m-%d")
    d2 = datetime.strptime(str(later)[:10], "%Y-%m-%d")
    return (d2 - d1).days


def commercial_signals(account_id: str, as_of: str, records: dict[str, list[dict]]) -> dict:
    signals = []
    evidence_refs = []
    gaps = []

    opportunities = records.get("Opportunity", [])
    contracts = records.get("Contract", [])
    assets = records.get("Asset", [])

    if not opportunities:
        gaps.append("no open renewal Opportunity found for this account as of " + as_of)
    else:
        opp = opportunities[0]
        evidence_refs.append(f"Opportunity/{opp['Id']}")

        # amount_decrease: Opportunity.Amount vs Contract.ARR__c.
        arr = contracts[0].get("ARR__c") if contracts else None
        if arr and opp.get("Amount") is not None and opp["Amount"] < arr:
            pct = (arr - opp["Amount"]) / arr * 100
            severity = "high" if pct > AMOUNT_DECREASE_HIGH_PCT else "medium" if pct > AMOUNT_DECREASE_MEDIUM_PCT else None
            if severity:
                signals.append(_signal(
                    "amount_decrease", severity,
                    f"Opportunity Amount (${opp['Amount']:,.0f}) is {pct:.0f}% below the current contract ARR (${arr:,.0f})",
                    f"Opportunity/{opp['Id']}",
                ))

        # opp_stalled: not evaluable — no OpportunityFieldHistory data for this org.
        gaps.append(
            "opp_stalled not evaluated — OpportunityFieldHistory has no populated "
            "data for this org (no field-change tracking occurred on this record set)"
        )

        # contract_downgrade: any Asset marked Obsolete or reduced to zero quantity.
        for asset in assets:
            if asset.get("Status") == "Obsolete" or asset.get("Quantity", 1) == 0:
                evidence_refs.append(f"Asset/{asset['Id']}")
                signals.append(_signal(
                    "contract_downgrade", "high",
                    f"{asset.get('Name', asset.get('Id'))} is {asset.get('Status', 'reduced to 0 quantity')}",
                    f"Asset/{asset['Id']}",
                ))

    if not contracts:
        gaps.append("no Contract found — cannot compare renewal Amount against current ARR")

    return _packet("commercial", account_id, as_of, signals, gaps, evidence_refs)


def relationship_signals(account_id: str, as_of: str, records: dict[str, list[dict]]) -> dict:
    signals = []
    evidence_refs = []
    gaps = []

    cases = records.get("Case", [])
    contacts = records.get("Contact", [])
    roles = records.get("OpportunityContactRole", [])
    tasks = records.get("Task", [])
    events = records.get("Event", [])

    active_cases = [c for c in cases if c.get("Status") != "Closed"]
    escalated = [c for c in cases if c.get("IsEscalated")]
    high_priority = [c for c in cases if c.get("Priority") == "High"]
    for c in cases:
        evidence_refs.append(f"Case/{c['Id']}")

    if len(active_cases) >= CASE_VOLUME_MEDIUM:
        severity = "high" if len(active_cases) >= CASE_VOLUME_HIGH else "medium"
        signals.append(_signal(
            "case_volume_spike", severity,
            f"{len(active_cases)} case(s) not yet closed",
            f"Case/{active_cases[0]['Id']}",
        ))

    if escalated or high_priority:
        severity = "high" if (len(escalated) >= 2 or high_priority) else "medium"
        ref = escalated[0]["Id"] if escalated else high_priority[0]["Id"]
        signals.append(_signal(
            "support_escalation", severity,
            f"{len(escalated)} escalated case(s), {len(high_priority)} high-priority case(s)",
            f"Case/{ref}",
        ))

    # champion_departed: primary contact role -> Contact whose Title marks them departed.
    primary_roles = [r for r in roles if r.get("IsPrimary")]
    contacts_by_id = {c["Id"]: c for c in contacts}
    for role in primary_roles:
        contact = contacts_by_id.get(role.get("ContactId"))
        if contact and str(contact.get("Title") or "").strip().startswith("(Former)"):
            evidence_refs.append(f"Contact/{contact['Id']}")
            signals.append(_signal(
                "champion_departed", "high",
                f"Primary contact {contact.get('Name', contact['Id'])} is marked departed: {contact['Title']}",
                f"Contact/{contact['Id']}",
            ))

    # low_engagement: fewer than 2 contact roles on the renewal opportunity.
    if roles:
        evidence_refs.append(f"OpportunityContactRole/{roles[0]['Id']}")
        if len(roles) <= 1:
            signals.append(_signal(
                "low_engagement", "medium",
                f"only {len(roles)} contact role(s) on the renewal opportunity",
                f"OpportunityContactRole/{roles[0]['Id']}",
            ))
    else:
        gaps.append("no OpportunityContactRole records — cannot assess engagement breadth")

    # no_recent_activity: latest Task/Event ActivityDate vs as_of.
    activity_dates = [t["ActivityDate"] for t in tasks if t.get("ActivityDate")] + [e["ActivityDate"] for e in events if e.get("ActivityDate")]
    activity_refs = [f"Task/{t['Id']}" for t in tasks if t.get("ActivityDate")] + [f"Event/{e['Id']}" for e in events if e.get("ActivityDate")]
    evidence_refs.extend(activity_refs)
    if activity_dates:
        latest = max(activity_dates)
        days = _days_between(latest, as_of)
        severity = "high" if days > NO_ACTIVITY_HIGH_DAYS else "medium" if days > NO_ACTIVITY_MEDIUM_DAYS else None
        if severity:
            ref = activity_refs[activity_dates.index(latest)]
            signals.append(_signal(
                "no_recent_activity", severity,
                f"no logged Task/Event activity in {days} days",
                ref,
            ))
    else:
        gaps.append("no Task/Event records found — cannot assess recent engagement")

    return _packet("relationship", account_id, as_of, signals, gaps, evidence_refs)
