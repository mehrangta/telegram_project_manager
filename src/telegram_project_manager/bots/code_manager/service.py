from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from openai_codex import Sandbox
from openai_codex.types import ReasoningEffort

from telegram_project_manager.bots.code_manager.codex_sdk import CodexSdkAdapter, CodexSdkError
from telegram_project_manager.bots.code_manager.progress import CodeProgressReporter
from telegram_project_manager.bots.code_manager.prompts import (
    DEVELOPER_INSTRUCTIONS,
    ci_repair_prompt,
    coding_prompt,
    plan_edit_prompt,
    planning_prompt,
    rebase_conflict_prompt,
)
from telegram_project_manager.bots.code_manager.schemas import (
    CODE_PLAN_SCHEMA,
    CODE_RESULT_SCHEMA,
    CodeJobValidationError,
    CodePlan,
    CodeResult,
)
from telegram_project_manager.bots.code_manager.workspace import (
    CodeGitHubService,
    GitWorkspaceService,
    IssueContext,
    PullRequestCheck,
    WorkspaceError,
)
from telegram_project_manager.integrations.gh.runner import GhError
from telegram_project_manager.platform.storage.db import Database


PLAN_PATH_TEMPLATE = ".codex/plans/{job_id}.md"
MAX_QUEUED_JOBS = 10
PLAN_TIMEOUT_SECONDS = 15 * 60
CODE_TIMEOUT_SECONDS = 45 * 60
CHECK_POLL_SECONDS = 10
CHECK_GRACE_SECONDS = 60
CHECK_TIMEOUT_SECONDS = 30 * 60
MAX_CI_REPAIR_ATTEMPTS = 2
MAX_REBASE_CONFLICT_ROUNDS = 20
TERMINAL_STATUSES = {"ready", "discarded"}
ACTIVE_STATUSES = {
    "queued_plan",
    "queued_plan_edit",
    "queued_code",
    "queued_checks",
    "queued_rebase",
    "preparing",
    "planning",
    "editing_plan",
    "awaiting_approval",
    "coding",
    "validating",
    "pushing",
    "waiting_checks",
    "repairing_checks",
    "rebasing",
    "failed",
    "interrupted",
}


