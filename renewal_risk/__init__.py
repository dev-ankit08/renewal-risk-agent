"""
renewal_risk — a platform-neutral renewal-risk agent.

Data comes from any number of configured sources (CRM, billing, ERP, support
desk, databases, CSV exports, any MCP server). Each source's records are mapped
into one standard data model (core/model.py), linked to a single account list,
screened by a configurable signal engine, and explained by a Claude pipeline
(coordinator -> specialists -> synthesis). Results are served over a REST API,
a Slack app, scheduled alerts and a CLI.

Everything a company changes lives in its config directory (default ./config,
override with RENEWAL_RISK_CONFIG_DIR) — never in this package.
"""

__version__ = "2.0.0"
