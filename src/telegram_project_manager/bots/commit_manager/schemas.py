from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any


REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,180}$")
SENSITIVE_PATTERNS = [".env", ".env.*", "*.pem", "*.key", "id_rsa", "id_ed25519", ".github/workflows/*"]


class PlanValidationError(ValueError):
    pass


@dataclass(frozen=True)
class FileChange:
    path: str
    operation: str
    content: str


@dataclass(frozen=True)
class CommitPlan:
    intent: str
    repo: str
    base_branch: str
    target_branch: str
    commit_message: str
    actual_behavior: str
    expected_behavior: str
    changes: list[FileChange]
    github_comment: str
    requires_confirmation: bool
    questions: list[str]

    @classmethod
    def from_llm(cls, raw: dict[str, Any], *, fallback_repo: str, fallback_branch: str, target_branch: str) -> "CommitPlan":
        repo = str(raw.get("repo") or fallback_repo)
        base_branch = str(raw.get("base_branch") or raw.get("branch") or fallback_branch)
        target = str(raw.get("target_branch") or target_branch)
        changes_raw = raw.get("changes") or raw.get("files") or []
        if not isinstance(changes_raw, list):
            raise PlanValidationError("changes must be a list")
        changes = [
            FileChange(
                path=str(item.get("path", "")),
                operation=str(item.get("operation", "create_or_update")),
                content=str(item.get("content", "")),
            )
            for item in changes_raw
            if isinstance(item, dict)
        ]
        questions = raw.get("questions") or []
        if not isinstance(questions, list):
            questions = []
        plan = cls(
            intent=str(raw.get("intent") or raw.get("action") or "create_commit"),
            repo=repo,
            base_branch=base_branch,
            target_branch=target,
            commit_message=str(raw.get("commit_message") or raw.get("summary") or "").strip(),
            actual_behavior=str(
                raw.get("actual_behavior") or "Not provided for legacy plan."
            ).strip(),
            expected_behavior=str(
                raw.get("expected_behavior") or raw.get("commit_message") or raw.get("summary") or "Not provided."
            ).strip(),
            changes=changes,
            github_comment=str(raw.get("github_comment") or "").strip(),
            requires_confirmation=bool(raw.get("requires_confirmation", raw.get("needs_confirmation", True))),
            questions=[str(q) for q in questions],
        )
        plan.validate()
        return plan

    def to_json(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "repo": self.repo,
            "base_branch": self.base_branch,
            "target_branch": self.target_branch,
            "commit_message": self.commit_message,
            "actual_behavior": self.actual_behavior,
            "expected_behavior": self.expected_behavior,
            "changes": [change.__dict__ for change in self.changes],
            "github_comment": self.github_comment,
            "requires_confirmation": self.requires_confirmation,
            "questions": self.questions,
        }

    def validate(self, *, max_files: int = 10, max_bytes: int = 100000) -> None:
        if self.intent != "create_commit":
            raise PlanValidationError("intent must be create_commit")
        validate_repo(self.repo)
        validate_branch(self.base_branch)
        validate_branch(self.target_branch)
        if not self.commit_message:
            raise PlanValidationError("commit message is required")
        if not self.actual_behavior:
            raise PlanValidationError("actual behavior is required")
        if not self.expected_behavior:
            raise PlanValidationError("expected behavior is required")
        if not self.changes:
            raise PlanValidationError("at least one file change is required")
        if len(self.changes) > max_files:
            raise PlanValidationError(f"too many files, max {max_files}")
        total_bytes = 0
        for change in self.changes:
            validate_path(change.path)
            if change.operation not in {"create", "update", "create_or_update"}:
                raise PlanValidationError(f"unsupported operation for {change.path}")
            if not change.content:
                raise PlanValidationError(f"content is required for {change.path}")
            total_bytes += len(change.content.encode("utf-8"))
        if total_bytes > max_bytes:
            raise PlanValidationError(f"commit content too large, max {max_bytes} bytes")


def validate_repo(repo: str) -> None:
    if not REPO_RE.match(repo):
        raise PlanValidationError("repo must look like owner/repository")


def validate_branch(branch: str) -> None:
    if not BRANCH_RE.match(branch) or ".." in branch or branch.endswith("/") or branch.endswith("."):
        raise PlanValidationError("invalid branch name")


def validate_path(path: str) -> None:
    normalized = path.replace("\\", "/").strip()
    if not normalized or normalized.startswith("/") or "../" in normalized or normalized == "..":
        raise PlanValidationError(f"invalid path: {path}")
    for pattern in SENSITIVE_PATTERNS:
        if fnmatch.fnmatch(normalized, pattern):
            raise PlanValidationError(f"sensitive path blocked: {path}")
