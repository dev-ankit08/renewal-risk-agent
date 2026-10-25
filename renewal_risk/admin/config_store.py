"""
config_store.py — how the admin UI reads and writes configuration.

The UI writes exactly the files the CLI and GitOps use (connectors.yaml,
mappings/*.overrides.yaml, ...), so there is one source of truth. Every save
keeps the previous content in the store's config_versions table.

ConfigStore is the interface; FileConfigStore is the free implementation. A
database-backed store (per tenant) can replace it for a hosted edition.
"""
from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from pathlib import Path

import yaml

ALLOWED_NAMES = re.compile(
    r"^(connectors|agent|access|slack|signals|playbooks|sections)\.yaml$|^mappings/[A-Za-z0-9_-]+\.overrides\.yaml$|^(links|outcomes)\.csv$"
)
HEADER = "# Managed by the Renewal Risk admin UI — edits here are kept, but comments may be lost on the next UI save.\n"


class ConfigStore(ABC):
    @abstractmethod
    def read_text(self, name: str) -> str | None: ...

    @abstractmethod
    def write_text(self, name: str, text: str, saved_by: str | None = None) -> None: ...

    def read(self, name: str) -> dict:
        text = self.read_text(name)
        data = yaml.safe_load(text) if text else None
        return data if isinstance(data, dict) else {}

    def write(self, name: str, data: dict, saved_by: str | None = None) -> None:
        self.write_text(name, HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), saved_by)


class FileConfigStore(ConfigStore):
    def __init__(self, base_dir: str | Path, history=None):
        self.base = Path(base_dir)
        self.history = history  # a service.store.Store, for config_versions

    def _path(self, name: str) -> Path:
        if not ALLOWED_NAMES.match(name):
            raise ValueError(f"not a configurable file: {name}")
        return self.base / name

    def read_text(self, name: str) -> str | None:
        path = self._path(name)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def write_text(self, name: str, text: str, saved_by: str | None = None) -> None:
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = path.read_text(encoding="utf-8") if path.exists() else ""
        if self.history is not None and previous:
            self.history.save_config_version(name, previous, saved_by)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)  # atomic: a crash never leaves a half-written config
