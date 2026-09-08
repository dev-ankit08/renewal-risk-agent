"""
config.py — the file you edit. Models, MCP toolset, mock-vs-real switch, rates.
"""
import os

# --- Model selection ---------------------------------------------------------
COORDINATOR_MODEL = "claude-opus-5"   # planner only, no tools
SPECIALIST_MODEL = "claude-sonnet-5"  # Commercial + Relationship, run in parallel
SYNTHESIS_MODEL = "claude-opus-5"     # packets only, never raw records

# --- Dry-run switch -----------------------------------------------------------
# True  -> stub_model.py (canned responses) + MockSalesforce (fixture data). No creds needed.
# False -> real Anthropic API + MCPSalesforce. Requires the three env vars below.
# Note: MCPSalesforce isn't wired up yet (see salesforce_client.py), so leave
# this True and use USE_STUB_MODEL below to call the real Anthropic API
# against mock Salesforce data instead.
USE_STUB = os.environ.get("RENEWAL_AGENT_USE_STUB", "true").lower() != "false"

# Model-only stub switch, independent of USE_STUB above. Defaults to whatever
# USE_STUB is, so existing dry-run/real-run behavior is unchanged unless you
# set RENEWAL_AGENT_USE_STUB_MODEL explicitly — e.g. real model + mock
# Salesforce: RENEWAL_AGENT_USE_STUB_MODEL=false with USE_STUB left true.
USE_STUB_MODEL = os.environ.get("RENEWAL_AGENT_USE_STUB_MODEL", str(USE_STUB)).lower() != "false"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
SALESFORCE_TOKEN = os.environ.get("SALESFORCE_TOKEN")
SALESFORCE_MCP_URL = os.environ.get("SALESFORCE_MCP_URL")

# --- MCP toolset / allowlist posture ------------------------------------------
# Only these three tools are ever exposed to a specialist. No write tool
# exists in this toolset — see salesforce_client.MCPSalesforce, which has no
# insert/update/delete method — and guard.soql_guard hard-blocks DML as a
# second, independent layer.
MCP_ALLOWED_TOOLS = ("soql_query", "get_record", "describe_object")
MCP_WRITES_ENABLED = False  # do not flip on for this POC

# --- Tool budget --------------------------------------------------------------
# Coordinator sets this per run; specialists stop calling tools once spent.
DEFAULT_TOOL_BUDGET_PER_SPECIALIST = 8

# --- As-of window --------------------------------------------------------------
DEFAULT_LOOKAHEAD_DAYS = 120

# --- Cost rates ------------------------------------------------------------------
# Placeholder $/1M tokens for the POC cost estimate only.
# VERIFY AT https://claude.com/pricing BEFORE ANY COST SLIDE.
RATES_PER_MILLION_TOKENS = {
    "claude-opus-5": {"input": 15.00, "output": 75.00},
    "claude-sonnet-5": {"input": 3.00, "output": 15.00},
}

# --- Output paths ----------------------------------------------------------------
BRIEFS_DIR = "briefs"
BRIEFS_CSV = "briefs.csv"
RUNS_DB = "runs.sqlite"