class CodeJobService:
    def __init__(
        self,
        *,
        db: Database,
        codex: CodexSdkAdapter,
        workspaces: GitWorkspaceService,
        github: CodeGitHubService,
        reporter: CodeProgressReporter,
        max_concurrent: int = 2,
        check_poll_seconds: float = CHECK_POLL_SECONDS,
        check_grace_seconds: float = CHECK_GRACE_SECONDS,
        check_timeout_seconds: float = CHECK_TIMEOUT_SECONDS,
        max_ci_repair_attempts: int = MAX_CI_REPAIR_ATTEMPTS,
    ) -> None:
        self.db = db
        self.codex = codex
        self.workspaces = workspaces
        self.github = github
        self.reporter = reporter
        self.check_poll_seconds = check_poll_seconds
        self.check_grace_seconds = check_grace_seconds
        self.check_timeout_seconds = check_timeout_seconds
        self.max_ci_repair_attempts = max_ci_repair_attempts
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def recover(self) -> None:
        self.db.mark_running_code_jobs_interrupted()
        for job in self.db.list_code_jobs(limit=100):
            if job["status"] in {
                "queued_plan", "queued_plan_edit", "queued_code", "queued_checks",
                "queued_rebase", "waiting_checks"
            }:
                self._schedule(str(job["id"]), str(job["resume_phase"]))
            elif job["status"] == "interrupted":
                try:
                    await self.reporter.refresh(str(job["id"]), force=True)
                except Exception:
                    logging.exception("Failed to refresh interrupted code job %s", job["id"])

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        await self.codex.close()

    async def create_job(
        self,
        *,
        chat_id: int,
        user_id: int,
        thread_id: int | None,
        issue: IssueContext,
        base_branch: str,
        source_path: str,
        skip_plan: bool,
    ) -> str:
        resolved_source = await asyncio.to_thread(
            self.workspaces.validate_source,
            source_path=source_path,
            repo=issue.repo,
        )
        if self.db.count_queued_code_jobs() >= MAX_QUEUED_JOBS:
            raise ValueError("Code job queue is full. Retry after another job finishes.")
        active = self.db.get_active_code_job(issue.repo, issue.number)
        if active:
            raise ValueError(f"Active code job already exists: {active['id']}")
        job_id = f"c-{uuid.uuid4().hex[:8]}"
        target_branch = f"codex/issue-{issue.number}-{job_id}"
        workspace = (self.db.path.parent / "code-jobs" / job_id / "repo").resolve()
        phase = "code" if skip_plan else "plan"
        status = "queued_code" if skip_plan else "queued_plan"
        self.db.create_code_job(
            {
                "id": job_id,
                "telegram_chat_id": chat_id,
                "telegram_user_id": user_id,
                "telegram_thread_id": thread_id,
                "repo": issue.repo,
                "issue_number": issue.number,
                "issue_title": issue.title,
                "issue_url": issue.url,
                "issue_context_json": issue.to_json(),
                "base_branch": base_branch,
                "target_branch": target_branch,
                "workspace_path": str(workspace),
                "source_repo_path": resolved_source,
                "status": status,
                "resume_phase": phase,
                "skip_plan": skip_plan,
            }
        )
        self.db.audit(
            "code.job",
            "queued",
            {"repo": issue.repo, "issue": issue.number, "skip_plan": skip_plan},
            job_id,
        )
        await self.reporter.create(job_id)
        self._schedule(job_id, phase)
        return job_id

    async def approve(self, job_id: str) -> None:
        changed = self.db.update_code_job(
            job_id,
            {"status": "queued_code", "resume_phase": "code", "error": None},
            allowed_statuses=("awaiting_approval",),
        )
        if not changed:
            raise ValueError("Code job is not awaiting approval.")
        self.db.audit("code.approve", "ok", {}, job_id)
        await self.reporter.refresh(job_id, force=True)
        self._schedule(job_id, "code")

    async def edit_plan(self, job_id: str, feedback: str) -> None:
        if not feedback.strip():
            raise ValueError("Plan feedback is required.")
        job = self._require_job(job_id)
        if job["status"] != "awaiting_approval":
            raise ValueError("Code job is not awaiting plan feedback.")
        feedback_items = [str(item) for item in job.get("feedback_json") or []]
        feedback_items.append(feedback.strip())
        changed = self.db.update_code_job(
            job_id,
            {
                "status": "queued_plan_edit",
                "resume_phase": "plan",
                "feedback_json": feedback_items,
                "error": None,
            },
            allowed_statuses=("awaiting_approval",),
        )
        if not changed:
            raise ValueError("Code job plan changed concurrently; retry your feedback.")
        self.db.audit("code.plan.edit", "queued", {"revision": int(job["plan_revision"]) + 1}, job_id)
        await self.reporter.refresh(job_id, force=True)
        self._schedule(job_id, "plan")

    async def retry(self, job_id: str) -> None:
        job = self._require_job(job_id)
        if job["status"] not in {"failed", "interrupted"}:
            raise ValueError("Only failed or interrupted code jobs can be retried.")
        phase = str(job["resume_phase"])
        if phase == "rebase":
            status = "queued_rebase"
        elif phase == "checks":
            status = "queued_checks"
        elif phase == "code":
            status = "queued_code"
        else:
            status = "queued_plan_edit" if job.get("pull_request_number") else "queued_plan"
        if not self.db.update_code_job(
            job_id,
            {"status": status, "error": None, "latest_activity": "Retry queued"},
            allowed_statuses=("failed", "interrupted"),
        ):
            raise ValueError("Code job changed concurrently; retry again.")
        self.db.audit("code.retry", "ok", {"phase": phase}, job_id)
        await self.reporter.refresh(job_id, force=True)
        self._schedule(job_id, phase)

    async def rebase(self, job_id: str) -> None:
        job = self._require_job(job_id)
        if job["status"] != "ready":
            raise ValueError("Only a ready, checked code job can be rebased.")
        if not job.get("pull_request_url"):
            raise ValueError("Code job has no pull request to rebase.")
        if job.get("deployment_merge_sha"):
            raise ValueError("This pull request was already merged and cannot be rebased.")
        if str(job.get("deployment_status") or "") in {
            "queued", "merging", "waiting_workflow", "dispatching", "deploying", "succeeded"
        }:
            raise ValueError("Code job is already deploying or deployed.")
        active = self.db.get_active_code_job(str(job["repo"]), int(job["issue_number"]))
        if active and str(active["id"]) != job_id:
            raise ValueError(f"A newer active code job exists: {active['id']}")
        remote_sha = await asyncio.to_thread(
            self.github.get_pr_head_sha, str(job["pull_request_url"])
        )
        if remote_sha != str(job.get("ci_head_sha") or ""):
            if not self.db.update_code_job(
                job_id,
                {
                    "status": "queued_checks",
                    "resume_phase": "checks",
                    "latest_activity": "PR head changed; checking the new commit",
                    "ci_head_sha": remote_sha,
                    "ci_wait_started_at": int(time.time()),
                    "ci_repair_attempts": 0,
                    "ci_checks_json": [],
                    "error": None,
                    "deployment_status": None,
                    "deployment_error": None,
                    "deployment_run_id": None,
                    "deployment_run_url": None,
                    "deployment_started_at": None,
                },
                allowed_statuses=("ready",),
            ):
                raise ValueError("Code job changed concurrently; retry the rebase command.")
            self.db.audit(
                "code.checks",
                "queued",
                {"head_sha": remote_sha, "reason": "pr_head_changed_before_rebase"},
                job_id,
            )
            await self.reporter.refresh(job_id, force=True)
            self._schedule(job_id, "checks")
            return
        if not self.db.update_code_job(
            job_id,
            {
                "status": "queued_rebase",
                "resume_phase": "rebase",
                "latest_activity": "Rebase queued",
                "error": None,
                "deployment_status": None,
                "deployment_error": None,
                "deployment_run_id": None,
                "deployment_run_url": None,
                "deployment_started_at": None,
            },
            allowed_statuses=("ready",),
        ):
            raise ValueError("Code job changed concurrently; retry the rebase command.")
        self.db.audit("code.rebase", "queued", {"head_sha": remote_sha}, job_id)
        await self.reporter.refresh(job_id, force=True)
        self._schedule(job_id, "rebase")

    async def discard(self, job_id: str) -> None:
        job = self._require_job(job_id)
        if job["status"] in TERMINAL_STATUSES:
            if job["status"] == "discarded":
                return
            raise ValueError("A ready code job cannot be discarded by the bot.")
        self.db.update_code_job(
            job_id,
            {"status": "discarded", "latest_activity": "Discarded", "error": None},
        )
        await self.codex.interrupt(job_id)
        await asyncio.to_thread(
            self.github.discard,
            repo=str(job["repo"]),
            number=int(job["pull_request_number"]) if job.get("pull_request_number") else None,
            branch=str(job["target_branch"]),
        )
        source = self._ensure_source(job)
        await asyncio.to_thread(
            self.workspaces.cleanup,
            source_path=source,
            path=Path(str(job["workspace_path"])),
            target_branch=str(job["target_branch"]),
        )
        self.db.audit("code.discard", "ok", {}, job_id)
        await self.reporter.refresh(job_id, force=True)

    def status(self, job_id: str) -> dict[str, Any]:
        return self._require_job(job_id)

    def _schedule(self, job_id: str, phase: str) -> None:
        existing = self._tasks.get(job_id)
        if existing and not existing.done():
            task = asyncio.create_task(
                self._run_after(existing, job_id, phase),
                name=f"code-job-{job_id}-{phase}-deferred",
            )
        else:
            task = asyncio.create_task(self._run(job_id, phase), name=f"code-job-{job_id}-{phase}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda finished: self._forget_task(job_id, finished))

    async def _run_after(
        self, previous: asyncio.Task[None], job_id: str, phase: str
    ) -> None:
        await previous
        await self._run(job_id, phase)

    def _forget_task(self, job_id: str, finished: asyncio.Task[None]) -> None:
        if self._tasks.get(job_id) is finished:
            self._tasks.pop(job_id, None)

    async def _run(self, job_id: str, phase: str) -> None:
        failure_phase = phase
        try:
            if phase == "plan":
                async with self._semaphore:
                    await self._run_plan(job_id)
            elif phase == "code":
                async with self._semaphore:
                    await self._run_code(job_id)
                job = self.db.get_code_job(job_id)
                if job and job["status"] == "queued_checks":
                    failure_phase = "checks"
                    await self._run_checks(job_id)
            elif phase == "rebase":
                async with self._semaphore:
                    await self._run_rebase(job_id)
                job = self.db.get_code_job(job_id)
                if job and job["status"] == "queued_checks":
                    failure_phase = "checks"
                    await self._run_checks(job_id)
            else:
                failure_phase = "checks"
                await self._run_checks(job_id)
        except asyncio.CancelledError:
            raise
        except (CodexSdkError, CodeJobValidationError, WorkspaceError, GhError, ValueError) as exc:
            await self._fail(job_id, str(exc), failure_phase)
        except Exception as exc:
            logging.exception("Unexpected code job failure: %s", job_id)
            await self._fail(job_id, f"Unexpected failure: {exc}", failure_phase)

    async def _run_rebase(self, job_id: str) -> None:
        job = self._require_job(job_id)
        if not self.db.update_code_job(
            job_id,
            {"status": "rebasing", "resume_phase": "rebase", "latest_activity": "Preparing rebase workspace"},
            allowed_statuses=("queued_rebase",),
        ):
            return
        await self.reporter.refresh(job_id, force=True)
        path = Path(str(job["workspace_path"]))
        source = self._ensure_source(job)
        checked_sha = str(job.get("ci_head_sha") or "")
        remote_sha = await asyncio.to_thread(
            self.github.get_pr_head_sha, str(job["pull_request_url"])
        )
        if remote_sha != checked_sha:
            raise ValueError("Pull request head changed after the rebase was queued.")
        checkout_sha = await asyncio.to_thread(
            self.workspaces.checkout_existing,
            source_path=source,
            repo=str(job["repo"]),
            branch=str(job["target_branch"]),
            path=path,
        )
        if checkout_sha != checked_sha:
            raise ValueError("Remote branch no longer matches the checked pull request head.")
        base_sha, conflicts = await asyncio.to_thread(
            self.workspaces.start_conflict_aware_rebase, path, str(job["base_branch"])
        )
        resolutions: list[dict[str, Any]] = []
        rounds = 0
        while conflicts:
            rounds += 1
            if rounds > MAX_REBASE_CONFLICT_ROUNDS:
                raise WorkspaceError(
                    f"Rebase exceeded {MAX_REBASE_CONFLICT_ROUNDS} conflict-resolution rounds"
                )
            self.db.update_code_job(
                job_id,
                {"latest_activity": f"Codex is resolving rebase conflicts ({rounds})"},
            )
            await self.reporter.refresh(job_id, force=True)
            _, raw = await self.codex.run_turn(
                job_id=job_id,
                cwd=str(path),
                prompt=rebase_conflict_prompt(dict(job["issue_context_json"]), conflicts, rounds),
                output_schema=CODE_RESULT_SCHEMA,
                sandbox=Sandbox.workspace_write,
                effort=ReasoningEffort.high,
                developer_instructions=DEVELOPER_INSTRUCTIONS,
                thread_id=str(job["codex_thread_id"]) if job.get("codex_thread_id") else None,
                timeout_seconds=CODE_TIMEOUT_SECONDS,
                on_progress=lambda event: self.reporter.activity(job_id, event),
                on_thread=lambda thread_id: self._store_thread(job_id, thread_id),
            )
            result = CodeResult.from_json(raw)
            resolutions.append({**result.to_json(), "files": list(conflicts), "round": rounds})
            conflicts = await asyncio.to_thread(
                self.workspaces.continue_conflict_aware_rebase, path, conflicts
            )
        sha = await asyncio.to_thread(self.workspaces.head_sha, path)
        await asyncio.to_thread(self.workspaces.push_rebased_branch, path)
        stored = dict(job["result_json"]) if isinstance(job.get("result_json"), dict) else {}
        stored["rebase"] = {
            "previous_head_sha": checked_sha,
            "base_sha": base_sha,
            "head_sha": sha,
            "conflict_resolutions": resolutions,
        }
        if not self.db.update_code_job(
            job_id,
            {
                "status": "queued_checks",
                "resume_phase": "checks",
                "latest_activity": "Rebased pull request pushed; waiting for CI checks",
                "base_sha": base_sha,
                "result_json": stored,
                "ci_head_sha": sha,
                "ci_wait_started_at": int(time.time()),
                "ci_repair_attempts": 0,
                "ci_checks_json": [],
                "error": None,
            },
            allowed_statuses=("rebasing",),
        ):
            return
        self.db.audit(
            "code.rebase", "ok", {"sha": sha, "base_sha": base_sha, "rounds": rounds}, job_id
        )
        await self.reporter.refresh(job_id, force=True)

    async def _run_plan(self, job_id: str) -> None:
        job = self._require_job(job_id)
        editing = bool(job.get("pull_request_number"))
        status = "editing_plan" if editing else "preparing"
        allowed = ("queued_plan_edit",) if editing else ("queued_plan",)
        if not self.db.update_code_job(
            job_id,
            {"status": status, "resume_phase": "plan", "latest_activity": "Preparing repository"},
            allowed_statuses=allowed,
        ):
            return
        await self.reporter.refresh(job_id, force=True)
        path = Path(str(job["workspace_path"]))
        source = self._ensure_source(job)
        if editing and not (path / ".git").exists():
            await asyncio.to_thread(
                self.workspaces.checkout_existing,
                source_path=source,
                repo=str(job["repo"]),
                branch=str(job["target_branch"]),
                path=path,
            )
        if editing:
            base_sha = await asyncio.to_thread(
                self.workspaces.refresh_base, path, str(job["base_branch"])
            )
            await asyncio.to_thread(self.workspaces.push_rebased_branch, path)
            self.db.update_code_job(job_id, {"base_sha": base_sha})
        elif not editing:
            base_sha = await asyncio.to_thread(
                self.workspaces.prepare,
                source_path=source,
                repo=str(job["repo"]),
                base_branch=str(job["base_branch"]),
                target_branch=str(job["target_branch"]),
                path=path,
            )
            self.db.update_code_job(job_id, {"base_sha": base_sha})
        if self._discarded(job_id):
            return
        self.db.update_code_job(
            job_id,
            {"status": "editing_plan" if editing else "planning", "latest_activity": "Codex is inspecting the repository"},
        )
        await self.reporter.refresh(job_id, force=True)
        job = self._require_job(job_id)
        issue = dict(job["issue_context_json"])
        feedback = [str(item) for item in job.get("feedback_json") or []]
        prompt = (
            plan_edit_prompt(issue, dict(job["plan_json"]), feedback)
            if editing and isinstance(job.get("plan_json"), dict)
            else planning_prompt(issue, feedback)
        )
        _, raw = await self.codex.run_turn(
            job_id=job_id,
            cwd=str(path),
            prompt=prompt,
            output_schema=CODE_PLAN_SCHEMA,
            sandbox=Sandbox.read_only,
            effort=ReasoningEffort.high,
            developer_instructions=DEVELOPER_INSTRUCTIONS,
            thread_id=str(job["codex_thread_id"]) if job.get("codex_thread_id") else None,
            timeout_seconds=PLAN_TIMEOUT_SECONDS,
            on_progress=lambda event: self.reporter.activity(job_id, event),
            on_thread=lambda thread_id: self._store_thread(job_id, thread_id),
        )
        plan = CodePlan.from_json(raw)
        if self._discarded(job_id):
            return
        revision = int(job.get("plan_revision") or 0) + 1
        plan_path = PLAN_PATH_TEMPLATE.format(job_id=job_id)
        markdown = plan.to_markdown(job_id, str(job["repo"]), int(job["issue_number"]), revision)
        await self.reporter.activity(job_id, {"kind": "phase", "text": "Publishing plan to draft pull request"}, force=True)
        await asyncio.to_thread(
            self.workspaces.commit_plan,
            path=path,
            plan_path=plan_path,
            markdown=markdown,
            message=(
                f"docs: revise Codex plan for #{job['issue_number']}"
                if editing
                else f"docs: add Codex plan for #{job['issue_number']}"
            ),
            first_push=not editing,
        )
        pr_body = _plan_pr_body(job, markdown)
        title = f"Draft: #{job['issue_number']} {job['issue_title']}"[:256]
        values: dict[str, Any] = {
            "plan_json": plan.to_json(),
            "plan_revision": revision,
            "status": "awaiting_approval",
            "latest_activity": "Plan ready for approval",
            "error": None,
        }
        if editing:
            await asyncio.to_thread(
                self.github.update_pr,
                repo=str(job["repo"]),
                number=int(job["pull_request_number"]),
                title=title,
                body=pr_body,
            )
        else:
            pr = await asyncio.to_thread(
                self.github.create_draft_pr,
                repo=str(job["repo"]),
                title=title,
                body=pr_body,
                head=str(job["target_branch"]),
                base=str(job["base_branch"]),
            )
            values.update(
                {
                    "pull_request_number": int(pr["number"]),
                    "pull_request_url": str(pr["html_url"]),
                }
            )
        self.db.update_code_job(job_id, values)
        self.db.audit("code.plan", "ok", {"revision": revision}, job_id)
        await self.reporter.refresh(job_id, force=True)

    async def _run_code(self, job_id: str) -> None:
        job = self._require_job(job_id)
        if not self.db.update_code_job(
            job_id,
            {"status": "preparing", "resume_phase": "code", "latest_activity": "Preparing coding workspace"},
            allowed_statuses=("queued_code",),
        ):
            return
        await self.reporter.refresh(job_id, force=True)
        path = Path(str(job["workspace_path"]))
        source = self._ensure_source(job)
        if not (path / ".git").exists():
            if job.get("pull_request_number"):
                await asyncio.to_thread(
                    self.workspaces.checkout_existing,
                    source_path=source,
                    repo=str(job["repo"]),
                    branch=str(job["target_branch"]),
                    path=path,
                )
            else:
                base_sha = await asyncio.to_thread(
                    self.workspaces.prepare,
                    source_path=source,
                    repo=str(job["repo"]),
                    base_branch=str(job["base_branch"]),
                    target_branch=str(job["target_branch"]),
                    path=path,
                )
                self.db.update_code_job(job_id, {"base_sha": base_sha})
        if not await asyncio.to_thread(self.workspaces.is_dirty, path):
            base_sha = await asyncio.to_thread(self.workspaces.refresh_base, path, str(job["base_branch"]))
            if job.get("pull_request_number"):
                await asyncio.to_thread(self.workspaces.push_rebased_branch, path)
            self.db.update_code_job(job_id, {"base_sha": base_sha})
        if self._discarded(job_id):
            return
        self.db.update_code_job(job_id, {"status": "coding", "latest_activity": "Codex is implementing the issue"})
        await self.reporter.refresh(job_id, force=True)
        job = self._require_job(job_id)
        plan = dict(job["plan_json"]) if isinstance(job.get("plan_json"), dict) else None
        plan_path = PLAN_PATH_TEMPLATE.format(job_id=job_id)
        _, raw = await self.codex.run_turn(
            job_id=job_id,
            cwd=str(path),
            prompt=coding_prompt(dict(job["issue_context_json"]), plan, plan_path),
            output_schema=CODE_RESULT_SCHEMA,
            sandbox=Sandbox.workspace_write,
            effort=ReasoningEffort.medium,
            developer_instructions=DEVELOPER_INSTRUCTIONS,
            thread_id=str(job["codex_thread_id"]) if job.get("codex_thread_id") else None,
            timeout_seconds=CODE_TIMEOUT_SECONDS,
            on_progress=lambda event: self.reporter.activity(job_id, event),
            on_thread=lambda thread_id: self._store_thread(job_id, thread_id),
        )
        await asyncio.to_thread(
            self.workspaces.remove_plan,
            path=path,
            plan_path=plan_path,
        )
        result = CodeResult.from_json(raw)
        if self._discarded(job_id):
            return
        self.db.update_code_job(job_id, {"status": "validating", "latest_activity": "Validating Codex changes"})
        await self.reporter.refresh(job_id, force=True)
        files = await asyncio.to_thread(self.workspaces.validate_code_changes, path=path, plan_path=plan_path)
        self.db.update_code_job(job_id, {"status": "pushing", "latest_activity": "Committing and pushing implementation"})
        await self.reporter.refresh(job_id, force=True)
        sha = await asyncio.to_thread(
            self.workspaces.commit_code,
            path=path,
            message=result.commit_message,
            first_push=not bool(job.get("pull_request_number")),
        )
        job = self._require_job(job_id)
        title = f"Fix #{job['issue_number']}: {job['issue_title']}"[:256]
        body = _ready_pr_body(job, result, files, sha)
        if job.get("pull_request_number"):
            await asyncio.to_thread(
                self.github.update_pr,
                repo=str(job["repo"]),
                number=int(job["pull_request_number"]),
                title=title,
                body=body,
            )
            pr_url = str(job["pull_request_url"])
            pr_number = int(job["pull_request_number"])
        else:
            pr = await asyncio.to_thread(
                self.github.create_draft_pr,
                repo=str(job["repo"]),
                title=title,
                body=body,
                head=str(job["target_branch"]),
                base=str(job["base_branch"]),
            )
            pr_url = str(pr["html_url"])
            pr_number = int(pr["number"])
        await asyncio.to_thread(self.github.mark_ready, pr_url)
        if not self.db.update_code_job(
            job_id,
            {
                "status": "queued_checks",
                "resume_phase": "checks",
                "latest_activity": "Pull request ready; waiting for CI checks",
                "pull_request_number": pr_number,
                "pull_request_url": pr_url,
                "result_json": {
                    **result.to_json(),
                    "files": files,
                    "commit_sha": sha,
                    "repairs": [],
                },
                "ci_head_sha": sha,
                "ci_wait_started_at": int(time.time()),
                "ci_repair_attempts": 0,
                "ci_checks_json": [],
                "error": None,
            },
            allowed_statuses=("pushing",),
        ):
            return
        self.db.audit("code.checks", "queued", {"pr": pr_number, "sha": sha}, job_id)
        await self.reporter.refresh(job_id, force=True)

    async def _run_checks(self, job_id: str) -> None:
        job = self._require_job(job_id)
        if job["status"] == "queued_checks":
            if not self.db.update_code_job(
                job_id,
                {"status": "waiting_checks", "resume_phase": "checks", "error": None},
                allowed_statuses=("queued_checks",),
            ):
                return
        elif job["status"] != "waiting_checks":
            return
        await self.reporter.refresh(job_id, force=True)
        while True:
            if self._discarded(job_id):
                return
            job = self._require_job(job_id)
            pr_url = str(job["pull_request_url"])
            remote_sha = await asyncio.to_thread(self.github.get_pr_head_sha, pr_url)
            expected_sha = str(job.get("ci_head_sha") or "")
            if remote_sha != expected_sha:
                now = int(time.time())
                self.db.update_code_job(
                    job_id,
                    {
                        "ci_head_sha": remote_sha,
                        "ci_wait_started_at": now,
                        "ci_checks_json": [],
                        "latest_activity": "PR head changed; waiting for its CI checks",
                    },
                )
                job = self._require_job(job_id)
            checks = await asyncio.to_thread(self.github.get_pr_checks, pr_url)
            snapshot = [check.to_json() for check in checks]
            self.db.update_code_job(job_id, {"ci_checks_json": snapshot})
            started = int(job.get("ci_wait_started_at") or time.time())
            elapsed = time.time() - started

            failures = tuple(
                check for check in checks if check.bucket in {"fail", "cancel"}
            )
            unknown = tuple(
                check
                for check in checks
                if check.bucket not in {"pass", "skipping", "pending", "fail", "cancel"}
            )
            if failures or unknown:
                rejected = failures + unknown
                code_failures = tuple(
                    check
                    for check in rejected
                    if check.bucket == "fail" and check.state in {"failure", "failed"}
                )
                infrastructure = tuple(check for check in rejected if check not in code_failures)
                if infrastructure:
                    raise WorkspaceError(_check_failure_message("CI infrastructure check failed", infrastructure))
                attempts = int(job.get("ci_repair_attempts") or 0)
                if attempts >= self.max_ci_repair_attempts:
                    raise WorkspaceError(
                        _check_failure_message(
                            f"CI checks still fail after {attempts} automatic repairs", code_failures
                        )
                    )
                diagnostics = await asyncio.to_thread(
                    self.github.failed_check_diagnostics,
                    repo=str(job["repo"]),
                    checks=code_failures,
                )
                await self._repair_ci(job_id, diagnostics, attempts + 1)
                continue

            if checks and all(check.bucket in {"pass", "skipping"} for check in checks):
                await self._complete_checks(job_id, checks)
                return
            if not checks and elapsed >= self.check_grace_seconds:
                await self._complete_checks(job_id, checks)
                return
            if elapsed >= self.check_timeout_seconds:
                pending = tuple(check for check in checks if check.bucket == "pending")
                detail = _check_failure_message("Timed out waiting for CI checks", pending)
                raise WorkspaceError(detail)

            pending_count = sum(check.bucket == "pending" for check in checks)
            activity = (
                f"Waiting for {pending_count} pending CI check(s)"
                if checks
                else "Waiting for CI checks to appear"
            )
            self.db.update_code_job(job_id, {"latest_activity": activity})
            await self.reporter.refresh(job_id)
            await asyncio.sleep(self.check_poll_seconds)

    async def _repair_ci(self, job_id: str, diagnostics: str, attempt: int) -> None:
        job = self._require_job(job_id)
        if not self.db.update_code_job(
            job_id,
            {
                "status": "repairing_checks",
                "latest_activity": f"Codex is repairing failed CI checks ({attempt}/{self.max_ci_repair_attempts})",
            },
            allowed_statuses=("waiting_checks",),
        ):
            return
        await self.reporter.refresh(job_id, force=True)
        path = Path(str(job["workspace_path"]))
        source = self._ensure_source(job)
        if not (path / ".git").exists():
            await asyncio.to_thread(
                self.workspaces.checkout_existing,
                source_path=source,
                repo=str(job["repo"]),
                branch=str(job["target_branch"]),
                path=path,
            )
        await asyncio.to_thread(
            self.workspaces.sync_to_remote_head,
            path=path,
            branch=str(job["target_branch"]),
            expected_sha=str(job["ci_head_sha"]),
        )
        implementation = dict(job["result_json"]) if isinstance(job.get("result_json"), dict) else {}
        plan = dict(job["plan_json"]) if isinstance(job.get("plan_json"), dict) else None
        async with self._semaphore:
            _, raw = await self.codex.run_turn(
                job_id=job_id,
                cwd=str(path),
                prompt=ci_repair_prompt(
                    dict(job["issue_context_json"]), plan, implementation, diagnostics, attempt
                ),
                output_schema=CODE_RESULT_SCHEMA,
                sandbox=Sandbox.workspace_write,
                effort=ReasoningEffort.medium,
                developer_instructions=DEVELOPER_INSTRUCTIONS,
                thread_id=str(job["codex_thread_id"]) if job.get("codex_thread_id") else None,
                timeout_seconds=CODE_TIMEOUT_SECONDS,
                on_progress=lambda event: self.reporter.activity(job_id, event),
                on_thread=lambda thread_id: self._store_thread(job_id, thread_id),
            )
        result = CodeResult.from_json(raw)
        if self._discarded(job_id):
            return
        files = await asyncio.to_thread(
            self.workspaces.validate_code_changes,
            path=path,
            plan_path=PLAN_PATH_TEMPLATE.format(job_id=job_id),
        )
        sha = await asyncio.to_thread(
            self.workspaces.commit_code,
            path=path,
            message=result.commit_message,
            first_push=False,
        )
        job = self._require_job(job_id)
        stored = dict(job["result_json"]) if isinstance(job.get("result_json"), dict) else {}
        all_files = sorted(
            {
                *(str(item) for item in stored.get("files") or []),
                *(str(item) for item in files),
            }
        )
        title = f"Fix #{job['issue_number']}: {job['issue_title']}"[:256]
        await asyncio.to_thread(
            self.github.update_pr,
            repo=str(job["repo"]),
            number=int(job["pull_request_number"]),
            title=title,
            body=_ready_pr_body(job, result, all_files, sha),
        )
        repairs = [dict(item) for item in stored.get("repairs") or [] if isinstance(item, dict)]
        repairs.append({**result.to_json(), "files": files, "commit_sha": sha})
        stored["repairs"] = repairs
        stored["files"] = all_files
        stored["commit_sha"] = sha
        if not self.db.update_code_job(
            job_id,
            {
                "status": "waiting_checks",
                "resume_phase": "checks",
                "latest_activity": f"Repair {attempt} pushed; waiting for new CI checks",
                "result_json": stored,
                "ci_head_sha": sha,
                "ci_wait_started_at": int(time.time()),
                "ci_repair_attempts": attempt,
                "ci_checks_json": [],
                "error": None,
            },
            allowed_statuses=("repairing_checks",),
        ):
            return
        self.db.audit("code.checks.repair", "ok", {"attempt": attempt, "sha": sha}, job_id)
        await self.reporter.refresh(job_id, force=True)

    async def _complete_checks(
        self, job_id: str, checks: tuple[PullRequestCheck, ...]
    ) -> None:
        job = self._require_job(job_id)
        stored = dict(job["result_json"]) if isinstance(job.get("result_json"), dict) else {}
        stored["ci"] = {
            "head_sha": str(job.get("ci_head_sha") or ""),
            "repair_attempts": int(job.get("ci_repair_attempts") or 0),
            "checks": [check.to_json() for check in checks],
        }
        if not self.db.update_code_job(
            job_id,
            {
                "status": "ready",
                "latest_activity": "All pull request checks passed",
                "result_json": stored,
                "ci_checks_json": [check.to_json() for check in checks],
                "error": None,
            },
            allowed_statuses=("waiting_checks",),
        ):
            return
        self.db.audit(
            "code.ready",
            "ok",
            {
                "pr": int(job["pull_request_number"]),
                "checks": len(checks),
                "repairs": int(job.get("ci_repair_attempts") or 0),
            },
            job_id,
        )
        await self._report_terminal(job_id)
        source = self._ensure_source(job)
        path = Path(str(job["workspace_path"]))
        try:
            await asyncio.to_thread(
                self.workspaces.cleanup,
                source_path=source,
                path=path,
                target_branch=str(job["target_branch"]),
            )
        except Exception:
            logging.exception("Failed to clean completed code workspace: %s", job_id)

    async def _fail(self, job_id: str, error: str, phase: str) -> None:
        safe_error = " ".join(error.split())[:1000]
        job = self.db.get_code_job(job_id)
        if not job or job["status"] in TERMINAL_STATUSES or job["status"] == "failed":
            return
        if not self.db.update_code_job(
            job_id,
            {
                "status": "failed",
                "resume_phase": phase,
                "latest_activity": f"{phase.title()} failed",
                "error": safe_error,
            },
            allowed_statuses=tuple(sorted(ACTIVE_STATUSES - {"failed"})),
        ):
            return
        self.db.audit("code.job", "failed", {"phase": phase, "error": safe_error}, job_id)
        if phase == "plan" and not job.get("pull_request_number"):
            try:
                await asyncio.to_thread(
                    self.github.discard,
                    repo=str(job["repo"]),
                    number=None,
                    branch=str(job["target_branch"]),
                )
            except Exception:
                logging.exception("Failed to clean an unpublished plan branch: %s", job_id)
        await self._report_terminal(job_id)

    async def _report_terminal(self, job_id: str) -> None:
        try:
            await self.reporter.refresh(job_id, force=True)
        except Exception:
            logging.exception("Failed to refresh terminal code job status: %s", job_id)
        try:
            await self.reporter.notify_terminal(job_id)
        except Exception:
            logging.exception("Failed to send terminal code job alert: %s", job_id)

    async def _store_thread(self, job_id: str, thread_id: str) -> None:
        self.db.update_code_job(job_id, {"codex_thread_id": thread_id})

    def _discarded(self, job_id: str) -> bool:
        job = self.db.get_code_job(job_id)
        return not job or job["status"] == "discarded"

    def _require_job(self, job_id: str) -> dict[str, Any]:
        job = self.db.get_code_job(job_id)
        if not job:
            raise ValueError("Code job not found.")
        return job

    def _ensure_source(self, job: dict[str, Any]) -> str:
        source = str(job.get("source_repo_path") or "")
        if not source:
            chat = self.db.get_chat_settings(int(job["telegram_chat_id"]))
            source = str(chat.get("local_repo_path") or "")
        resolved = self.workspaces.validate_source(source_path=source, repo=str(job["repo"]))
        if resolved != job.get("source_repo_path"):
            self.db.update_code_job(str(job["id"]), {"source_repo_path": resolved})
        return resolved


def _plan_pr_body(job: dict[str, Any], markdown: str) -> str:
    return "\n".join(
        [
            f"Draft implementation plan for [{job['repo']}#{job['issue_number']}]({job['issue_url']}).",
            "",
            markdown.strip(),
            "",
            f"Refs #{job['issue_number']}",
            "",
            f"<!-- telegram-code-job:{job['id']} -->",
        ]
    )


def _ready_pr_body(job: dict[str, Any], result: CodeResult, files: list[str], sha: str) -> str:
    lines = [
        f"Implements [{job['repo']}#{job['issue_number']}]({job['issue_url']}).",
        "",
        "## Summary",
        "",
        result.summary,
    ]
    if isinstance(job.get("plan_json"), dict):
        plan = CodePlan.from_json(dict(job["plan_json"]))
        lines.extend(["", "## Approved plan", "", plan.summary, ""])
        lines.extend(f"{index}. {step.title}" for index, step in enumerate(plan.steps, 1))
    lines.extend(["", "## Changed files", "", *(f"- `{path}`" for path in files)])
    lines.extend(["", "## Validation", ""])
    if result.tests:
        lines.extend(
            f"- `{item.command or 'not specified'}` — {item.status}: {item.summary}"
            for item in result.tests
        )
    else:
        lines.append("- No validation command was reported.")
    lines.extend(
        [
            "",
            f"Commit: `{sha}`",
            "",
            f"Closes #{job['issue_number']}",
            "",
            f"<!-- telegram-code-job:{job['id']} -->",
        ]
    )
    return "\n".join(lines)


def _check_failure_message(prefix: str, checks: tuple[PullRequestCheck, ...]) -> str:
    if not checks:
        return prefix
    details = ", ".join(
        f"{check.name} ({check.state or check.bucket})"
        + (f" {check.link}" if check.link else "")
        for check in checks
    )
    return f"{prefix}: {details}"
