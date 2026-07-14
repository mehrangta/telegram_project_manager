from __future__ import annotations

import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class LocalRepositoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitTreeEntry:
    path: str
    sha: str
    size: int
    type: str


class GitCommandRunner:
    def run(self, args: list[str], *, cwd: Path | None = None, timeout: int = 300) -> str:
        completed = subprocess.run(
            args,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr.strip() or completed.stdout.strip() or "command failed")[:1000]
            raise LocalRepositoryError(f"{args[0]} failed: {detail}")
        return completed.stdout

    def run_bytes(self, args: list[str], *, cwd: Path | None = None, timeout: int = 300) -> bytes:
        completed = subprocess.run(
            args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr.decode("utf-8", "replace").strip() or "command failed")[:1000]
            raise LocalRepositoryError(f"{args[0]} failed: {detail}")
        return completed.stdout


class LocalRepositoryService:
    """Validates and refreshes service-owned Git object stores."""

    def __init__(self, commands: GitCommandRunner | None = None) -> None:
        self.commands = commands or GitCommandRunner()
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    def lock_for(self, source_path: str | Path) -> threading.RLock:
        key = str(Path(source_path).expanduser().resolve())
        with self._locks_guard:
            return self._locks.setdefault(key, threading.RLock())

    def validate(self, source_path: str | Path, expected_repo: str) -> Path:
        raw = str(source_path).strip()
        if not raw:
            raise LocalRepositoryError(
                "No local repository cache is configured. Admin: /repo local set <absolute-path>"
            )
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise LocalRepositoryError("Local repository cache path must be absolute.")
        try:
            path = candidate.resolve(strict=True)
        except OSError as exc:
            raise LocalRepositoryError(f"Local repository cache is unavailable: {candidate}") from exc
        if not path.is_dir():
            raise LocalRepositoryError(f"Local repository cache is not a directory: {path}")
        try:
            common_raw = self.commands.run(
                ["git", "-C", str(path), "rev-parse", "--git-common-dir"]
            ).strip()
            origin = self.commands.run(
                ["git", "-C", str(path), "config", "--get", "remote.origin.url"]
            ).strip()
        except LocalRepositoryError as exc:
            raise LocalRepositoryError(f"Invalid local Git repository at {path}: {exc}") from exc
        common = Path(common_raw)
        if not common.is_absolute():
            common = (path / common).resolve()
        if not os.access(common, os.R_OK | os.W_OK | os.X_OK):
            raise LocalRepositoryError(
                f"Git repository metadata is not readable and writable by the bot: {common}"
            )
        actual_repo = github_repo_from_remote(origin)
        if actual_repo.lower() != expected_repo.lower():
            raise LocalRepositoryError(
                f"Local repository origin is {actual_repo or origin}, expected {expected_repo}."
            )
        return path

    def fetch(self, source_path: str | Path, branch: str) -> tuple[Path, str]:
        path = Path(source_path).expanduser().resolve()
        validate_branch(branch)
        with self.lock_for(path):
            self.commands.run(
                [
                    "git",
                    "-C",
                    str(path),
                    "fetch",
                    "--prune",
                    "origin",
                    f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                ],
                timeout=900,
            )
            commit = self.commands.run(
                ["git", "-C", str(path), "rev-parse", f"refs/remotes/origin/{branch}^{{commit}}"]
            ).strip()
        return path, commit

    def tree(self, source_path: str | Path, commit: str) -> list[GitTreeEntry]:
        raw = self.commands.run_bytes(
            ["git", "-C", str(source_path), "ls-tree", "-r", "-l", "-z", commit]
        )
        entries: list[GitTreeEntry] = []
        for record in raw.split(b"\0"):
            if not record:
                continue
            metadata, separator, path_raw = record.partition(b"\t")
            fields = metadata.decode("ascii", "replace").split()
            if not separator or len(fields) != 4:
                raise LocalRepositoryError("Local repository returned an invalid Git tree entry.")
            mode, item_type, sha, size_raw = fields
            del mode
            if size_raw == "-":
                continue
            entries.append(
                GitTreeEntry(
                    path=path_raw.decode("utf-8", "strict"),
                    sha=sha,
                    size=int(size_raw),
                    type=item_type,
                )
            )
        return entries

    def read_blob(self, source_path: str | Path, sha: str) -> bytes:
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
            raise LocalRepositoryError("Local repository returned an invalid object ID.")
        return self.commands.run_bytes(["git", "-C", str(source_path), "cat-file", "blob", sha])


def validate_branch(branch: str) -> None:
    if not branch or branch.startswith("-") or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
        raise LocalRepositoryError(f"Invalid Git branch name: {branch}")
    if any(value in branch for value in ("..", "@{", "//")) or branch.endswith(("/", ".", ".lock")):
        raise LocalRepositoryError(f"Invalid Git branch name: {branch}")


def github_repo_from_remote(remote: str) -> str:
    value = remote.strip()
    match = re.fullmatch(r"git@github\.com:([^/]+/[^/]+?)(?:\.git)?", value, re.IGNORECASE)
    if match:
        return match.group(1).removesuffix(".git")
    parsed = urlparse(value)
    if parsed.hostname and parsed.hostname.lower() == "github.com":
        return parsed.path.strip("/").removesuffix(".git")
    return ""
