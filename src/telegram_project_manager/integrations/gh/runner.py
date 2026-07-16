from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GhResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int

    def json(self) -> Any:
        return json.loads(self.stdout or "{}")


class GhError(RuntimeError):
    def __init__(self, result: GhResult) -> None:
        self.result = result
        message = result.stderr.strip() or result.stdout.strip() or "gh command failed"
        super().__init__(message)


class GhRunner:
    def __init__(self, cwd: Path | None = None, timeout_seconds: int = 90) -> None:
        self.cwd = cwd or Path.cwd()
        self.timeout_seconds = timeout_seconds

    def run(
        self,
        args: list[str],
        input_json: dict[str, Any] | None = None,
        check: bool = True,
        timeout_seconds: int | None = None,
    ) -> GhResult:
        start = time.monotonic()
        input_data = None
        full_args = ["gh", *args]
        if input_json is not None:
            input_data = json.dumps(input_json)
        completed = subprocess.run(
            full_args,
            cwd=self.cwd,
            input=input_data,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.timeout_seconds if timeout_seconds is None else timeout_seconds,
            check=False,
        )
        result = GhResult(
            args=full_args,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        if check and result.returncode != 0:
            raise GhError(result)
        return result

    def api_json(self, endpoint: str, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
        value = self.api_value(endpoint, method=method, body=body)
        if not isinstance(value, dict):
            result = GhResult(["gh", "api", endpoint], 1, json.dumps(value), "Expected JSON object", 0)
            raise GhError(result)
        return value

    def api_value(self, endpoint: str, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
        args = ["api", endpoint]
        if method != "GET":
            args.extend(["--method", method])
        if body is not None:
            args.extend(["--input", "-"])
        result = self.run(args, input_json=body)
        return result.json()

    def auth_status(self) -> GhResult:
        return self.run(["auth", "status"], check=False)
