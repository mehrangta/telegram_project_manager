from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from telegram_project_manager.bots.code_manager.progress import CodeProgressReporter
from telegram_project_manager.bots.pull_request_manager.github import (
    DeploymentGitHubService,
    PullRequestSnapshot,
)
from telegram_project_manager.integrations.gh.runner import GhError
from telegram_project_manager.platform.storage.db import Database


MERGE_TIMEOUT_SECONDS = 30 * 60
WORKFLOW_DISCOVERY_SECONDS = 2 * 60
DEPLOY_TIMEOUT_SECONDS = 30 * 60
POLL_SECONDS = 10
ACTIVE_DEPLOYMENT_STATUSES = {
    "queued", "merging", "resolving_conflicts", "waiting_workflow",
    "dispatching", "deploying"
}
ACTIVE_CONFLICT_RECOVERY_STATUSES = {
    "queued_rebase", "rebasing", "queued_checks", "waiting_checks", "repairing_checks"
}
MAX_CONFLICT_RESOLUTION_ATTEMPTS = 2
MERGE_MODE = "merge"
DEPLOY_MODE = "deploy"


class DeploymentError(ValueError):
    pass


class MergeDeploymentService:
    def __init__(
        self,
        *,
        db: Database,
        github: DeploymentGitHubService,
        reporter: CodeProgressReporter,
        poll_seconds: float = POLL_SECONDS,
        merge_timeout_seconds: float = MERGE_TIMEOUT_SECONDS,
        discovery_seconds: float = WORKFLOW_DISCOVERY_SECONDS,
        deploy_timeout_seconds: float = DEPLOY_TIMEOUT_SECONDS,
        conflict_rebaser: Callable[[str], Awaitable[None]] | None = None,
        max_conflict_resolution_attempts: int = MAX_CONFLICT_RESOLUTION_ATTEMPTS,
    ) -> None:
        self.db = db
        self.github = github
        self.reporter = reporter
        self.poll_seconds = poll_seconds
        self.merge_timeout_seconds = merge_timeout_seconds
        self.discovery_seconds = discovery_seconds
        self.deploy_timeout_seconds = deploy_timeout_seconds
        self.conflict_rebaser = conflict_rebaser
        self.max_conflict_resolution_attempts = max_conflict_resolution_attempts
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def recover(self) -> None:
        for job in self.db.list_active_deployments():
            self._schedule(str(job["id"]))

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def start(self, job_id: str) -> str:
        """Backward-compatible deployment entry point."""
        return await self.start_deploy(job_id)

    async def start_merge(self, job_id: str) -> str:
        return await self._start(job_id, MERGE_MODE)

    async def start_deploy(self, job_id: str) -> str:
        return await self._start(job_id, DEPLOY_MODE)

    async def _start(self, job_id: str, mode: str) -> str:
        job = self._require_job(job_id)
        if job["status"] != "ready":
            raise DeploymentError("Code job is not ready. All pull request checks must pass first.")
        workflow = ""
        if mode == DEPLOY_MODE:
            if str(job.get("base_branch") or "") != "main":
                raise DeploymentError("Deployment only supports pull requests targeting main.")
            if not self.db.is_repo_deploy_enabled(str(job["repo"])):
                raise DeploymentError(
                    f"Deployment is disabled for {job['repo']}. Admin: "
                    f"/repo deploy enable {job['repo']}"
                )
            workflow = self.db.get_repo_deploy_workflow(str(job["repo"]))
            if not workflow:
                raise DeploymentError(
                    "No deployment workflow configured. Admin: "
                    f"/repo deploy set {job['repo']} <workflow-name-or-file>"
                )
        deployment_status = str(job.get("deployment_status") or "")
        operation_mode = _operation_mode(job)
        if mode == DEPLOY_MODE and deployment_status == "succeeded":
            return self._status_message(job, "Deployment already succeeded.")
        if mode == MERGE_MODE and job.get("deployment_merge_sha"):
            return self._status_message(job, "Pull request already merged.")
        if deployment_status in ACTIVE_DEPLOYMENT_STATUSES:
            if operation_mode != mode:
                active = "merge-only" if operation_mode == MERGE_MODE else "merge and deployment"
                raise DeploymentError(
                    f"A {active} operation is already in progress. Retry after it completes."
                )
            self._schedule(job_id)
            heading = (
                "Merge already in progress."
                if mode == MERGE_MODE
                else "Merge and deployment already in progress."
            )
            return self._status_message(job, heading)
        outcome = self.db.start_code_job_operation(job_id, mode)
        if outcome == "merged":
            return self._status_message(self._require_job(job_id), "Pull request already merged.")
        if outcome != "started":
            current = self._require_job(job_id)
            if outcome == "not_ready":
                raise DeploymentError("Code job is not ready. All pull request checks must pass first.")
            active_mode = _operation_mode(current)
            active = "merge" if active_mode == MERGE_MODE else "merge and deployment"
            return self._status_message(current, f"{active.title()} already in progress.")
        audit = {"repo": job["repo"]}
        if workflow:
            audit["workflow"] = workflow
        self.db.audit(f"{mode}.queue", "ok", audit, job_id)
        self._schedule(job_id)
        try:
            await self.reporter.refresh(job_id, force=True)
        except Exception:
            logging.exception("Failed to refresh queued %s operation %s", mode, job_id)
        heading = "Merge queued." if mode == MERGE_MODE else "Merge and deployment queued."
        return f"{heading}\nCode Job ID: {job_id}\nRepo: {job['repo']}"

    def _schedule(self, job_id: str) -> None:
        existing = self._tasks.get(job_id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self._run(job_id), name=f"deploy-{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job_id, None))

    async def _run(self, job_id: str) -> None:
        try:
            while True:
                job = self._require_job(job_id)
                operation_status = str(job.get("deployment_status") or "")
                if operation_status in {"queued", "merging"}:
                    await self._merge(job_id)
                    continue
                if operation_status == "resolving_conflicts":
                    await self._wait_for_conflict_recovery(job_id)
                    continue
                break
            job = self._require_job(job_id)
            if _operation_mode(job) == DEPLOY_MODE and str(job.get("deployment_status") or "") in {
                "waiting_workflow", "dispatching", "deploying"
            }:
                await self._monitor_deployment(job_id)
        except asyncio.CancelledError:
            raise
        except (DeploymentError, GhError, ValueError) as exc:
            await self._fail(job_id, str(exc))
        except Exception as exc:
            logging.exception("Unexpected merge or deployment failure: %s", job_id)
            await self._fail(job_id, f"Unexpected failure: {exc}")

    async def _merge(self, job_id: str) -> None:
        job = self._require_job(job_id)
        self.db.update_code_job(
            job_id,
            {
                "deployment_status": "merging",
                "deployment_error": None,
                "latest_activity": "Validating and squash-merging pull request",
            },
        )
        await self.reporter.refresh(job_id, force=True)
        pr_url = str(job.get("pull_request_url") or "")
        if not pr_url:
            raise DeploymentError("Ready code job has no pull request URL.")
        snapshot = await asyncio.to_thread(self.github.get_pr, pr_url)
        require_main = _operation_mode(job) == DEPLOY_MODE
        self._validate_checked_identity(job, snapshot, require_main=require_main)
        if not snapshot.merged:
            if _has_explicit_conflict(snapshot):
                await self._queue_conflict_recovery(job_id, job)
                return
            self._validate_preflight(snapshot)
            pr_number = int(job["pull_request_number"])
            suffix = f" (#{pr_number})"
            subject = (
                f"Fix #{job['issue_number']}: {job['issue_title']}"
            )[: 256 - len(suffix)] + suffix
            await asyncio.to_thread(
                self.github.squash_merge,
                pr_url=pr_url,
                head_sha=snapshot.head_sha,
                commit_subject=subject,
                commit_body=f"Closes #{job['issue_number']}",
            )
            deadline = time.time() + self.merge_timeout_seconds
            while True:
                snapshot = await asyncio.to_thread(self.github.get_pr, pr_url)
                if snapshot.merged:
                    break
                if snapshot.state != "OPEN":
                    raise DeploymentError("Pull request closed without merging.")
                if time.time() >= deadline:
                    raise DeploymentError("Timed out waiting for the pull request or merge queue.")
                self.db.update_code_job(
                    job_id, {"latest_activity": "Waiting for GitHub merge queue"}
                )
                await self.reporter.refresh(job_id)
                await asyncio.sleep(self.poll_seconds)
        if not snapshot.merge_sha:
            raise DeploymentError("GitHub did not return the merge commit SHA.")
        mode = _operation_mode(job)
        now = int(time.time())
        next_status = "merged" if mode == MERGE_MODE else "waiting_workflow"
        next_activity = (
            f"Merged into {job['base_branch']}"
            if mode == MERGE_MODE
            else "Merged to main; waiting for deployment workflow"
        )
        self.db.update_code_job(
            job_id,
            {
                "deployment_status": next_status,
                "deployment_merge_sha": snapshot.merge_sha,
                "deployment_started_at": now,
                "deployment_error": None,
                "latest_activity": next_activity,
            },
        )
        self.db.audit(
            f"{mode}.merge",
            "ok",
            {"repo": job["repo"], "merge_sha": snapshot.merge_sha},
            job_id,
        )
        await self.reporter.refresh(job_id, force=True)
        if mode == MERGE_MODE:
            await self._notify_merge(job_id)

    async def _queue_conflict_recovery(
        self, job_id: str, job: dict[str, Any]
    ) -> None:
        attempts = int(job.get("deployment_conflict_attempts") or 0)
        if attempts >= self.max_conflict_resolution_attempts:
            raise DeploymentError(
                "Automatic conflict resolution stopped after "
                f"{self.max_conflict_resolution_attempts} attempts because the pull request "
                "is still conflicting."
            )
        if self.conflict_rebaser is None:
            raise DeploymentError("Automatic conflict resolution is not configured.")
        attempt = attempts + 1
        self.db.update_code_job(
            job_id,
            {
                "deployment_status": "resolving_conflicts",
                "deployment_conflict_attempts": attempt,
                "deployment_error": None,
                "latest_activity": (
                    "Conflict detected; resolving automatically "
                    f"({attempt}/{self.max_conflict_resolution_attempts})"
                ),
            },
        )
        self.db.audit(
            f"{_operation_mode(job)}.conflict",
            "queued",
            {"attempt": attempt, "head_sha": str(job.get("ci_head_sha") or "")},
            job_id,
        )
        await self.reporter.refresh(job_id, force=True)
        try:
            await self.conflict_rebaser(job_id)
        except (GhError, ValueError) as exc:
            raise DeploymentError(
                f"Automatic conflict resolution could not start: {exc}"
            ) from exc

    async def _wait_for_conflict_recovery(self, job_id: str) -> None:
        while True:
            job = self._require_job(job_id)
            code_status = str(job.get("status") or "")
            if code_status == "ready":
                self.db.update_code_job(
                    job_id,
                    {
                        "deployment_status": "merging",
                        "deployment_error": None,
                        "latest_activity": (
                            "Conflict resolution passed CI; retrying pull request merge"
                        ),
                    },
                )
                await self.reporter.refresh(job_id, force=True)
                return
            if code_status in {"failed", "interrupted", "discarded"}:
                detail = str(job.get("error") or code_status)
                raise DeploymentError(
                    f"Automatic conflict resolution failed during {code_status}: {detail}"
                )
            if code_status not in ACTIVE_CONFLICT_RECOVERY_STATUSES:
                raise DeploymentError(
                    "Automatic conflict resolution entered an unexpected code-job "
                    f"state: {code_status or 'unknown'}."
                )
            await asyncio.sleep(self.poll_seconds)

    def _validate_preflight(
        self,
        pr: PullRequestSnapshot,
    ) -> None:
        if pr.state != "OPEN":
            raise DeploymentError(f"Pull request is not open. Current state: {pr.state or 'unknown'}")
        if pr.is_draft:
            raise DeploymentError("Pull request is still a draft.")
        if pr.mergeable != "MERGEABLE":
            raise DeploymentError(f"Pull request is not mergeable: {pr.mergeable or 'unknown'}.")
        if pr.review_decision in {"CHANGES_REQUESTED", "REVIEW_REQUIRED"}:
            raise DeploymentError(
                f"Pull request review requirement is not satisfied: {pr.review_decision.lower()}."
            )
        if pr.merge_state_status in {"BLOCKED", "DIRTY"}:
            raise DeploymentError(
                f"Pull request merge is blocked: {pr.merge_state_status.lower()}."
            )
        rejected = [check for check in pr.checks if check.bucket not in {"pass", "skipping"}]
        if rejected:
            summary = ", ".join(f"{check.name} ({check.state or check.bucket})" for check in rejected)
            raise DeploymentError(f"Pull request checks are not passing: {summary}")

    def _validate_checked_identity(
        self,
        job: dict[str, Any],
        pr: PullRequestSnapshot,
        *,
        require_main: bool,
    ) -> None:
        base_branch = str(job.get("base_branch") or "")
        if not base_branch or pr.base_branch != base_branch:
            raise DeploymentError("Pull request base branch does not match the checked code job.")
        if require_main and base_branch != "main":
            raise DeploymentError("Deployment only supports pull requests targeting main.")
        checked_sha = str(job.get("ci_head_sha") or "")
        if not checked_sha or pr.head_sha != checked_sha:
            raise DeploymentError("Pull request head changed after checks passed; run a new checked code job.")

    async def _monitor_deployment(self, job_id: str) -> None:
        job = self._require_job(job_id)
        repo = str(job["repo"])
        workflow = self.db.get_repo_deploy_workflow(repo)
        if not workflow:
            raise DeploymentError("Deployment workflow configuration was removed after merge.")
        merge_sha = str(job.get("deployment_merge_sha") or "")
        if not merge_sha:
            raise DeploymentError("Merged pull request has no stored merge SHA.")
        run_id = int(job.get("deployment_run_id") or 0)
        if not run_id:
            deployment_status = str(job.get("deployment_status") or "")
            started = int(job.get("deployment_started_at") or time.time())
            if deployment_status == "waiting_workflow":
                started = int(time.time())
                self.db.update_code_job(
                    job_id,
                    {
                        "deployment_status": "dispatching",
                        "deployment_started_at": started,
                        "latest_activity": f"Dispatching deployment workflow: {workflow}",
                    },
                )
                await self.reporter.refresh(job_id, force=True)
                run = await asyncio.to_thread(
                    self.github.dispatch_workflow,
                    repo=repo,
                    workflow=workflow,
                    commit_sha=merge_sha,
                )
                self.db.audit(
                    "deploy.dispatch",
                    "ok",
                    {"repo": repo, "workflow": workflow, "merge_sha": merge_sha},
                    job_id,
                )
                if run:
                    run_id = run.run_id
                    self.db.update_code_job(
                        job_id,
                        {
                            "deployment_status": "deploying",
                            "deployment_run_id": run.run_id,
                            "deployment_run_url": run.url,
                            "deployment_started_at": int(time.time()),
                            "latest_activity": f"Deployment workflow requested: {workflow}",
                        },
                    )
                    await self.reporter.refresh(job_id, force=True)
            while True:
                if run_id:
                    break
                run = await asyncio.to_thread(
                    self.github.find_dispatched_workflow_run,
                    repo=repo,
                    workflow=workflow,
                    not_before=started,
                )
                if run:
                    run_id = run.run_id
                    self.db.update_code_job(
                        job_id,
                        {
                            "deployment_status": "deploying",
                            "deployment_run_id": run.run_id,
                            "deployment_run_url": run.url,
                            "deployment_started_at": int(time.time()),
                            "latest_activity": f"Deployment workflow running: {run.workflow_name or workflow}",
                        },
                    )
                    await self.reporter.refresh(job_id, force=True)
                    break
                if time.time() - started >= self.discovery_seconds:
                    raise DeploymentError(
                        f"Merged successfully, but dispatched workflow '{workflow}' did not start within 2 minutes."
                    )
                await asyncio.sleep(self.poll_seconds)
        job = self._require_job(job_id)
        started = int(job.get("deployment_started_at") or time.time())
        while True:
            run = await asyncio.to_thread(self.github.get_workflow_run, repo=repo, run_id=run_id)
            if run.status == "completed":
                if run.conclusion != "success":
                    raise DeploymentError(
                        f"Merged successfully, but deployment workflow finished with {run.conclusion or 'unknown'}."
                    )
                self.db.update_code_job(
                    job_id,
                    {
                        "deployment_status": "succeeded",
                        "deployment_run_url": run.url,
                        "deployment_error": None,
                        "latest_activity": "Deployment succeeded",
                    },
                )
                self.db.audit(
                    "deploy.workflow",
                    "ok",
                    {"repo": repo, "run_id": run.run_id, "merge_sha": merge_sha},
                    job_id,
                )
                await self.reporter.refresh(job_id, force=True)
                await self._notify(job_id)
                return
            if time.time() - started >= self.deploy_timeout_seconds:
                raise DeploymentError("Merged successfully, but deployment timed out after 30 minutes.")
            self.db.update_code_job(job_id, {"latest_activity": "Waiting for deployment workflow"})
            await self.reporter.refresh(job_id)
            await asyncio.sleep(self.poll_seconds)

    async def _fail(self, job_id: str, error: str) -> None:
        job = self.db.get_code_job(job_id)
        if not job:
            return
        mode = _operation_mode(job)
        merged = bool(job.get("deployment_merge_sha"))
        if mode == MERGE_MODE:
            activity = "Merge failed"
            audit_action = "merge.merge"
        else:
            activity = "Merged to main; deployment failed" if merged else "Merge and deployment failed"
            audit_action = "deploy.workflow" if merged else "deploy.merge"
        self.db.update_code_job(
            job_id,
            {
                "deployment_status": "failed",
                "deployment_error": error,
                "latest_activity": activity,
            },
        )
        self.db.audit(audit_action, "failed", {"error": error}, job_id)
        try:
            await self.reporter.refresh(job_id, force=True)
            if mode == MERGE_MODE:
                await self._notify_merge(job_id)
            else:
                await self._notify(job_id)
        except Exception:
            logging.exception("Failed to report %s failure for %s", mode, job_id)

    async def _notify(self, job_id: str) -> None:
        try:
            await self.reporter.notify_deployment(job_id)
        except Exception:
            logging.exception("Failed to send deployment notification for %s", job_id)

    async def _notify_merge(self, job_id: str) -> None:
        try:
            await self.reporter.notify_merge(job_id)
        except Exception:
            logging.exception("Failed to send merge notification for %s", job_id)

    def _require_job(self, job_id: str) -> dict[str, Any]:
        job = self.db.get_code_job(job_id)
        if not job:
            raise DeploymentError("Code job not found.")
        return job

    @staticmethod
    def _status_message(job: dict[str, Any], heading: str) -> str:
        lines = [heading, f"Code Job ID: {job['id']}"]
        if job.get("deployment_merge_sha"):
            lines.append(f"Merge commit: {str(job['deployment_merge_sha'])[:12]}")
        if job.get("deployment_run_url"):
            lines.append(f"Deployment: {job['deployment_run_url']}")
        if job.get("deployment_error"):
            lines.append(f"Error: {job['deployment_error']}")
        return "\n".join(lines)


def _operation_mode(job: dict[str, Any]) -> str:
    mode = str(job.get("deployment_mode") or "")
    if mode in {MERGE_MODE, DEPLOY_MODE}:
        return mode
    return DEPLOY_MODE if job.get("deployment_status") else ""


def _has_explicit_conflict(pr: PullRequestSnapshot) -> bool:
    return pr.mergeable == "CONFLICTING" or pr.merge_state_status == "DIRTY"
