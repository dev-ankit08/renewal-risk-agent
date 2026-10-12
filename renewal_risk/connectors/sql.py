"""
sql.py — any database SQLAlchemy can reach (Postgres, MySQL, SQL Server,
Snowflake, BigQuery, SQLite, Oracle... with the matching driver installed).

  type: sql
  dsn: ${ENV:USAGE_DB_DSN}         # e.g. postgresql+psycopg://user:pw@host/db
  entities:
    usage_metric:
      fetch: {query: "SELECT id, metric, day, value FROM usage WHERE tenant_id = :link_value AND day <= :as_of"}
      fields: {id: id, metric: metric, period_start: "day | date", value: value, created_at: "day | date"}

Values are always passed as bind parameters (:link_value, :as_of, :text,
:before, :after, :limit), never string-formatted into the query. Only a
single SELECT / WITH statement is accepted, and every query runs inside a
transaction that is rolled back — the connector cannot write.
"""
from __future__ import annotations

import re

from ..core.connector import Connector, ConnectorError, HealthResult, register

_READ_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_WRITE_WORDS = re.compile(r"\b(insert|update|delete|merge|upsert|drop|alter|create|truncate|grant|revoke|call|exec|execute)\b", re.IGNORECASE)
_BIND = re.compile(r"(?<!:):([a-z_]+)")


def check_read_only(query: str) -> None:
    stripped = query.strip().rstrip(";")
    if ";" in stripped:
        raise ConnectorError("only a single SQL statement is allowed")
    if not _READ_ONLY.match(stripped):
        raise ConnectorError("only SELECT / WITH queries are allowed")
    # Quoted literals are ignored so a WHERE status = 'deleted' still passes.
    unquoted = re.sub(r"'(?:[^']|'')*'", "''", stripped)
    if _WRITE_WORDS.search(unquoted):
        raise ConnectorError(f"query contains a write keyword: {query!r}")


@register("sql")
class SqlConnector(Connector):
    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        try:
            import sqlalchemy
        except ImportError as exc:
            raise ConnectorError("the sql connector needs `pip install sqlalchemy` plus a database driver") from exc
        self._sa = sqlalchemy
        dsn = config.get("dsn")
        if dsn and "{config_dir}" in dsn:  # lets a sqlite path sit next to the config
            dsn = dsn.replace("{config_dir}", str(config.get("_config_dir", ".")).replace("\\", "/"))
        if not dsn:
            raise ConnectorError(f"source '{name}': `dsn` is required")
        try:
            self.engine = sqlalchemy.create_engine(dsn, pool_pre_ping=True)
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(f"source '{name}': cannot create engine: {exc}") from exc

    def execute(self, request: dict, ctx: dict, mode: str) -> list[dict]:
        query = request.get("query")
        if not query:
            raise ConnectorError(f"source '{self.name}': request needs `query:`")
        check_read_only(query)
        params = {name: ctx.get(name) for name in set(_BIND.findall(query))}
        missing = [n for n, v in params.items() if v is None]
        if missing:
            raise ConnectorError(f"query uses unknown bind parameter(s): {', '.join(missing)}")
        if mode == "search" and "text" in params:
            params["text"] = f"%{params['text']}%" if request.get("like", True) else params["text"]
        try:
            with self.engine.connect() as conn:
                trans = conn.begin()
                try:
                    result = conn.execute(self._sa.text(query), params)
                    rows = [dict(r._mapping) for r in result]
                finally:
                    trans.rollback()
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(f"source '{self.name}': query failed: {exc}") from exc
        return rows

    def health(self) -> HealthResult:
        try:
            with self.engine.connect() as conn:
                conn.execute(self._sa.text("SELECT 1"))
            return HealthResult(True, f"connected ({self.engine.dialect.name})")
        except Exception as exc:  # noqa: BLE001
            return HealthResult(False, f"cannot connect: {exc}")

    def close(self) -> None:
        self.engine.dispose()
