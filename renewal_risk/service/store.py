"""
store.py — SQLite persistence (stdlib only; one file under the state dir).

Every table carries tenant_id so the same schema serves a multi-tenant
deployment later; a self-hosted install uses the single tenant "default".

  runs              append-only stage ledger (evaluate.py reads it)
  assessments       one row per completed assessment (latest = current view)
  alerts            last band alerted on per account (de-duplication)
  feedback          a person's agree/disagree on an assessment + real outcomes
  admin_users       admin-UI logins (scrypt password hashes)
  config_versions   every config file the admin UI saved (audit + rollback)
  mapping_versions  every approved field mapping
  action_items      recommended actions sent to their owners, and what they did
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (tenant_id TEXT NOT NULL DEFAULT 'default', account_id TEXT, as_of TEXT, stage TEXT, detail TEXT, ts TEXT);
CREATE INDEX IF NOT EXISTS runs_account ON runs(tenant_id, account_id, stage);
CREATE TABLE IF NOT EXISTS assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL DEFAULT 'default',
    account_id TEXT NOT NULL, account_name TEXT, as_of TEXT, score REAL, band TEXT, abstained INTEGER,
    cost_usd REAL, result TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS assessments_account ON assessments(tenant_id, account_id, created_at);
CREATE TABLE IF NOT EXISTS alerts (
    tenant_id TEXT NOT NULL DEFAULT 'default', account_id TEXT NOT NULL, band TEXT, score REAL, sent_at REAL,
    PRIMARY KEY (tenant_id, account_id)
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL DEFAULT 'default',
    account_id TEXT NOT NULL, assessment_id INTEGER, user_email TEXT, verdict TEXT, outcome TEXT, note TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_users (
    tenant_id TEXT NOT NULL DEFAULT 'default', email TEXT NOT NULL, password_hash TEXT NOT NULL, created_at REAL NOT NULL,
    PRIMARY KEY (tenant_id, email)
);
CREATE TABLE IF NOT EXISTS config_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL DEFAULT 'default',
    name TEXT NOT NULL, content TEXT NOT NULL, saved_by TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS action_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL DEFAULT 'default',
    account_id TEXT NOT NULL, account_name TEXT, assessment_id INTEGER, playbook_id TEXT, action_key TEXT NOT NULL,
    action TEXT NOT NULL, owner_email TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', due_at REAL,
    snoozed_until REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS action_items_key ON action_items(tenant_id, account_id, action_key, owner_email);
CREATE TABLE IF NOT EXISTS mapping_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL DEFAULT 'default',
    source TEXT NOT NULL, entity TEXT NOT NULL, fields TEXT NOT NULL, approved_by TEXT, created_at REAL NOT NULL
);
"""

# Columns added after the first release; older databases are migrated in place.
_MIGRATIONS = {"runs": "tenant_id", "assessments": "tenant_id"}


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, salt, digest = stored.split("$")
        candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(candidate.hex(), digest)
    except (ValueError, TypeError):
        return False


