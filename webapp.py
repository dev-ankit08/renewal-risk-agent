#!/usr/bin/env python3
"""
webapp.py — a local web page for plain-English risk questions.

    python webapp.py
    then open http://127.0.0.1:5000

One text box. Type "is <company> at risk" or "accounts at risk this
quarter/half/year". Answers come from the free heuristic screen
(prescreen.py/heuristics.py) — no Anthropic call, instant. A second,
explicit "Get full AI explanation" button (only shown after a single-account
result) runs the real, paid pipeline (agent.run_account) for a genuine
explained assessment — that call is never triggered just by asking a
question.
"""
from __future__ import annotations

import sys
from datetime import date

from flask import Flask, Response, jsonify, request

import agent
import nlquery
import prescreen
import salesforce_client

# See run.py for why this is necessary on Windows (console codepage mojibake,
# confirmed live this session). Not strictly required for HTTP responses
# (those go over a socket, not the console), but cheap, consistent insurance
# for whatever this process itself prints (the Flask dev server's own log).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

app = Flask(__name__)

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Renewal Risk Agent</title>
<style>
  body { font-family: -apple-system, Segoe UI, Arial, sans-serif; max-width: 780px; margin: 40px auto; padding: 0 16px; color: #1a1a1a; }
  h1 { font-size: 1.4rem; }
  .sub { color: #666; margin-top: -8px; margin-bottom: 24px; }
  #q { width: 100%; font-size: 1rem; padding: 10px 12px; box-sizing: border-box; border: 1px solid #ccc; border-radius: 6px; }
  #ask-btn { margin-top: 10px; padding: 10px 18px; font-size: 1rem; border: none; border-radius: 6px; background: #2563eb; color: white; cursor: pointer; }
  #ask-btn:disabled { background: #93b4f0; cursor: default; }
  #status { color: #666; margin-top: 10px; font-style: italic; }
  #results { margin-top: 24px; }
  .card { border: 1px solid #ddd; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
  .flagged { border-left: 5px solid #dc2626; }
  .clear { border-left: 5px solid #16a34a; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 0.85rem; font-weight: 600; }
  .badge.high { background: #fee2e2; color: #991b1b; }
  .badge.medium { background: #fef3c7; color: #92400e; }
  .badge.ok { background: #dcfce7; color: #166534; }
  .notice { background: #fef3c7; border: 1px solid #fde68a; border-radius: 6px; padding: 10px 14px; }
  .error { background: #fee2e2; border: 1px solid #fecaca; border-radius: 6px; padding: 10px 14px; }
  ul.signals { padding-left: 20px; }
  table { width: 100%; border-collapse: collapse; margin-top: 12px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #eee; font-size: 0.92rem; }
  #explain-btn { margin-top: 12px; padding: 8px 14px; border: 1px solid #2563eb; border-radius: 6px; background: white; color: #2563eb; cursor: pointer; }
  #explain-btn:disabled { opacity: 0.6; cursor: default; }
  .rationale { white-space: pre-wrap; margin-top: 10px; }
  .cand { cursor: pointer; color: #2563eb; text-decoration: underline; }
</style>
</head>
<body>
<h1>Renewal Risk Agent</h1>
<p class="sub">Ask in plain English &mdash; e.g. "is Ridgeline Materials at risk" or "accounts at risk this quarter".</p>
<input id="q" type="text" placeholder="Type a question..." autofocus>
<button id="ask-btn" onclick="ask()">Ask</button>
<div id="status"></div>
<div id="results"></div>

<script>
const q = document.getElementById('q');
const askBtn = document.getElementById('ask-btn');
const statusEl = document.getElementById('status');
const results = document.getElementById('results');

q.addEventListener('keydown', e => { if (e.key === 'Enter') ask(); });

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function signalLine(s) {
  return `<li><strong>${escapeHtml(s.name)}</strong> (${escapeHtml(s.severity)}): ${escapeHtml(s.value)}</li>`;
}

async function ask() {
  const question = q.value.trim();
  if (!question) return;
  askBtn.disabled = true;
  const isBulk = /this\\s+(quarter|quater|half|year)/i.test(question) && /risk/i.test(question);
  statusEl.textContent = isBulk
    ? 'Screening accounts... a full window can take a few minutes (each account needs several real Salesforce lookups).'
    : 'Looking that up...';
  results.innerHTML = '';
  try {
    const resp = await fetch('/ask', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question}),
    });
    const data = await resp.json();
    render(data);
  } catch (err) {
    results.innerHTML = `<div class="error">Something went wrong: ${escapeHtml(err)}</div>`;
  } finally {
    askBtn.disabled = false;
    statusEl.textContent = '';
  }
}

function render(data) {
  if (data.type === 'error') {
    results.innerHTML = `<div class="error">${escapeHtml(data.message)}</div>`;
  } else if (data.type === 'not_found') {
    results.innerHTML = `<div class="notice">${escapeHtml(data.message)}</div>`;
  } else if (data.type === 'ambiguous') {
    const items = data.candidates.map(c =>
      `<li class="cand" onclick="q.value='is ' + '${escapeHtml(c.name).replace(/'/g, "\\\\'")}' + ' at risk'; ask();">${escapeHtml(c.name)}</li>`
    ).join('');
    results.innerHTML = `<div class="notice">${escapeHtml(data.message)}<ul>${items}</ul></div>`;
  } else if (data.type === 'single_account') {
    renderSingleAccount(data);
  } else if (data.type === 'bulk_window') {
    renderBulkWindow(data);
  }
}

function renderSingleAccount(r) {
  const cls = r.flag ? 'flagged' : 'clear';
  const badge = r.flag
    ? `<span class="badge high">FLAGGED &mdash; ${r.high_severity_count} high, ${r.medium_severity_count} medium</span>`
    : `<span class="badge ok">No signals detected</span>`;
  const signals = r.signals.length ? `<ul class="signals">${r.signals.map(signalLine).join('')}</ul>` : '<p>No risk signals found.</p>';
  const gaps = r.gaps && r.gaps.length ? `<p><em>Gaps: ${escapeHtml(r.gaps.join('; '))}</em></p>` : '';
  results.innerHTML = `
    <div class="card ${cls}">
      <h2>${escapeHtml(r.account_name)}</h2>
      ${badge}
      ${signals}
      ${gaps}
      <p style="color:#666; font-size:0.85rem;">Free heuristic screen &mdash; no AI call, fixed thresholds.</p>
      <button id="explain-btn" onclick="explain('${r.account_id}', '${escapeHtml(r.account_name).replace(/'/g, "\\\\'")}')">Get full AI explanation (real cost, ~15-30s)</button>
      <div id="explain-area"></div>
    </div>`;
}

async function explain(accountId, accountName) {
  const btn = document.getElementById('explain-btn');
  btn.disabled = true;
  btn.textContent = 'Calling Claude... this can take 15-30s';
  const area = document.getElementById('explain-area');
  try {
    const resp = await fetch('/explain', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({account_id: accountId, account_name: accountName}),
    });
    const data = await resp.json();
    if (data.type === 'error') {
      area.innerHTML = `<div class="error">${escapeHtml(data.message)}</div>`;
    } else {
      const badgeClass = data.band === 'high' ? 'high' : (data.band === 'medium' ? 'medium' : 'ok');
      area.innerHTML = `
        <hr>
        <p><span class="badge ${badgeClass}">Score ${data.score} (${escapeHtml(data.band)})</span> ${data.abstained ? '<em>(model abstained)</em>' : ''}</p>
        <div class="rationale">${escapeHtml(data.rationale)}</div>
        <p><strong>Drivers:</strong></p>
        <ul>${data.drivers.map(d => `<li>${escapeHtml(d)}</li>`).join('')}</ul>`;
    }
  } catch (err) {
    area.innerHTML = `<div class="error">Something went wrong: ${escapeHtml(err)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Get full AI explanation (real cost, ~15-30s)';
  }
}

function renderBulkWindow(r) {
  const capNote = r.capped
    ? `<div class="notice">Screened the soonest-renewing ${r.screened_count} of ${r.total_matching} matching accounts.</div>`
    : '';
  const rows = r.results.map(row => `
    <tr>
      <td>${escapeHtml(row.name)}</td>
      <td>${escapeHtml(row.close_date || '')}</td>
      <td>${row.amount != null ? '$' + Number(row.amount).toLocaleString() : ''}</td>
      <td>${row.flag ? '<span class="badge high">Flagged</span>' : '<span class="badge ok">Clear</span>'}</td>
      <td>${row.high_severity_count}</td>
      <td>${row.medium_severity_count}</td>
    </tr>`).join('');
  const flaggedCount = r.results.filter(x => x.flag).length;
  results.innerHTML = `
    <p><strong>${escapeHtml(r.period)}</strong>: ${r.window_start} &ndash; ${r.window_end} &mdash; ${flaggedCount} of ${r.screened_count} flagged</p>
    ${capNote}
    <table>
      <thead><tr><th>Account</th><th>Renewal</th><th>Amount</th><th>Status</th><th>High</th><th>Medium</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="6">No renewals found in this window.</td></tr>'}</tbody>
    </table>
    <p style="color:#666; font-size:0.85rem;">Free heuristic screen &mdash; no AI call. Ask "is &lt;company&gt; at risk" for a full explanation on any one of these.</p>`;
}
</script>
</body>
</html>
"""


@app.route("/")
def index() -> str:
    return INDEX_HTML


@app.route("/ask", methods=["POST"])
def ask() -> Response:
    body = request.get_json(force=True, silent=True) or {}
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"type": "error", "message": "Type a question first."})

    try:
        intent, params = nlquery.parse_intent(question)
    except nlquery.IntentParseError as exc:
        return jsonify({"type": "error", "message": str(exc)})

    if intent == "single_account":
        try:
            result = nlquery.answer_single_account(params["company_name"])
        except prescreen.AccountLookupError as exc:
            return jsonify({
                "type": "ambiguous" if exc.candidates else "not_found",
                "message": str(exc),
                "candidates": exc.candidates,
            })
        except salesforce_client.SalesforceQueryError as exc:
            return jsonify({"type": "error", "message": f"Salesforce error: {exc}"})
        return jsonify({"type": "single_account", **result})

    try:
        result = nlquery.answer_bulk_window(params["period"])
    except salesforce_client.SalesforceQueryError as exc:
        return jsonify({"type": "error", "message": f"Salesforce error: {exc}"})
    return jsonify({"type": "bulk_window", **result})


@app.route("/explain", methods=["POST"])
def explain() -> tuple[Response, int] | Response:
    body = request.get_json(force=True, silent=True) or {}
    account_id = body.get("account_id")
    account_name = body.get("account_name")
    if not account_id:
        return jsonify({"type": "error", "message": "Missing account_id."}), 400

    as_of = date.today().isoformat()
    try:
        result = agent.run_account(account_id, as_of)
    except salesforce_client.SalesforceQueryError as exc:
        return jsonify({"type": "error", "message": f"Salesforce error: {exc}"})
    except Exception as exc:  # noqa: BLE001 - surface Anthropic/API errors, don't crash the server
        return jsonify({"type": "error", "message": f"Full assessment failed: {exc}"})

    agent.write_brief(result, account_name)
    agent.append_briefs_csv(result, account_name)
    a = result.assessment
    return jsonify({
        "type": "explanation",
        "account_id": account_id,
        "score": a["score"],
        "band": a["band"],
        "abstained": a["abstained"],
        "drivers": a["drivers"],
        "rationale": a["rationale"],
        "citations": a["citations"],
        "leakage_violations": result.leakage_violations,
    })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
