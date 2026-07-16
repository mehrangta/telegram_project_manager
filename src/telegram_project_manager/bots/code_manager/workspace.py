from __future__ import annotations

import fnmatch
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlsplit

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
WORKFLOW_PATTERN = ".github/workflows/*"
MAX_CHANGED_FILES = 100
MAX_CHANGED_BYTES = 5_000_000
MAX_CI_DIAGNOSTIC_CHARS = 50_000
MAX_ISSUE_IMAGES = 10
MAX_ISSUE_IMAGE_BYTES = 10_000_000
MAX_ISSUE_IMAGE_TOTAL_BYTES = 20_000_000
ISSUE_IMAGE_DIRECTORY = ".codex/issue-images"
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
ACTION_RUN_RE = re.compile(r"/actions/runs/(\d+)")
WORKFLOW_USES_LINE_RE = re.compile(
    r"(?m)^\s*(?:-\s*)?uses:\s*[\"']?([^\"'#\s]+)[\"']?\s*(?:#\s*([^\r\n]+))?$"
)
ACTION_RELEASE_HINT_RE = re.compile(r"\b(?:stable|v\d+(?:\.\d+){0,2})\b", re.IGNORECASE)
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

    def prepare_read_only(
        self,
        *,
        source_path: str,
        repo: str,
        base_branch: str,
        path: Path,
    ) -> str:
        source = self.repositories.validate(source_path, repo)
        _, base_sha = self.repositories.fetch(source, base_branch)
        with self.repositories.lock_for(source):
            self._remove_existing(source, path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.commands.run(["git", "-C", str(source), "worktree", "prune"])
            self.commands.run(
                [
                    "git", "-C", str(source), "worktree", "add", "--detach",
                    str(path), f"refs/remotes/origin/{base_branch}",
                ],
                timeout=900,
            )
        return base_sha

    def cleanup_read_only(self, *, source_path: str, path: Path) -> None:
        source = Path(source_path).expanduser().resolve()
        with self.repositories.lock_for(source):
            self._remove_existing(source, path)
            try:
                self.commands.run(["git", "-C", str(source), "worktree", "prune"])
            except WorkspaceError:
                pass

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
        self.commands.run(
            [
                "git", "push", "--force-with-lease", "--set-upstream", "origin", "HEAD",
            ],
            cwd=path,
            timeout=900,
        )

    def start_conflict_aware_rebase(
        self, path: Path, base_branch: str
    ) -> tuple[str, list[str]]:
        self.commands.run(["git", "fetch", "origin", base_branch], cwd=path, timeout=600)
        base_sha = self.commands.run(
            ["git", "rev-parse", f"origin/{base_branch}"], cwd=path
        ).strip()
        try:
            self.commands.run(["git", "rebase", f"origin/{base_branch}"], cwd=path, timeout=600)
        except WorkspaceError:
            conflicts = self.rebase_conflicts(path)
            if not conflicts:
                raise
            return base_sha, conflicts
        return base_sha, []

    def rebase_conflicts(self, path: Path) -> list[str]:
        raw = self.commands.run(
            ["git", "diff", "--name-only", "--diff-filter=U", "--"], cwd=path
        )
        conflicts = sorted(
            {item.strip().replace("\\", "/") for item in raw.splitlines() if item.strip()}
        )
        if len(conflicts) > MAX_CHANGED_FILES:
            raise WorkspaceError(f"rebase has too many conflicted files; maximum is {MAX_CHANGED_FILES}")
        for item in conflicts:
            if any(fnmatch.fnmatch(item, pattern) for pattern in SENSITIVE_PATTERNS):
                raise WorkspaceError(f"sensitive conflict path blocked: {item}")
        return conflicts

    def continue_conflict_aware_rebase(
        self, path: Path, conflict_files: list[str]
    ) -> list[str]:
        if not conflict_files:
            raise WorkspaceError("rebase continuation has no conflicted files")
        current = self.rebase_conflicts(path)
        if current != sorted(set(conflict_files)):
            raise WorkspaceError("the set of conflicted files changed unexpectedly")
        status = self.commands.run(["git", "status", "--porcelain"], cwd=path)
        allowed = set(current)
        for line in status.splitlines():
            if len(line) < 4:
                continue
            item = line[3:].strip().strip('"').replace("\\", "/")
            if " -> " in item:
                item = item.rsplit(" -> ", 1)[-1]
            worktree_changed = line.startswith("??") or line[1] != " "
            if worktree_changed and item not in allowed:
                raise WorkspaceError(f"Codex changed non-conflict path during rebase: {item}")
        self.commands.run(["git", "diff", "--check"], cwd=path)
        self.commands.run(["git", "add", "--", *current], cwd=path)
        try:
            self.commands.run(
                ["git", "-c", "core.editor=true", "rebase", "--continue"],
                cwd=path,
                timeout=600,
            )
        except WorkspaceError:
            conflicts = self.rebase_conflicts(path)
            if not conflicts:
                raise
            return conflicts
        return []

    def head_sha(self, path: Path) -> str:
        return self.commands.run(["git", "rev-parse", "HEAD"], cwd=path).strip()

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
        self.commands.run(["git", "add", "-f", "--", plan_path], cwd=path)
        self.commands.run(["git", "commit", "-m", message], cwd=path)
        push = ["git", "push"]
        if first_push:
            push.extend(["--set-upstream", "origin", "HEAD"])
        self.commands.run(push, cwd=path, timeout=900)
        return self.commands.run(["git", "rev-parse", "HEAD"], cwd=path).strip()

    def validate_code_changes(
        self,
        *,
        path: Path,
        plan_path: str,
        allowed_workflow_paths: tuple[str, ...] = (),
    ) -> list[str]:
        self.commands.run(["git", "diff", "--check"], cwd=path)
        raw = self.commands.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=path,
        )
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
        allowed_workflows = set(allowed_workflow_paths)
        for item in unique:
            is_workflow = fnmatch.fnmatch(item, WORKFLOW_PATTERN)
            if is_workflow and item in allowed_workflows:
                continue
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

    def stage_issue_images(
        self,
        *,
        source_path: str,
        repo: str,
        issue: dict[str, Any],
        path: Path,
    ) -> list[str]:
        asset_paths = _managed_issue_asset_paths(issue, repo)
        self.remove_issue_images(path=path)
        if not asset_paths:
            return []
        source = self.repositories.validate(source_path, repo)
        source, commit = self.repositories.fetch(source, "issue-assets")
        entries = {item.path: item for item in self.repositories.tree(source, commit)}
        destination = path / ISSUE_IMAGE_DIRECTORY
        staged: list[str] = []
        total = 0
        try:
            destination.mkdir(parents=True, exist_ok=True)
            for position, asset_path in enumerate(asset_paths, start=1):
                entry = entries.get(asset_path)
                if entry is None or entry.type != "blob":
                    raise WorkspaceError(f"Issue image is missing from issue-assets: {asset_path}")
                if entry.size > MAX_ISSUE_IMAGE_BYTES:
                    raise WorkspaceError("Each issue image must be 10 MB or smaller")
                total += entry.size
                if total > MAX_ISSUE_IMAGE_TOTAL_BYTES:
                    raise WorkspaceError("Issue images must be 20 MB or smaller in total")
                content = self.repositories.read_blob(source, entry.sha)
                extension = PurePosixPath(asset_path).suffix.lower()
                _validate_issue_image(content, extension)
                image_path = destination / f"{position}{extension}"
                image_path.write_bytes(content)
                staged.append(str(image_path.resolve()))
            return staged
        except Exception:
            self.remove_issue_images(path=path)
            raise

    @staticmethod
    def remove_issue_images(*, path: Path) -> None:
        workspace = path.resolve()
        target = workspace / ".codex" / "issue-images"
        if target.is_symlink():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)

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


