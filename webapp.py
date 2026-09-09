#!/usr/bin/env python3
"""
webapp.py — a local web page for plain-English risk questions.

    python webapp.py
    then open http://127.0.0.1:5000

One text box. Type "is <company> at risk" or "accounts at risk this
quarter/half/year". Both question types trigger the REAL Coordinator ->
Specialists -> Synthesis pipeline (agent.run_account) — every answer costs
real Anthropic tokens and takes real time (single account: ~15-30s; a bulk
window: up to a few minutes, capped). There is no free/instant tier on this
page — see run.py's separate `prescreen` command for that.
"""
from __future__ import annotations

import sys

from flask import Flask, Response, jsonify, request

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
  .badge { display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 0.85rem; font-weight: 600; }
  .badge.high { background: #fee2e2; color: #991b1b; }
  .badge.medium { background: #fef3c7; color: #92400e; }
  .badge.ok, .badge.low { background: #dcfce7; color: #166534; }
  .notice { background: #fef3c7; border: 1px solid #fde68a; border-radius: 6px; padding: 10px 14px; }
  .error { background: #fee2e2; border: 1px solid #fecaca; border-radius: 6px; padding: 10px 14px; }
  table { width: 100%; border-collapse: collapse; margin-top: 12px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #eee; font-size: 0.92rem; vertical-align: top; }
  tr.row { cursor: pointer; }
  tr.row:hover { background: #f8fafc; }
  .detail { display: none; background: #f8fafc; }
  .detail.open { display: table-row; }
  .rationale { white-space: pre-wrap; margin-top: 6px; }
  .cand { cursor: pointer; color: #2563eb; text-decoration: underline; }
</style>
</head>
<body>
<h1>Renewal Risk Agent</h1>
<p class="sub">Ask in plain English &mdash; e.g. "is Ridgeline Materials at risk" or "accounts at risk this quarter". Every answer is a real, AI-explained assessment (not a heuristic guess) &mdash; costs real API credits and takes real time.</p>
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

function bandClass(band) {
  return band === 'high' ? 'high' : (band === 'medium' ? 'medium' : 'ok');
}

async function ask() {
  const question = q.value.trim();
  if (!question) return;
  askBtn.disabled = true;
  const isBulk = /this\\s+(quarter|quater|half|year)/i.test(question) && /risk/i.test(question);
  statusEl.textContent = isBulk
    ? 'Running the real AI pipeline on every matching account... this can take a few minutes and uses real API credits.'
    : 'Asking the AI agent (Coordinator -> Specialists -> Synthesis)... ~15-30s, uses real API credits.';
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
  results.innerHTML = `
    <div class="card">
      <h2>${escapeHtml(r.account_name)}</h2>
      <p><span class="badge ${bandClass(r.band)}">Score ${r.score} (${escapeHtml(r.band)})</span> ${r.abstained ? '<em>(model abstained)</em>' : ''}</p>
      <div class="rationale">${escapeHtml(r.rationale)}</div>
      <p><strong>Drivers:</strong></p>
      <ul>${r.drivers.map(d => `<li>${escapeHtml(d)}</li>`).join('') || '<li>none</li>'}</ul>
    </div>`;
}

function renderBulkWindow(r) {
  const capNote = r.capped
    ? `<div class="notice">Ran the real AI pipeline on the soonest-renewing ${r.screened_count} of ${r.total_matching} matching accounts (estimated cost: ~$${r.estimated_cost_usd}).</div>`
    : `<div class="notice">Estimated cost: ~$${r.estimated_cost_usd} for ${r.screened_count} account(s).</div>`;
  const rows = r.results.map((row, i) => `
    <tr class="row" onclick="toggleDetail(${i})">
      <td>${escapeHtml(row.name)}</td>
      <td>${escapeHtml(row.close_date || '')}</td>
      <td>${row.amount != null ? '$' + Number(row.amount).toLocaleString() : ''}</td>
      <td><span class="badge ${bandClass(row.band)}">${row.score} (${escapeHtml(row.band)})</span></td>
    </tr>
    <tr class="detail" id="detail-${i}">
      <td colspan="4">
        <div class="rationale">${escapeHtml(row.rationale)}</div>
        <p><strong>Drivers:</strong></p>
        <ul>${row.drivers.map(d => `<li>${escapeHtml(d)}</li>`).join('') || '<li>none</li>'}</ul>
      </td>
    </tr>`).join('');
  results.innerHTML = `
    <p><strong>${escapeHtml(r.period)}</strong>: ${r.window_start} &ndash; ${r.window_end} &mdash; ${r.screened_count} account(s) assessed</p>
    ${capNote}
    <table>
      <thead><tr><th>Account</th><th>Renewal</th><th>Amount</th><th>Score</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="4">No renewals found in this window.</td></tr>'}</tbody>
    </table>
    <p style="color:#666; font-size:0.85rem;">Click a row for the full explanation.</p>`;
}

function toggleDetail(i) {
  document.getElementById('detail-' + i).classList.toggle('open');
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
        except Exception as exc:  # noqa: BLE001 - surface Anthropic/API errors, don't crash the server
            return jsonify({"type": "error", "message": f"Full assessment failed: {exc}"})
        return jsonify({"type": "single_account", **result})

    try:
        result = nlquery.answer_bulk_window(params["period"])
    except salesforce_client.SalesforceQueryError as exc:
        return jsonify({"type": "error", "message": f"Salesforce error: {exc}"})
    return jsonify({"type": "bulk_window", **result})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
