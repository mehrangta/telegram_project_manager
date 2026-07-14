from __future__ import annotations

import fnmatch
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram_project_manager.integrations.gh.runner import GhError, GhRunner


SENSITIVE_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_ed25519",
    ".github/workflows/*",
)
MAX_CHANGED_FILES = 100
MAX_CHANGED_BYTES = 5_000_000


class WorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class IssueContext:
    repo: str
    number: int
    title: str
    body: str
    url: str
    comments: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "url": self.url,
            "comments": list(self.comments),
        }


class CommandRunner:
    def run(self, args: list[str], *, cwd: Path | None = None, timeout: int = 300) -> str:
        completed = subprocess.run(
            args,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr.strip() or completed.stdout.strip() or "command failed")[:1000]
            raise WorkspaceError(f"{args[0]} failed: {detail}")
        return completed.stdout


class GitWorkspaceService:
    def __init__(self, commands: CommandRunner | None = None) -> None:
        self.commands = commands or CommandRunner()

    def prepare(self, *, repo: str, base_branch: str, target_branch: str, path: Path) -> str:
        if path.exists():
            shutil.rmtree(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.commands.run(
            ["gh", "repo", "clone", repo, str(path), "--", "--depth", "1", "--branch", base_branch],
            timeout=900,
        )
        self.commands.run(["git", "config", "user.name", "telegram-project-manager[bot]"], cwd=path)
        self.commands.run(
            ["git", "config", "user.email", "telegram-project-manager[bot]@users.noreply.github.com"],
            cwd=path,
        )
        self.commands.run(["git", "switch", "-c", target_branch], cwd=path)
        return self.commands.run(["git", "rev-parse", "HEAD"], cwd=path).strip()

    def checkout_existing(self, *, repo: str, branch: str, path: Path) -> str:
        if path.exists():
            shutil.rmtree(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.commands.run(
            ["gh", "repo", "clone", repo, str(path), "--", "--depth", "20", "--branch", branch],
            timeout=900,
        )
        self.commands.run(["git", "config", "user.name", "telegram-project-manager[bot]"], cwd=path)
        self.commands.run(
            ["git", "config", "user.email", "telegram-project-manager[bot]@users.noreply.github.com"],
            cwd=path,
        )
        return self.commands.run(["git", "rev-parse", "HEAD"], cwd=path).strip()

    def refresh_base(self, path: Path, base_branch: str) -> str:
        self.commands.run(["git", "fetch", "origin", base_branch], cwd=path, timeout=600)
        self.commands.run(["git", "rebase", f"origin/{base_branch}"], cwd=path, timeout=600)
        return self.commands.run(["git", "rev-parse", f"origin/{base_branch}"], cwd=path).strip()

    def push_rebased_branch(self, path: Path) -> None:
        self.commands.run(["git", "push", "--force-with-lease"], cwd=path, timeout=900)

    def is_dirty(self, path: Path) -> bool:
        return bool(self.commands.run(["git", "status", "--porcelain"], cwd=path).strip())

    def commit_plan(
        self,
        *,
        path: Path,
        plan_path: str,
        markdown: str,
        message: str,
        first_push: bool,
    ) -> str:
        destination = path / plan_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(markdown, encoding="utf-8")
        self.commands.run(["git", "add", "--", plan_path], cwd=path)
        self.commands.run(["git", "commit", "-m", message], cwd=path)
        push = ["git", "push"]
        if first_push:
            push.extend(["--set-upstream", "origin", "HEAD"])
        self.commands.run(push, cwd=path, timeout=900)
        return self.commands.run(["git", "rev-parse", "HEAD"], cwd=path).strip()

    def validate_code_changes(self, *, path: Path, plan_path: str) -> list[str]:
        self.commands.run(["git", "diff", "--check"], cwd=path)
        raw = self.commands.run(["git", "status", "--porcelain"], cwd=path)
        changed: list[str] = []
        for line in raw.splitlines():
            if len(line) < 4:
                continue
            value = line[3:].strip()
            if " -> " in value:
                value = value.rsplit(" -> ", 1)[-1]
            value = value.strip('"').replace("\\", "/")
            if value:
                changed.append(value)
        unique = sorted(set(changed))
        code_paths = [item for item in unique if item != plan_path]
        if not code_paths:
            raise WorkspaceError("Codex produced no implementation changes")
        if len(unique) > MAX_CHANGED_FILES:
            raise WorkspaceError(f"Codex changed too many files; maximum is {MAX_CHANGED_FILES}")
        for item in unique:
            if any(fnmatch.fnmatch(item, pattern) for pattern in SENSITIVE_PATTERNS):
                raise WorkspaceError(f"sensitive path blocked: {item}")
        total_bytes = sum(
            candidate.stat().st_size
            for item in unique
            if (candidate := path / item).is_file()
        )
        if total_bytes > MAX_CHANGED_BYTES:
            raise WorkspaceError("Codex changes exceed the 5 MB safety limit")
        if (path / plan_path).exists():
            raise WorkspaceError(f"Host failed to remove the temporary plan file: {plan_path}")
        return code_paths

    @staticmethod
    def remove_plan(*, path: Path, plan_path: str) -> None:
        workspace = path.resolve()
        target = (workspace / plan_path).resolve()
        try:
            relative = target.relative_to(workspace)
        except ValueError as exc:
            raise WorkspaceError("temporary plan path escapes the workspace") from exc
        if not relative.parts or relative.parts[:2] != (".codex", "plans"):
            raise WorkspaceError("temporary plan path is outside .codex/plans")
        if target.exists() or target.is_symlink():
            target.unlink()

    def commit_code(self, *, path: Path, message: str, first_push: bool) -> str:
        self.commands.run(["git", "add", "-A"], cwd=path)
        self.commands.run(["git", "commit", "-m", message], cwd=path)
        push = ["git", "push"]
        if first_push:
            push.extend(["--set-upstream", "origin", "HEAD"])
        self.commands.run(push, cwd=path, timeout=900)
        return self.commands.run(["git", "rev-parse", "HEAD"], cwd=path).strip()

    @staticmethod
    def cleanup(path: Path) -> None:
        if path.exists():
            shutil.rmtree(path)


class CodeGitHubService:
    def __init__(self, gh: GhRunner) -> None:
        self.gh = gh

    def get_issue(self, repo: str, number: int) -> IssueContext:
        issue = self.gh.api_json(f"repos/{repo}/issues/{number}")
        if issue.get("pull_request"):
            raise WorkspaceError("/code accepts GitHub issues, not pull requests")
        if issue.get("state") != "open":
            raise WorkspaceError("GitHub issue must be open")
        raw_comments = self.gh.api_value(
            f"repos/{repo}/issues/{number}/comments?per_page=20&sort=created&direction=desc"
        )
        comments: list[str] = []
        used = 0
        if isinstance(raw_comments, list):
            for item in reversed(raw_comments[-20:]):
                if not isinstance(item, dict):
                    continue
                body = str(item.get("body") or "").strip()
                if not body:
                    continue
                encoded = body.encode("utf-8")
                if used + len(encoded) > 32_000:
                    break
                used += len(encoded)
                comments.append(body)
        return IssueContext(
            repo=repo,
            number=number,
            title=str(issue.get("title") or "").strip(),
            body=str(issue.get("body") or "").strip(),
            url=str(issue.get("html_url") or f"https://github.com/{repo}/issues/{number}"),
            comments=tuple(comments),
        )

    def create_draft_pr(
        self,
        *,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict[str, Any]:
        return self.gh.api_json(
            f"repos/{repo}/pulls",
            method="POST",
            body={"title": title, "body": body, "head": head, "base": base, "draft": True},
        )

    def update_pr(self, *, repo: str, number: int, title: str, body: str) -> None:
        self.gh.api_json(
            f"repos/{repo}/pulls/{number}",
            method="PATCH",
            body={"title": title, "body": body},
        )

    def mark_ready(self, pr_url: str) -> None:
        self.gh.run(["pr", "ready", pr_url])

    def discard(self, *, repo: str, number: int | None, branch: str) -> None:
        if number is not None:
            try:
                self.gh.api_json(
                    f"repos/{repo}/pulls/{number}",
                    method="PATCH",
                    body={"state": "closed"},
                )
            except GhError:
                pass
        try:
            self.gh.api_value(f"repos/{repo}/git/refs/heads/{branch}", method="DELETE")
        except GhError:
            pass