def _managed_issue_asset_paths(issue: dict[str, Any], repo: str) -> list[str]:
    prefix = f"/{repo.lower()}/blob/issue-assets/"
    paths: list[str] = []
    seen: set[str] = set()
    values = [
        str(issue.get("body") or ""),
        *(str(item) for item in issue.get("comments") or []),
    ]
    for text in values:
        for match in MARKDOWN_IMAGE_RE.finditer(text):
            raw_url = match.group(1)
            parsed = urlsplit(raw_url)
            if parsed.scheme or parsed.netloc:
                if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
                    continue
                url_path = unquote(parsed.path)
                if not url_path.lower().startswith(prefix):
                    continue
                asset_path = url_path[len(prefix):]
            else:
                marker = "../blob/issue-assets/"
                relative = unquote(parsed.path)
                if not relative.startswith(marker):
                    continue
                asset_path = relative[len(marker):]
            candidate = PurePosixPath(asset_path)
            if (
                candidate.is_absolute()
                or not candidate.parts
                or ".." in candidate.parts
                or candidate.parts[0] != ".issue-assets"
                or candidate.suffix.lower() not in {".jpg", ".jpeg", ".png", ".gif"}
            ):
                continue
            normalized = candidate.as_posix()
            if normalized not in seen:
                seen.add(normalized)
                paths.append(normalized)
                if len(paths) == MAX_ISSUE_IMAGES:
                    return paths
    return paths


