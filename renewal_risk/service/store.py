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
