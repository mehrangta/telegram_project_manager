from __future__ import annotations

import asyncio
import logging
import time
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
ACTIVE_DEPLOYMENT_STATUSES = {"queued", "merging", "waiting_workflow", "deploying"}


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
    ) -> None:
        self.db = db
        self.github = github
        self.reporter = reporter
        self.poll_seconds = poll_seconds
        self.merge_timeout_seconds = merge_timeout_seconds
        self.discovery_seconds = discovery_seconds
        self.deploy_timeout_seconds = deploy_timeout_seconds
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
        job = self._require_job(job_id)
        if job["status"] != "ready":
            raise DeploymentError("Code job is not ready. All pull request checks must pass first.")
        workflow = self.db.get_repo_deploy_workflow(str(job["repo"]))
        if not workflow:
            raise DeploymentError(
                "No deployment workflow configured. Admin: "
                f"/repo deploy set {job['repo']} <workflow-name-or-file>"
            )
        deployment_status = str(job.get("deployment_status") or "")
        if deployment_status == "succeeded":
            return self._status_message(job, "Deployment already succeeded.")
        if deployment_status in ACTIVE_DEPLOYMENT_STATUSES:
            self._schedule(job_id)
            return self._status_message(job, "Merge and deployment already in progress.")
        if not self.db.start_code_job_deployment(job_id):
            current = self._require_job(job_id)
            return self._status_message(current, "Merge and deployment already in progress.")
        self.db.audit("deploy.queue", "ok", {"repo": job["repo"], "workflow": workflow}, job_id)
        self._schedule(job_id)
        try:
            await self.reporter.refresh(job_id, force=True)
        except Exception:
            logging.exception("Failed to refresh queued deployment %s", job_id)
        return f"Merge and deployment queued.\nCode Job ID: {job_id}\nRepo: {job['repo']}"

    def _schedule(self, job_id: str) -> None:
        existing = self._tasks.get(job_id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self._run(job_id), name=f"deploy-{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job_id, None))

    async def _run(self, job_id: str) -> None:
        try:
            job = self._require_job(job_id)
            if str(job.get("deployment_status") or "") in {"queued", "merging"}:
                await self._merge(job_id)
            job = self._require_job(job_id)
            if str(job.get("deployment_status") or "") in {"waiting_workflow", "deploying"}:
                await self._monitor_deployment(job_id)
        except asyncio.CancelledError:
            raise
        except (DeploymentError, GhError, ValueError) as exc:
            await self._fail(job_id, str(exc))
        except Exception as exc:
            logging.exception("Unexpected merge/deployment failure: %s", job_id)
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
        if not snapshot.merged:
            self._validate_preflight(job, snapshot)
            await asyncio.to_thread(
                self.github.squash_merge,
                pr_url=pr_url,
                head_sha=snapshot.head_sha,
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
        now = int(time.time())
        self.db.update_code_job(
            job_id,
            {
                "deployment_status": "waiting_workflow",
                "deployment_merge_sha": snapshot.merge_sha,
                "deployment_started_at": now,
                "latest_activity": "Merged to main; waiting for deployment workflow",
            },
        )
        self.db.audit(
            "deploy.merge",
            "ok",
            {"repo": job["repo"], "merge_sha": snapshot.merge_sha},
            job_id,
        )
        await self.reporter.refresh(job_id, force=True)

    def _validate_preflight(self, job: dict[str, Any], pr: PullRequestSnapshot) -> None:
        if pr.state != "OPEN":
            raise DeploymentError(f"Pull request is not open. Current state: {pr.state or 'unknown'}")
        if pr.is_draft:
            raise DeploymentError("Pull request is still a draft.")
        if str(job.get("base_branch") or "") != "main" or pr.base_branch != "main":
            raise DeploymentError("Deployment only supports pull requests targeting main.")
        checked_sha = str(job.get("ci_head_sha") or "")
        if not checked_sha or pr.head_sha != checked_sha:
            raise DeploymentError("Pull request head changed after checks passed; run a new checked code job.")
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
            started = int(job.get("deployment_started_at") or time.time())
            while True:
                run = await asyncio.to_thread(
                    self.github.find_workflow_run,
                    repo=repo,
                    workflow=workflow,
                    commit_sha=merge_sha,
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
                        f"Merged successfully, but deployment workflow '{workflow}' did not start within 2 minutes."
                    )
                await asyncio.sleep(self.poll_seconds)
        job = self._require_job(job_id)
        started = int(job.get("deployment_started_at") or time.time())
        while True:
            run = await asyncio.to_thread(self.github.get_workflow_run, repo=repo, run_id=run_id)
            if run.head_sha and run.head_sha != merge_sha:
                raise DeploymentError("Deployment workflow run targets the wrong commit.")
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
        merged = bool(job.get("deployment_merge_sha"))
        activity = "Merged to main; deployment failed" if merged else "Merge and deployment failed"
        self.db.update_code_job(
            job_id,
            {
                "deployment_status": "failed",
                "deployment_error": error,
                "latest_activity": activity,
            },
        )
        self.db.audit("deploy.workflow" if merged else "deploy.merge", "failed", {"error": error}, job_id)
        try:
            await self.reporter.refresh(job_id, force=True)
            await self._notify(job_id)
        except Exception:
            logging.exception("Failed to report deployment failure for %s", job_id)

    async def _notify(self, job_id: str) -> None:
        try:
            await self.reporter.notify_deployment(job_id)
        except Exception:
            logging.exception("Failed to send deployment notification for %s", job_id)

    def _require_job(self, job_id: str) -> dict[str, Any]:
        job = self.db.get_code_job(job_id)
        if not job:
            raise DeploymentError("Code job not found.")
        return job

    @staticmethod
    def _status_message(job: dict[str, Any], heading: str) -> str:
        lines = [heading, f"Code Job ID: {job['id']}"]
        if job.get("deployment_run_url"):
            lines.append(f"Deployment: {job['deployment_run_url']}")
        if job.get("deployment_error"):
            lines.append(f"Error: {job['deployment_error']}")
        return "\n".join(lines)