def _validate_issue_image(content: bytes, extension: str) -> None:
    header = content[:16].hex()
    valid = {
        ".jpg": header.startswith("ffd8ff"),
        ".jpeg": header.startswith("ffd8ff"),
        ".png": header.startswith("89504e470d0a1a0a"),
        ".gif": header.startswith(("474946383761", "474946383961")),
    }
    if not valid.get(extension, False):
        raise WorkspaceError(f"Issue image content does not match {extension}")


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

    def get_authenticated_login(self) -> str:
        user = self.gh.api_json("user")
        login = str(user.get("login") or "").strip()
        if not login:
            raise WorkspaceError("Authenticated GitHub login is unavailable")
        return login

    def validate_workflow_action_refs(self, *, path: Path, files: list[str]) -> None:
        references: dict[str, set[str]] = {}
        workspace = path.resolve()
        for item in files:
            if not fnmatch.fnmatch(item, WORKFLOW_PATTERN):
                continue
            candidate = (workspace / item).resolve()
            try:
                candidate.relative_to(workspace)
            except ValueError as exc:
                raise WorkspaceError(f"Workflow path escapes the workspace: {item}") from exc
            if not candidate.is_file():
                continue
            content = candidate.read_text(encoding="utf-8")
            for reference, comment in WORKFLOW_USES_LINE_RE.findall(content):
                references.setdefault(reference, set()).add(comment.strip())
        invalid: list[str] = []
        for reference in sorted(references):
            if reference.startswith(("./", "docker://")):
                continue
            source, separator, revision = reference.rpartition("@")
            parts = source.split("/")
            if not separator or len(parts) < 2 or not revision:
                raise WorkspaceError(f"Invalid GitHub Action reference: {reference}")
            action_repo = "/".join(parts[:2])
            try:
                self.gh.api_json(
                    f"repos/{action_repo}/commits/{quote(revision, safe='')}"
                )
            except GhError:
                detail = f"- {reference}"
                hints = {
                    match.group(0)
                    for comment in references[reference]
                    if (match := ACTION_RELEASE_HINT_RE.search(comment))
                }
                for hint in sorted(hints):
                    try:
                        resolved = self.gh.api_json(
                            f"repos/{action_repo}/commits/{quote(hint, safe='')}"
                        )
                    except GhError:
                        continue
                    sha = str(resolved.get("sha") or "") if isinstance(resolved, dict) else ""
                    if re.fullmatch(r"[0-9a-fA-F]{40}", sha):
                        detail += f"; verified {hint} replacement: {source}@{sha.lower()}"
                        break
                invalid.append(detail)
        if invalid:
            raise WorkspaceError(
                "GitHub Action references do not exist:\n" + "\n".join(invalid)
            )

    def list_pr_comments(
        self, *, repo: str, number: int, after_id: int = 0
    ) -> list[dict[str, Any]]:
        raw = self.gh.api_value(
            f"repos/{repo}/issues/{number}/comments?per_page=100&sort=created&direction=asc"
        )
        if not isinstance(raw, list):
            return []
        comments = []
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                continue
            if int(item["id"]) > after_id:
                comments.append(item)
        return comments

    def publish_plan_questions(
        self,
        *,
        repo: str,
        number: int,
        body: str,
        comment_id: int | None,
    ) -> int:
        if comment_id:
            result = self.gh.api_json(
                f"repos/{repo}/issues/comments/{comment_id}",
                method="PATCH",
                body={"body": body},
            )
        else:
            result = self.gh.api_json(
                f"repos/{repo}/issues/{number}/comments",
                method="POST",
                body={"body": body},
            )
        value = result.get("id")
        if not isinstance(value, int):
            raise WorkspaceError("GitHub plan-question comment has no ID")
        return value

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
        result = self.gh.run(["pr", "view", pr_url, "--json", "statusCheckRollup"])
        try:
            value = result.json()
        except json.JSONDecodeError as exc:
            raise GhError(result) from exc
        raw = value.get("statusCheckRollup") if isinstance(value, dict) else None
        if not isinstance(raw, list):
            raise GhError(result)
        checks = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            state = str(item.get("conclusion") or item.get("state") or item.get("status") or "").lower()
            status = str(item.get("status") or "").lower()
            checks.append(
                PullRequestCheck(
                    name=str(item.get("name") or item.get("context") or "Unnamed check"),
                    state=state,
                    bucket=_check_bucket(state, status),
                    link=str(item.get("detailsUrl") or item.get("targetUrl") or ""),
                    workflow=str(item.get("workflowName") or ""),
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
            log = (
                result.stdout.strip()
                if result.returncode == 0
                else self._failed_run_log_via_api(repo, run_id)
            )
            if log:
                parts.append(f"Failed GitHub Actions log for run {run_id}:\n{log}")
        return _redact_ci_diagnostics("\n\n".join(parts))[:MAX_CI_DIAGNOSTIC_CHARS]

    def _failed_run_log_via_api(self, repo: str, run_id: str) -> str:
        try:
            payload = self.gh.api_value(
                f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100"
            )
        except GhError:
            return ""
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return ""
        logs: list[str] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            conclusion = str(job.get("conclusion") or "").lower()
            job_id = job.get("id")
            if conclusion not in {"failure", "timed_out", "startup_failure"}:
                continue
            if not isinstance(job_id, int):
                continue
            result = self.gh.run(
                ["api", f"repos/{repo}/actions/jobs/{job_id}/logs"],
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                logs.append(result.stdout.strip())
        return "\n\n".join(logs)

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


def _check_bucket(state: str, status: str) -> str:
    if status and status != "completed":
        return "pending"
    if state == "success":
        return "pass"
    if state in {"neutral", "skipped"}:
        return "skipping"
    if state in {"queued", "in_progress", "pending", "expected", "requested", "waiting", ""}:
        return "pending"
    if state == "cancelled":
        return "cancel"
    return "fail"
