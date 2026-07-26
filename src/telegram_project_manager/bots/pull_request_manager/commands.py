from __future__ import annotations

import re

from telegram_project_manager.bots.pull_request_manager.pull_request_list import (
    PullRequestListError,
    PullRequestListService,
)
from telegram_project_manager.bots.pull_request_manager.service import (
    DeploymentError,
    MergeDeploymentService,
)
from telegram_project_manager.platform.permissions import PermissionService
from telegram_project_manager.platform.router import IncomingMessage
from telegram_project_manager.platform.storage.db import Database


JOB_RE = re.compile(r"c-[0-9a-f]{8}")


class PullRequestManager:
    def __init__(
        self,
        *,
        db: Database,
        service: MergeDeploymentService,
        pull_request_lists: PullRequestListService,
    ) -> None:
        self.db = db
        self.permissions = PermissionService(db)
        self.service = service
        self.pull_request_lists = pull_request_lists

    async def handle(self, message: IncomingMessage) -> str | None:
        command, _, rest = message.text.strip().partition(" ")
        command = command.split("@", 1)[0].lower()
        if command not in {"/prs", "/merge", "/deploy"}:
            return None
        admin_error = self.permissions.require_admin(message.user_id)
        if admin_error:
            return admin_error
        if command == "/prs":
            return await self._pull_requests(message, rest.strip())
        job_id = rest.strip().lower() or str(message.reply_to_code_job_id or "").lower()
        if not JOB_RE.fullmatch(job_id):
            return (
                f"Usage: {command} c-12345678, or reply {command} "
                "to a code-job message."
            )
        job = self.db.get_code_job(job_id)
        if not job:
            return "Code job not found."
        if int(job["telegram_chat_id"]) != message.chat_id:
            return "Code job belongs to a different chat."
        if job.get("telegram_thread_id") != message.thread_id:
            return "Code job belongs to a different topic."
        try:
            if command == "/merge":
                return await self.service.start_merge(job_id)
            return await self.service.start_deploy(job_id)
        except DeploymentError as exc:
            action = command.lstrip("/")
            self.db.audit(f"{action}.queue", "failed", {"error": str(exc)}, job_id)
            heading = "Merge" if action == "merge" else "Merge and deployment"
            return f"{heading} not started.\nReason: {exc}"

    async def _pull_requests(self, message: IncomingMessage, rest: str) -> str | None:
        if rest:
            return "Usage: /prs"
        settings = self.db.get_scope_settings(message.chat_id, message.thread_id)
        repo = str(settings.get("active_repo") or "")
        if not repo:
            return (
                f"No active repo for this {_scope_name(message)}. "
                "Admin: /repo set owner/repository"
            )
        if not self.db.is_repo_allowed(repo):
            return "Active repo is not in allowed repo list. Admin: /repo allow owner/repository"
        try:
            await self.pull_request_lists.publish(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                repo=repo,
            )
        except PullRequestListError as exc:
            self.db.audit("prs.list", "failed", {"repo": repo, "error": str(exc)})
            return f"Pull requests not loaded.\nReason: {exc}"
        return None


def _scope_name(message: IncomingMessage) -> str:
    return "topic" if message.thread_id is not None else "chat"
