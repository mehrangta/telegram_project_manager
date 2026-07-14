from __future__ import annotations

import asyncio
import re

from telegram_project_manager.bots.code_manager.progress import CodeProgressReporter
from telegram_project_manager.bots.code_manager.service import CodeJobService
from telegram_project_manager.bots.code_manager.workspace import CodeGitHubService, WorkspaceError
from telegram_project_manager.integrations.gh.runner import GhError
from telegram_project_manager.platform.permissions import PermissionService
from telegram_project_manager.platform.router import IncomingMessage
from telegram_project_manager.platform.storage.db import Database


ISSUE_URL_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(\d+)/?$"
)
REPO_ISSUE_RE = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)$")
NUMBER_RE = re.compile(r"^#?(\d+)$")
JOB_RE = re.compile(r"^c-[0-9a-f]{8}$")


class CodeManager:
    def __init__(
        self,
        *,
        db: Database,
        service: CodeJobService,
        github: CodeGitHubService,
        reporter: CodeProgressReporter,
    ) -> None:
        self.db = db
        self.permissions = PermissionService(db)
        self.service = service
        self.github = github
        self.reporter = reporter

    async def handle(self, message: IncomingMessage) -> str | None:
        text = message.text.strip()
        if message.reply_to_code_job_id and not text.startswith("/"):
            return await self._reply_control(message, message.reply_to_code_job_id, text)
        command, _, rest = text.partition(" ")
        if command.split("@", 1)[0].lower() != "/code":
            return None
        admin_error = self.permissions.require_admin(message.user_id)
        if admin_error:
            return admin_error
        rest = rest.strip()
        action, _, action_rest = rest.partition(" ")
        action = action.lower()
        if action in {"approve", "discard", "retry", "status", "edit"}:
            return await self._command_control(message, action, action_rest.strip())
        return await self._start(message, rest)

    async def _start(self, message: IncomingMessage, rest: str) -> str | None:
        tokens = rest.split()
        skip_plan = "--skip-plan" in tokens
        tokens = [item for item in tokens if item != "--skip-plan"]
        reference = " ".join(tokens).strip() or str(message.reply_to_issue_ref or "")
        chat = self.db.get_chat_settings(message.chat_id)
        active_repo = str(chat.get("active_repo") or "")
        try:
            repo, number = parse_issue_reference(reference, active_repo)
        except ValueError as exc:
            return str(exc)
        if not self.db.is_repo_allowed(repo):
            return "Issue repository is not allowed. Admin: /repo allow owner/repository"
        try:
            issue = await asyncio.to_thread(self.github.get_issue, repo, number)
            await self.service.create_job(
                chat_id=message.chat_id,
                user_id=message.user_id,
                thread_id=message.thread_id,
                issue=issue,
                base_branch=str(chat.get("default_branch") or "main"),
                source_path=str(chat.get("local_repo_path") or ""),
                skip_plan=skip_plan,
            )
        except (ValueError, WorkspaceError, GhError) as exc:
            return f"Code job not started.\nReason: {exc}"
        return None

    async def _reply_control(self, message: IncomingMessage, job_id: str, text: str) -> str | None:
        admin_error = self.permissions.require_admin(message.user_id)
        if admin_error:
            return admin_error
        action = text.strip().lower()
        if action in {"approve", "discard", "retry"}:
            return await self._perform_control(message, action, job_id, "")
        return await self._perform_control(message, "edit", job_id, text.strip())

    async def _command_control(self, message: IncomingMessage, action: str, rest: str) -> str | None:
        if action == "edit":
            job_id, _, feedback = rest.partition(" ")
            return await self._perform_control(message, action, job_id, feedback.strip())
        job_id = rest.strip()
        if action == "status" and not job_id:
            jobs = self.db.list_code_jobs(chat_id=message.chat_id, limit=10)
            if not jobs:
                return "No code jobs for this chat."
            return "Recent code jobs:\n" + "\n".join(
                f"- {job['id']} {job['repo']}#{job['issue_number']} — {job['status']}"
                for job in jobs
            )
        return await self._perform_control(message, action, job_id, "")

    async def _perform_control(
        self,
        message: IncomingMessage,
        action: str,
        job_id: str,
        feedback: str,
    ) -> str | None:
        if not JOB_RE.fullmatch(job_id):
            return f"Usage: /code {action} c-12345678" + (" <feedback>" if action == "edit" else "")
        job = self.db.get_code_job(job_id)
        if not job:
            return "Code job not found."
        if int(job["telegram_user_id"]) != message.user_id and not self.permissions.is_admin(message.user_id):
            return "Only the requester or an admin can control this code job."
        if int(job["telegram_chat_id"]) != message.chat_id:
            return "Code job belongs to a different chat."
        try:
            if action == "approve":
                await self.service.approve(job_id)
            elif action == "edit":
                await self.service.edit_plan(job_id, feedback)
            elif action == "discard":
                await self.service.discard(job_id)
            elif action == "retry":
                await self.service.retry(job_id)
            elif action == "status":
                return self.reporter.render(job)
            else:
                return "Unknown /code action."
        except (ValueError, WorkspaceError, GhError) as exc:
            return f"Code job not updated.\nReason: {exc}"
        self.db.audit(f"code.{action}", "ok", {"actor": message.user_id}, job_id)
        return None


def parse_issue_reference(reference: str, active_repo: str) -> tuple[str, int]:
    value = reference.strip()
    if not value:
        raise ValueError(
            "Usage: /code #123 [--skip-plan], an issue URL, or reply /code to an Issue created message."
        )
    url = ISSUE_URL_RE.fullmatch(value)
    if url:
        return url.group(1), int(url.group(2))
    repo_issue = REPO_ISSUE_RE.fullmatch(value)
    if repo_issue:
        return repo_issue.group(1), int(repo_issue.group(2))
    number = NUMBER_RE.fullmatch(value)
    if number:
        if not active_repo:
            raise ValueError("No active repo for this chat. Admin: /repo set owner/repository")
        return active_repo, int(number.group(1))
    raise ValueError("Issue must be #123, owner/repo#123, or a GitHub issue URL.")