class Store:
    def __init__(self, path: str | Path, tenant_id: str = "default"):
        self.path = str(path)
        self.tenant_id = tenant_id
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as conn:
            for table, column in _MIGRATIONS.items():
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
                if cols and column not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT 'default'")
            if [r[1] for r in conn.execute("PRAGMA table_info(alerts)")] == ["account_id", "band", "score", "sent_at"]:
                conn.execute("DROP TABLE alerts")  # v1 alerts table had no tenant; alert state is disposable
            conn.executescript(_SCHEMA)

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _write(self, sql: str, params: tuple):
        with self._lock, self._conn() as conn:
            return conn.execute(sql, params)

    def _one(self, sql: str, params: tuple):
        with self._conn() as conn:
            return conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple) -> list:
        with self._conn() as conn:
            return conn.execute(sql, params).fetchall()

    # -- ledger / assessments ------------------------------------------------

    def log(self, account_id: str, as_of: str, stage: str, detail) -> None:
        self._write("INSERT INTO runs (tenant_id, account_id, as_of, stage, detail, ts) VALUES (?, ?, ?, ?, ?, ?)",
                    (self.tenant_id, account_id, as_of, stage, json.dumps(detail, default=str), time.strftime("%Y-%m-%dT%H:%M:%S")))

    def save_assessment(self, result) -> int:
        a = result.assessment
        cur = self._write(
            "INSERT INTO assessments (tenant_id, account_id, account_name, as_of, score, band, abstained, cost_usd, result, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.tenant_id, result.account_id, result.account_name, result.as_of, a["score"], a["band"], int(a["abstained"]),
             result.cost_usd, json.dumps(result.to_dict(), default=str), result.created_at),
        )
        return cur.lastrowid

    def latest_assessment(self, account_id: str, max_age_hours: float | None = None) -> dict | None:
        row = self._one("SELECT id, result, created_at FROM assessments WHERE tenant_id = ? AND account_id = ? ORDER BY created_at DESC LIMIT 1",
                        (self.tenant_id, account_id))
        if row is None or (max_age_hours is not None and time.time() - row["created_at"] > max_age_hours * 3600):
            return None
        return {**json.loads(row["result"]), "assessment_id": row["id"]}

    def previous_band(self, account_id: str, before: float) -> str | None:
        row = self._one("SELECT band FROM assessments WHERE tenant_id = ? AND account_id = ? AND created_at < ? ORDER BY created_at DESC LIMIT 1",
                        (self.tenant_id, account_id, before))
        return row["band"] if row else None

    def assessment_stats(self, days: int = 30) -> list[dict]:
        since = time.time() - days * 86400
        rows = self._all(
            "SELECT date(created_at, 'unixepoch') AS day, COUNT(*) AS assessments, ROUND(SUM(cost_usd), 4) AS cost_usd,"
            " SUM(band = 'high') AS high FROM assessments WHERE tenant_id = ? AND created_at >= ? GROUP BY day ORDER BY day",
            (self.tenant_id, since))
        return [dict(r) for r in rows]

    def purge_accounts(self, account_ids: list[str]) -> None:
        """Forget everything recorded about these accounts (the demo workspace's, on leaving it)."""
        marks = ",".join("?" * len(account_ids))
        for table in ("assessments", "runs", "alerts", "feedback", "action_items"):
            self._write(f"DELETE FROM {table} WHERE tenant_id = ? AND account_id IN ({marks})", (self.tenant_id, *account_ids))

    def recent_assessments(self, limit: int = 50) -> list[dict]:
        rows = self._all("SELECT id, account_id, account_name, as_of, score, band, cost_usd, created_at FROM assessments"
                         " WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?", (self.tenant_id, limit))
        return [dict(r) for r in rows]

    # -- alerts --------------------------------------------------------------

    def last_alert(self, account_id: str) -> dict | None:
        row = self._one("SELECT * FROM alerts WHERE tenant_id = ? AND account_id = ?", (self.tenant_id, account_id))
        return dict(row) if row else None

    def record_alert(self, account_id: str, band: str, score: float) -> None:
        self._write("INSERT INTO alerts (tenant_id, account_id, band, score, sent_at) VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(tenant_id, account_id) DO UPDATE SET band = excluded.band, score = excluded.score, sent_at = excluded.sent_at",
                    (self.tenant_id, account_id, band, score, time.time()))

    def clear_alert(self, account_id: str) -> None:
        self._write("DELETE FROM alerts WHERE tenant_id = ? AND account_id = ?", (self.tenant_id, account_id))

    # -- feedback ------------------------------------------------------------

    def add_feedback(self, account_id: str, user_email: str | None, verdict: str | None = None, outcome: str | None = None,
                     note: str | None = None, assessment_id: int | None = None) -> int:
        cur = self._write("INSERT INTO feedback (tenant_id, account_id, assessment_id, user_email, verdict, outcome, note, created_at)"
                          " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                          (self.tenant_id, account_id, assessment_id, user_email, verdict, outcome, (note or "")[:500], time.time()))
        return cur.lastrowid

    def feedback(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM feedback WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?", (self.tenant_id, limit))]

    # -- admin users ---------------------------------------------------------

    def admin_count(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM admin_users WHERE tenant_id = ?", (self.tenant_id,))["n"]

    def create_admin(self, email: str, password: str) -> None:
        if len(password) < 10:
            raise ValueError("password must be at least 10 characters")
        self._write("INSERT INTO admin_users (tenant_id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                    (self.tenant_id, email.strip().lower(), hash_password(password), time.time()))

    def check_admin(self, email: str, password: str) -> bool:
        row = self._one("SELECT password_hash FROM admin_users WHERE tenant_id = ? AND email = ?", (self.tenant_id, (email or "").strip().lower()))
        return bool(row) and verify_password(password, row["password_hash"])

    # -- config + mapping history --------------------------------------------

    def save_config_version(self, name: str, content: str, saved_by: str | None) -> None:
        self._write("INSERT INTO config_versions (tenant_id, name, content, saved_by, created_at) VALUES (?, ?, ?, ?, ?)",
                    (self.tenant_id, name, content, saved_by, time.time()))

    def save_mapping_version(self, source: str, entity: str, fields: dict, approved_by: str | None) -> None:
        self._write("INSERT INTO mapping_versions (tenant_id, source, entity, fields, approved_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (self.tenant_id, source, entity, json.dumps(fields), approved_by, time.time()))

    # -- action items ----------------------------------------------------------

    def should_notify(self, account_id: str, action_key: str, owner_email: str, cooldown_days: float = 30) -> bool:
        """False while the same action for the same person is open, snoozed, or
        was closed recently — so a scan never repeats a DM."""
        now = time.time()
        row = self._one(
            "SELECT status, snoozed_until, updated_at FROM action_items WHERE tenant_id = ? AND account_id = ? AND action_key = ?"
            " AND owner_email = ? ORDER BY updated_at DESC LIMIT 1", (self.tenant_id, account_id, action_key, owner_email))
        if row is None:
            return True
        if row["status"] == "open":
            return False
        if row["status"] == "snoozed":
            return (row["snoozed_until"] or 0) <= now
        return now - row["updated_at"] > cooldown_days * 86400

    def add_action_item(self, account_id: str, account_name: str | None, assessment_id: int | None, playbook_id: str,
                        action_key: str, action: str, owner_email: str, due_in_days: int) -> int:
        now = time.time()
        cur = self._write(
            "INSERT INTO action_items (tenant_id, account_id, account_name, assessment_id, playbook_id, action_key, action, owner_email,"
            " status, due_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
            (self.tenant_id, account_id, account_name, assessment_id, playbook_id, action_key, action[:300], owner_email,
             now + due_in_days * 86400, now, now))
        return cur.lastrowid

    def action_item(self, item_id: int) -> dict | None:
        row = self._one("SELECT * FROM action_items WHERE tenant_id = ? AND id = ?", (self.tenant_id, item_id))
        return dict(row) if row else None

    def set_action_status(self, item_id: int, status: str, snooze_days: float | None = None) -> dict | None:
        if status not in ("open", "done", "snoozed", "not_relevant"):
            raise ValueError(f"unknown status {status!r}")
        now = time.time()
        self._write("UPDATE action_items SET status = ?, snoozed_until = ?, updated_at = ? WHERE tenant_id = ? AND id = ?",
                    (status, now + snooze_days * 86400 if snooze_days else None, now, self.tenant_id, item_id))
        return self.action_item(item_id)

    def action_items(self, status: str | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM action_items WHERE tenant_id = ?" + (" AND status = ?" if status else "") + " ORDER BY created_at DESC LIMIT ?"
        params = (self.tenant_id, status, limit) if status else (self.tenant_id, limit)
        return [dict(r) for r in self._all(sql, params)]

    def action_summary(self, days: int = 30) -> dict:
        row = self._one(
            "SELECT COUNT(*) AS total, SUM(status = 'open') AS open, SUM(status = 'done') AS done,"
            " SUM(status = 'snoozed') AS snoozed, SUM(status = 'not_relevant') AS not_relevant,"
            " SUM(status = 'open' AND due_at < ?) AS overdue FROM action_items WHERE tenant_id = ? AND created_at >= ?",
            (time.time(), self.tenant_id, time.time() - days * 86400))
        summary = {k: int(row[k] or 0) for k in ("total", "open", "done", "snoozed", "not_relevant", "overdue")}
        summary["completion_rate"] = round(summary["done"] / summary["total"], 3) if summary["total"] else None
        return summary
