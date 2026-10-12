"""report.py — the one-page markdown brief and the rollup CSV."""
from __future__ import annotations

import csv
from pathlib import Path

from .service.risk_service import assessment_view


def render_brief(result: dict) -> str:
    v = assessment_view(result)
    lines = [
        f"# Renewal Risk Brief — {v['account_name']}", "",
        f"**Account:** {v['account_id']}  ", f"**As of:** {v['as_of']}  ",
        f"**Risk:** {v['score']}/100 ({v['band']}){' — low confidence' if v['abstained'] else ''}", "",
        "## Why", v["rationale"], "", "## Recommended actions",
    ]
    lines += [f"- **{a['action']}** — {a['owner_role']}, {a['priority']} priority, within {a['due_in_days']} days "
              f"({', '.join(f'`{r}`' for r in a['evidence_refs'])})" for a in v["recommended_actions"]] or ["- none"]
    lines += ["", "## Drivers"] + ([f"- {d}" for d in v["drivers"]] or ["- none"])
    lines += ["", "## Evidence"]
    lines += [f"- **{s['name']}** ({s['severity']}, {s['specialist']}): {s['value']} — `{s['evidence_ref']}`" for s in v["signals"]] or ["- no signals"]
    if v["gaps"]:
        lines += ["", "## Not assessed / data gaps"] + [f"- {g}" for g in v["gaps"]]
    if v["leakage_violations"]:
        lines += ["", f"## ⚠ Leakage audit: {v['leakage_violations']} record(s) dated after as-of"]
    if v["fabricated_refs"]:
        lines += ["", "## ⚠ Dropped citations (not in the evidence given)"] + [f"- `{r}`" for r in v["fabricated_refs"]]
    lines += ["", "---", f"_Records read: {v['record_counts']} · cost ${v['cost_usd']} · read-only: nothing was written to any source._"]
    return "\n".join(lines) + "\n"


def write_brief(result: dict, briefs_dir: str | Path) -> Path:
    path = Path(briefs_dir)
    path.mkdir(parents=True, exist_ok=True)
    out = path / f"{result['account_id']}.md"
    out.write_text(render_brief(result), encoding="utf-8")
    return out


def append_csv(result: dict, csv_path: str | Path) -> None:
    path = Path(csv_path)
    exists = path.exists()
    a = result["assessment"]
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not exists:
            writer.writerow(["account_id", "account_name", "as_of", "score", "band", "abstained", "actions", "leakage_violations", "cost_usd"])
        writer.writerow([result["account_id"], result.get("account_name") or "", result["as_of"], a["score"], a["band"],
                         a["abstained"], len(a.get("recommended_actions", [])), len(result["leakage_violations"]), result["cost_usd"]])
