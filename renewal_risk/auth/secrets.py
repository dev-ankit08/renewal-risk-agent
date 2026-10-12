"""
secrets.py — where credentials entered in the admin UI live.

SecretStore is the interface; LocalSecretStore (encrypted SQLite) is the free
implementation. A Vault / cloud KMS store can replace it later without
touching callers.

Config refers to secrets as ${SECRET:<key>} (keys like `sources/billing/api_key`),
so connectors.yaml written by the UI never contains a credential. OAuth token
bundles are stored as JSON dicts under `sources/<name>/oauth` and read at
request time (they refresh), not interpolated into config.

Encryption: Fernet (AES-128-CBC + HMAC-SHA256). The key comes from
RENEWAL_RISK_MASTER_KEY; when it is unset a key file is generated next to the
store (`master.key`) — fine for a single-host install, but production should
set the env var from a real secret manager and back it up: losing the key
means re-entering every credential.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path


class SecretStoreError(Exception):
    pass


class SecretStore(ABC):
    @abstractmethod
    def get(self, key: str):
        """The stored value (str or dict), or None."""

    @abstractmethod
    def set(self, key: str, value) -> None: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def keys(self, prefix: str = "") -> list[str]: ...

    def delete_prefix(self, prefix: str) -> None:
        for key in self.keys(prefix):
            self.delete(key)


def _load_or_create_key(key_path: Path) -> bytes:
    env = os.environ.get("RENEWAL_RISK_MASTER_KEY")
    if env:
        return env.encode()
    from cryptography.fernet import Fernet

    if key_path.exists():
        return key_path.read_bytes().strip()
    key = Fernet.generate_key()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key)
    try:
        os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass  # Windows: rely on the directory's ACL
    return key


class LocalSecretStore(SecretStore):
    def __init__(self, path: str | Path, tenant_id: str = "default", key: bytes | None = None):
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:
            raise SecretStoreError("the secret store needs `pip install cryptography`") from exc
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.tenant_id = tenant_id
        self._fernet = Fernet(key or _load_or_create_key(self.path.parent / "master.key"))
        self._lock = threading.Lock()
        with self._conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS secrets (tenant_id TEXT NOT NULL, key TEXT NOT NULL, value BLOB NOT NULL,"
                " updated_at REAL NOT NULL, PRIMARY KEY (tenant_id, key))"
            )

    def _conn(self):
        return sqlite3.connect(self.path, timeout=30)

    def get(self, key: str):
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM secrets WHERE tenant_id = ? AND key = ?", (self.tenant_id, key)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(self._fernet.decrypt(row[0]).decode())
        except Exception as exc:  # noqa: BLE001 - wrong master key or corrupted row
            raise SecretStoreError(f"cannot decrypt secret '{key}' — was RENEWAL_RISK_MASTER_KEY changed?") from exc

    def set(self, key: str, value) -> None:
        token = self._fernet.encrypt(json.dumps(value).encode())
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO secrets (tenant_id, key, value, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(tenant_id, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (self.tenant_id, key, token, time.time()),
            )

    def delete(self, key: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM secrets WHERE tenant_id = ? AND key = ?", (self.tenant_id, key))

    def keys(self, prefix: str = "") -> list[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT key FROM secrets WHERE tenant_id = ? AND key LIKE ? ORDER BY key",
                                (self.tenant_id, prefix.replace("%", r"\%") + "%")).fetchall()
        return [r[0] for r in rows]


_default: dict[str, SecretStore] = {}
_default_lock = threading.Lock()


def default_store(state_dir: Path, tenant_id: str = "default") -> SecretStore:
    """One store per state dir + tenant for the process."""
    key = f"{state_dir}|{tenant_id}"
    with _default_lock:
        if key not in _default:
            _default[key] = LocalSecretStore(state_dir / "secrets.sqlite", tenant_id)
        return _default[key]
