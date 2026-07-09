from __future__ import annotations

import json
import os
from pathlib import Path


class SecretStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._file_values: dict[str, str] | None = None

    def get(self, key: str, default: str | None = None) -> str | None:
        value = os.environ.get(key)
        if value:
            return value
        return self._load_file_values().get(key, default)

    def require(self, key: str) -> str:
        value = self.get(key)
        if not value:
            raise SystemExit(f"Missing secret: {key}")
        return value

    def require_int(self, key: str) -> int:
        value = self.require(key)
        try:
            return int(value)
        except ValueError as exc:
            raise SystemExit(f"Secret must be an integer: {key}") from exc

    def _load_file_values(self) -> dict[str, str]:
        if self._file_values is not None:
            return self._file_values
        if not self.path.exists():
            self._file_values = {}
            return self._file_values
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self._file_values = {str(k): str(v) for k, v in raw.items() if v is not None}
        return self._file_values

