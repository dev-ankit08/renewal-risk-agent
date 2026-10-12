"""
csv_file.py — a folder of CSV (or XLSX) exports.

Useful for demos, for systems with no API, and for anything an admin can
export on a schedule. Source config:

  type: csv
  path: ./data/erp-export          # folder, relative to the config directory
  entities:
    invoice:
      fetch: {file: invoices.csv, match: customer_no}   # rows where customer_no == link_value
      list:  {file: invoices.csv}
      fields: {...}
"""
from __future__ import annotations

import csv
from pathlib import Path

from ..core.connector import Connector, ConnectorError, HealthResult, register
from ..core.settings import config_dir


@register("csv")
class CsvConnector(Connector):
    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        base = Path(config.get("path") or ".")
        self.folder = base if base.is_absolute() else (Path(config.get("_config_dir") or config_dir()) / base)
        self._cache: dict[str, list[dict]] = {}

    def _read(self, filename: str) -> list[dict]:
        if filename in self._cache:
            return self._cache[filename]
        path = (self.folder / filename).resolve()
        if self.folder.resolve() not in path.parents:
            raise ConnectorError(f"file '{filename}' is outside the source folder")
        if not path.exists():
            raise ConnectorError(f"source '{self.name}': file not found: {path}")
        if path.suffix.lower() in (".xlsx", ".xlsm"):
            rows = self._read_xlsx(path)
        else:
            with open(path, newline="", encoding="utf-8-sig") as f:
                rows = [dict(r) for r in csv.DictReader(f)]
        self._cache[filename] = rows
        return rows

    @staticmethod
    def _read_xlsx(path: Path) -> list[dict]:
        try:
            import openpyxl
        except ImportError as exc:
            raise ConnectorError("reading .xlsx needs `pip install openpyxl`") from exc
        sheet = openpyxl.load_workbook(path, read_only=True, data_only=True).active
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            return []
        header = [str(h) if h is not None else f"col{i}" for i, h in enumerate(rows[0])]
        return [dict(zip(header, r)) for r in rows[1:]]

    def execute(self, request: dict, ctx: dict, mode: str) -> list[dict]:
        rows = self._read(request["file"])
        if mode == "fetch":
            column = request.get("match")
            if not column:
                raise ConnectorError(f"source '{self.name}': fetch on '{request['file']}' needs `match: <column>`")
            wanted = str(ctx["link_value"]).strip().lower()
            rows = [r for r in rows if str(r.get(column) or "").strip().lower() == wanted]
        elif mode == "search":
            column = request.get("match", "name")
            needle = str(ctx.get("text", "")).strip().lower()
            rows = [r for r in rows if needle in str(r.get(column) or "").lower()]
        return rows

    def health(self) -> HealthResult:
        if not self.folder.exists():
            return HealthResult(False, f"folder not found: {self.folder}")
        return super().health()
