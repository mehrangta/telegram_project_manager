from __future__ import annotations

import re

from telegram_project_manager.bots.pull_request_manager.service import (
    DeploymentError,
    MergeDeploymentService,
)
from telegram_project_manager.platform.permissions import PermissionService
from telegram_project_manager.platform.router import IncomingMessage
from telegram_project_manager.platform.storage.db import Database


JOB_RE = re.compile(r"c-[0-9a-f]{8}")


class PullRequestManager:
    def __init__(self, *, db: Database, service: MergeDeploymentService) -> None:
        self.db = db
        self.permissions = PermissionService(db)
        self.service = service

    async def handle(self, message: IncomingMessage) -> str | None:
        command, _, rest = message.text.strip().partition(" ")
        if command.split("@", 1)[0].lower() != "/deploy":
            return None
        admin_error = self.permissions.require_admin(message.user_id)
        if admin_error:
            return admin_error
        job_id = rest.strip().lower() or str(message.reply_to_code_job_id or "").lower()
        if not JOB_RE.fullmatch(job_id):
            return "Usage: /deploy c-12345678, or reply /deploy to a code-job message."
        job = self.db.get_code_job(job_id)
        if not job:
            return "Code job not found."
        if int(job["telegram_chat_id"]) != message.chat_id:
            return "Code job belongs to a different chat."
        try:
            return await self.service.start(job_id)
        except DeploymentError as exc:
            self.db.audit("deploy.queue", "failed", {"error": str(exc)}, job_id)
            return f"Merge and deployment not started.\nReason: {exc}"
