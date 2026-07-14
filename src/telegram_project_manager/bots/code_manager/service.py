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
    coding_prompt,
    plan_edit_prompt,
    planning_prompt,
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
    WorkspaceError,
)
from telegram_project_manager.integrations.gh.runner import GhError
from telegram_project_manager.platform.storage.db import Database


PLAN_PATH_TEMPLATE = ".codex/plans/{job_id}.md"
MAX_QUEUED_JOBS = 10
PLAN_TIMEOUT_SECONDS = 15 * 60
CODE_TIMEOUT_SECONDS = 45 * 60
TERMINAL_STATUSES = {"ready", "discarded"}
ACTIVE_STATUSES = {
    "queued_plan",
    "queued_plan_edit",
    "queued_code",
    "preparing",
    "planning",
    "editing_plan",
    "awaiting_approval",
    "coding",
    "validating",
    "pushing",
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
    ) -> None:
        self.db = db
        self.codex = codex
        self.workspaces = workspaces
        self.github = github
        self.reporter = reporter
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def recover(self) -> None:
        self.db.mark_running_code_jobs_interrupted()
        for job in self.db.list_code_jobs(limit=100):
            if job["status"] in {"queued_plan", "queued_plan_edit", "queued_code"}:
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
        status = "queued_code" if phase == "code" else (
            "queued_plan_edit" if job.get("pull_request_number") else "queued_plan"
        )
        if not self.db.update_code_job(
            job_id,
            {"status": status, "error": None, "latest_activity": "Retry queued"},
            allowed_statuses=("failed", "interrupted"),
        ):
            raise ValueError("Code job changed concurrently; retry again.")
        self.db.audit("code.retry", "ok", {"phase": phase}, job_id)
        await self.reporter.refresh(job_id, force=True)
        self._schedule(job_id, phase)

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
            return
        task = asyncio.create_task(self._run(job_id, phase), name=f"code-job-{job_id}-{phase}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job_id, None))

    async def _run(self, job_id: str, phase: str) -> None:
        async with self._semaphore:
            try:
                if phase == "plan":
                    await self._run_plan(job_id)
                else:
                    await self._run_code(job_id)
            except asyncio.CancelledError:
                raise
            except (CodexSdkError, CodeJobValidationError, WorkspaceError, GhError, ValueError) as exc:
                await self._fail(job_id, str(exc), phase)
            except Exception as exc:
                logging.exception("Unexpected code job failure: %s", job_id)
                await self._fail(job_id, f"Unexpected failure: {exc}", phase)

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
        self.db.update_code_job(
            job_id,
            {
                "status": "ready",
                "latest_activity": "Implementation ready for review",
                "pull_request_number": pr_number,
                "pull_request_url": pr_url,
                "result_json": {**result.to_json(), "files": files, "commit_sha": sha},
                "error": None,
            },
        )
        self.db.audit("code.ready", "ok", {"pr": pr_number, "files": len(files)}, job_id)
        await self.reporter.refresh(job_id, force=True)
        await asyncio.to_thread(
            self.workspaces.cleanup,
            source_path=source,
            path=path,
            target_branch=str(job["target_branch"]),
        )

    async def _fail(self, job_id: str, error: str, phase: str) -> None:
        safe_error = " ".join(error.split())[:1000]
        job = self.db.get_code_job(job_id)
        if not job or job["status"] == "discarded":
            return
        self.db.update_code_job(
            job_id,
            {
                "status": "failed",
                "resume_phase": phase,
                "latest_activity": f"{phase.title()} failed",
                "error": safe_error,
            },
        )
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
        try:
            await self.reporter.refresh(job_id, force=True)
        except Exception:
            logging.exception("Failed to report code job error: %s", job_id)

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
