from __future__ import annotations

import fnmatch
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram_project_manager.integrations.gh.runner import GhError, GhRunner
from telegram_project_manager.integrations.git.local_repository import LocalRepositoryError, LocalRepositoryService


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
MAX_CI_DIAGNOSTIC_CHARS = 50_000
ACTION_RUN_RE = re.compile(r"/actions/runs/(\d+)")
API_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
GITHUB_TOKEN_RE = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", re.IGNORECASE)
BEARER_TOKEN_RE = re.compile(r"(?i)(authorization:\s*bearer\s+)\S+")


WorkspaceError = LocalRepositoryError


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


@dataclass(frozen=True)
class PullRequestCheck:
    name: str
    state: str
    bucket: str
    link: str
    workflow: str
    description: str

    def to_json(self) -> dict[str, str]:
        return {
            "name": self.name,
            "state": self.state,
            "bucket": self.bucket,
            "link": self.link,
            "workflow": self.workflow,
            "description": self.description,
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
    def __init__(
        self,
        commands: CommandRunner | None = None,
        repositories: LocalRepositoryService | None = None,
    ) -> None:
        self.commands = commands or CommandRunner()
        self.repositories = repositories or LocalRepositoryService()

    def validate_source(self, *, source_path: str, repo: str) -> str:
        return str(self.repositories.validate(source_path, repo))

    def prepare(
        self,
        *,
        source_path: str,
        repo: str,
        base_branch: str,
        target_branch: str,
        path: Path,
    ) -> str:
        source = self.repositories.validate(source_path, repo)
        _, base_sha = self.repositories.fetch(source, base_branch)
        with self.repositories.lock_for(source):
            self._remove_existing(source, path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.commands.run(["git", "-C", str(source), "worktree", "prune"])
            try:
                self.commands.run(["git", "-C", str(source), "branch", "-D", target_branch])
            except WorkspaceError:
                pass
            self.commands.run(
                [
                    "git", "-C", str(source), "worktree", "add", "-b", target_branch,
                    str(path), f"refs/remotes/origin/{base_branch}",
                ],
                timeout=900,
            )
            self._configure_identity(path)
        return base_sha

    def checkout_existing(
        self,
        *,
        source_path: str,
        repo: str,
        branch: str,
        path: Path,
    ) -> str:
        source = self.repositories.validate(source_path, repo)
        _, remote_sha = self.repositories.fetch(source, branch)
        with self.repositories.lock_for(source):
            self._remove_existing(source, path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.commands.run(["git", "-C", str(source), "worktree", "prune"])
            try:
                self.commands.run(
                    ["git", "-C", str(source), "show-ref", "--verify", f"refs/heads/{branch}"]
                )
                add_args = ["git", "-C", str(source), "worktree", "add", str(path), branch]
            except WorkspaceError:
                add_args = [
                    "git", "-C", str(source), "worktree", "add", "-b", branch,
                    str(path), f"refs/remotes/origin/{branch}",
                ]
            self.commands.run(add_args, timeout=900)
            self._configure_identity(path)
        return remote_sha

    def refresh_base(self, path: Path, base_branch: str) -> str:
        self.commands.run(["git", "fetch", "origin", base_branch], cwd=path, timeout=600)
        self.commands.run(["git", "rebase", f"origin/{base_branch}"], cwd=path, timeout=600)
        return self.commands.run(["git", "rev-parse", f"origin/{base_branch}"], cwd=path).strip()

    def push_rebased_branch(self, path: Path) -> None:
        self.commands.run(["git", "push", "--force-with-lease"], cwd=path, timeout=900)

    def is_dirty(self, path: Path) -> bool:
        return bool(self.commands.run(["git", "status", "--porcelain"], cwd=path).strip())

    def sync_to_remote_head(self, *, path: Path, branch: str, expected_sha: str) -> None:
        local_sha = self.commands.run(["git", "rev-parse", "HEAD"], cwd=path).strip()
        if local_sha == expected_sha:
            return
        if self.is_dirty(path):
            raise WorkspaceError("workspace is dirty while the pull request head changed")
        self.commands.run(["git", "fetch", "origin", branch], cwd=path, timeout=600)
        remote_sha = self.commands.run(
            ["git", "rev-parse", f"origin/{branch}"], cwd=path
        ).strip()
        if remote_sha != expected_sha:
            raise WorkspaceError("pull request head changed again while preparing CI repair")
        self.commands.run(["git", "reset", "--hard", expected_sha], cwd=path)

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

    def cleanup(self, *, source_path: str, path: Path, target_branch: str) -> None:
        source = Path(source_path).expanduser().resolve()
        with self.repositories.lock_for(source):
            self._remove_existing(source, path)
            try:
                self.commands.run(["git", "-C", str(source), "worktree", "prune"])
                self.commands.run(["git", "-C", str(source), "branch", "-D", target_branch])
            except WorkspaceError:
                pass

    def _remove_existing(self, source: Path, path: Path) -> None:
        try:
            self.commands.run(
                ["git", "-C", str(source), "worktree", "remove", "--force", str(path)]
            )
        except WorkspaceError:
            if path.exists():
                shutil.rmtree(path)

    def _configure_identity(self, path: Path) -> None:
        self.commands.run(["git", "config", "user.name", "telegram-project-manager[bot]"], cwd=path)
        self.commands.run(
            ["git", "config", "user.email", "telegram-project-manager[bot]@users.noreply.github.com"],
            cwd=path,
        )


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

    def get_pr_head_sha(self, pr_url: str) -> str:
        result = self.gh.run(["pr", "view", pr_url, "--json", "headRefOid"])
        try:
            value = result.json()
        except json.JSONDecodeError as exc:
            raise GhError(result) from exc
        sha = str(value.get("headRefOid") or "") if isinstance(value, dict) else ""
        if not sha:
            raise GhError(result)
        return sha

    def get_pr_checks(self, pr_url: str) -> tuple[PullRequestCheck, ...]:
        result = self.gh.run(
            [
                "pr", "checks", pr_url, "--json",
                "name,state,bucket,link,workflow,description",
            ],
            check=False,
        )
        raw: Any = None
        if result.stdout.strip():
            try:
                raw = result.json()
            except json.JSONDecodeError as exc:
                raise GhError(result) from exc
        if not isinstance(raw, list):
            detail = f"{result.stderr}\n{result.stdout}".lower()
            if "no checks reported" in detail:
                return ()
            raise GhError(result)
        checks = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            checks.append(
                PullRequestCheck(
                    name=str(item.get("name") or "Unnamed check"),
                    state=str(item.get("state") or "").lower(),
                    bucket=str(item.get("bucket") or "").lower(),
                    link=str(item.get("link") or ""),
                    workflow=str(item.get("workflow") or ""),
                    description=str(item.get("description") or ""),
                )
            )
        return tuple(checks)

    def failed_check_diagnostics(
        self, *, repo: str, checks: tuple[PullRequestCheck, ...]
    ) -> str:
        parts: list[str] = []
        seen_runs: set[str] = set()
        for check in checks:
            parts.append(
                "\n".join(
                    [
                        f"Check: {check.name}",
                        f"Workflow: {check.workflow or '(external check)'}",
                        f"State: {check.state or check.bucket}",
                        f"Description: {check.description or '(none)'}",
                        f"Link: {check.link or '(none)'}",
                    ]
                )
            )
            match = ACTION_RUN_RE.search(check.link)
            if not match or match.group(1) in seen_runs:
                continue
            run_id = match.group(1)
            seen_runs.add(run_id)
            result = self.gh.run(
                ["run", "view", run_id, "--repo", repo, "--log-failed"],
                check=False,
            )
            log = result.stdout.strip() if result.returncode == 0 else ""
            if log:
                parts.append(f"Failed GitHub Actions log for run {run_id}:\n{log}")
        return _redact_ci_diagnostics("\n\n".join(parts))[:MAX_CI_DIAGNOSTIC_CHARS]

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


def _redact_ci_diagnostics(value: str) -> str:
    value = API_KEY_RE.sub("[REDACTED_API_KEY]", value)
    value = GITHUB_TOKEN_RE.sub("[REDACTED_GITHUB_TOKEN]", value)
    return BEARER_TOKEN_RE.sub(r"\1[REDACTED]", value)
