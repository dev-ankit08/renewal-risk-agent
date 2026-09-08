"""
config.py — models, org alias, cost rates. Configure this via a .env file in
the repo root (copy .env.example -> .env) or real environment variables —
both work; .env is loaded first via python-dotenv if present, but real env
vars always take precedence over it.
"""
import os

from dotenv import load_dotenv

load_dotenv()  # no-op if .env doesn't exist; does not override existing os.environ

# --- Model selection ---------------------------------------------------------
COORDINATOR_MODEL = "claude-opus-5"   # planner only, no tools
SPECIALIST_MODEL = "claude-sonnet-5"  # Commercial + Relationship, run in parallel
SYNTHESIS_MODEL = "claude-opus-5"     # packets only, never raw records

# --- Required configuration ----------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# Salesforce org alias, already authenticated via the `sf` CLI on this machine.
# There is no token/URL to configure here — auth is inherited entirely from
# whatever `sf` CLI session is active. See salesforce_client.py.
SALESFORCE_ORG_ALIAS = os.environ.get("RENEWAL_AGENT_SF_ORG_ALIAS", "renewalRiskSandbox")

# Explicit opt-in required before querying any org alias other than the known
# synthetic test sandbox — see salesforce_client.get_client(). Real Salesforce
# data sent through this pipeline reaches the Anthropic API in specialist
# prompts; confirm a Zero Data Retention agreement is in place with Anthropic
# before setting this true for a real org.
ZDR_CONFIRMED = os.environ.get("RENEWAL_AGENT_ZDR_CONFIRMED", "false").lower() == "true"

# --- Tool budget --------------------------------------------------------------
# Coordinator sets this per run; currently descriptive only — nothing enforces
# it yet (no specialist call is capped by it).
DEFAULT_TOOL_BUDGET_PER_SPECIALIST = 8

# --- As-of window --------------------------------------------------------------
# Documents the "120 days before renewal" trigger concept. Not currently
# computed anywhere — run.py always requires an explicit --as-of.
DEFAULT_LOOKAHEAD_DAYS = 120

# --- sf CLI ----------------------------------------------------------------------
SF_CLI_TIMEOUT_SECONDS = 60

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
