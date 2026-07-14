from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from telegram_project_manager.bots.code_manager.workspace import PullRequestCheck
from telegram_project_manager.integrations.gh.runner import GhError, GhRunner


ACTION_RUN_RE = re.compile(r"https://github\.com/[^/]+/[^/]+/actions/runs/(\d+)")


@dataclass(frozen=True)
class PullRequestSnapshot:
    state: str
    is_draft: bool
    base_branch: str
    head_sha: str
    mergeable: str
    merge_state_status: str
    review_decision: str
    merged_at: str
    merge_sha: str
    checks: tuple[PullRequestCheck, ...]

    @property
    def merged(self) -> bool:
        return self.state == "MERGED" or bool(self.merged_at)


@dataclass(frozen=True)
class WorkflowRun:
    run_id: int
    status: str
    conclusion: str
    url: str
    head_sha: str
    workflow_name: str
    created_at: str = ""


class DeploymentGitHubService:
    def __init__(self, gh: GhRunner) -> None:
        self.gh = gh

    def get_pr(self, pr_url: str) -> PullRequestSnapshot:
        result = self.gh.run(
            [
                "pr",
                "view",
                pr_url,
                "--json",
                (
                    "state,isDraft,baseRefName,headRefOid,mergeable,mergeStateStatus,"
                    "reviewDecision,mergedAt,mergeCommit,statusCheckRollup"
                ),
            ]
        )
        try:
            value = result.json()
        except json.JSONDecodeError as exc:
            raise GhError(result) from exc
        if not isinstance(value, dict):
            raise GhError(result)
        merge_commit = value.get("mergeCommit")
        merge_sha = str(merge_commit.get("oid") or "") if isinstance(merge_commit, dict) else ""
        raw_checks = value.get("statusCheckRollup")
        if not isinstance(raw_checks, list):
            raise GhError(result)
        return PullRequestSnapshot(
            state=str(value.get("state") or "").upper(),
            is_draft=bool(value.get("isDraft")),
            base_branch=str(value.get("baseRefName") or ""),
            head_sha=str(value.get("headRefOid") or ""),
            mergeable=str(value.get("mergeable") or "").upper(),
            merge_state_status=str(value.get("mergeStateStatus") or "").upper(),
            review_decision=str(value.get("reviewDecision") or "").upper(),
            merged_at=str(value.get("mergedAt") or ""),
            merge_sha=merge_sha,
            checks=tuple(_parse_check(item) for item in raw_checks if isinstance(item, dict)),
        )

    def squash_merge(self, *, pr_url: str, head_sha: str) -> None:
        self.gh.run(
            [
                "pr",
                "merge",
                pr_url,
                "--squash",
                "--delete-branch",
                "--match-head-commit",
                head_sha,
            ]
        )

    def dispatch_workflow(
        self, *, repo: str, workflow: str, commit_sha: str
    ) -> WorkflowRun | None:
        result = self.gh.run(
            [
                "workflow",
                "run",
                workflow,
                "--repo",
                repo,
                "--ref",
                "main",
                "--raw-field",
                f"ref={commit_sha}",
            ]
        )
        match = ACTION_RUN_RE.search(result.stdout)
        if not match:
            return None
        return WorkflowRun(
            run_id=int(match.group(1)),
            status="requested",
            conclusion="",
            url=match.group(0),
            head_sha="",
            workflow_name=workflow,
        )

    def find_dispatched_workflow_run(
        self, *, repo: str, workflow: str, not_before: int
    ) -> WorkflowRun | None:
        result = self.gh.run(
            [
                "run",
                "list",
                "--repo",
                repo,
                "--workflow",
                workflow,
                "--event",
                "workflow_dispatch",
                "--limit",
                "5",
                "--json",
                "databaseId,status,conclusion,url,headSha,workflowName,createdAt",
            ]
        )
        try:
            value = result.json()
        except json.JSONDecodeError as exc:
            raise GhError(result) from exc
        if not isinstance(value, list):
            raise GhError(result)
        for item in value:
            if not isinstance(item, dict):
                continue
            run = _parse_run(item)
            if _created_epoch(run.created_at) >= not_before - 5:
                return run
        return None

    def get_workflow_run(self, *, repo: str, run_id: int) -> WorkflowRun:
        result = self.gh.run(
            [
                "run",
                "view",
                str(run_id),
                "--repo",
                repo,
                "--json",
                "databaseId,status,conclusion,url,headSha,workflowName",
            ]
        )
        try:
            value = result.json()
        except json.JSONDecodeError as exc:
            raise GhError(result) from exc
        if not isinstance(value, dict):
            raise GhError(result)
        return _parse_run(value)


def _parse_check(item: dict[str, Any]) -> PullRequestCheck:
    state = str(item.get("conclusion") or item.get("state") or item.get("status") or "").lower()
    status = str(item.get("status") or "").lower()
    if status and status != "completed":
        bucket = "pending"
    elif state == "success":
        bucket = "pass"
    elif state in {"neutral", "skipped"}:
        bucket = "skipping"
    elif state in {"queued", "in_progress", "pending", "expected", "requested", "waiting", ""}:
        bucket = "pending"
    elif state == "cancelled":
        bucket = "cancel"
    else:
        bucket = "fail"
    return PullRequestCheck(
        name=str(item.get("name") or item.get("context") or "Unnamed check"),
        state=state,
        bucket=bucket,
        link=str(item.get("detailsUrl") or item.get("targetUrl") or ""),
        workflow=str(item.get("workflowName") or ""),
        description=str(item.get("description") or ""),
    )


def _parse_run(item: dict[str, Any]) -> WorkflowRun:
    return WorkflowRun(
        run_id=int(item.get("databaseId") or 0),
        status=str(item.get("status") or "").lower(),
        conclusion=str(item.get("conclusion") or "").lower(),
        url=str(item.get("url") or ""),
        head_sha=str(item.get("headSha") or ""),
        workflow_name=str(item.get("workflowName") or ""),
        created_at=str(item.get("createdAt") or ""),
    )


def _created_epoch(value: str) -> int:
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0
