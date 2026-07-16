from __future__ import annotations

from telegram_project_manager.bots.ask_manager.images import validate_attachments
from telegram_project_manager.bots.do_manager.service import DoService
from telegram_project_manager.integrations.git.local_repository import LocalRepositoryError
from telegram_project_manager.platform.permissions import PermissionService
from telegram_project_manager.platform.responses import OutgoingMessage, outgoing_message
from telegram_project_manager.platform.router import IncomingMessage
from telegram_project_manager.platform.storage.db import Database


class DoManager:
    def __init__(self, *, db: Database, service: DoService) -> None:
        self.db = db
        self.permissions = PermissionService(db)
        self.service = service

    async def handle(self, message: IncomingMessage) -> str | OutgoingMessage | None:
        command, _, rest = message.text.strip().partition(" ")
        if command.split("@", 1)[0].lower() != "/do":
            return None
        admin_error = self.permissions.require_admin(message.user_id)
        if admin_error:
            return admin_error
        rest = rest.strip()
        action, _, tail = rest.partition(" ")
        if action.lower() == "status":
            try:
                text = self.service.status(
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    job_id=tail.strip(),
                )
            except ValueError as exc:
                return outgoing_message(str(exc), reply_to_message_id=message.message_id)
            return outgoing_message(text, reply_to_message_id=message.message_id)
        host_mode = action.lower() == "--host"
        job = tail.strip() if host_mode else rest
        if not job:
            return outgoing_message(
                "Usage: /do <job> [images] | /do --host <job> | /do status [d-job-id]",
                reply_to_message_id=message.message_id,
            )
        if host_mode and not message.is_private:
            return outgoing_message(
                "Host-wide /do jobs are available only in private admin chats.",
                reply_to_message_id=message.message_id,
            )
        try:
            validate_attachments(message.attachments)
            if host_mode:
                await self.service.submit(
                    chat_id=message.chat_id,
                    user_id=message.user_id,
                    thread_id=message.thread_id,
                    message_id=message.message_id,
                    mode="host",
                    job=job,
                    attachments=message.attachments,
                )
                return None
            settings = self.db.get_scope_settings(message.chat_id, message.thread_id)
            repo = str(settings.get("active_repo") or "")
            if not repo:
                scope = "topic" if message.thread_id is not None else "chat"
                raise ValueError(f"No active repo for this {scope}. Admin: /repo set owner/repository")
            if not self.db.is_repo_allowed(repo):
                raise ValueError("Active repo is not in allowed repo list. Admin: /repo allow owner/repository")
            await self.service.submit(
                chat_id=message.chat_id,
                user_id=message.user_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
                mode="repo",
                repo=repo,
                branch=str(settings.get("default_branch") or "main"),
                source_path=str(settings.get("local_repo_path") or ""),
                job=job,
                attachments=message.attachments,
            )
        except (ValueError, LocalRepositoryError) as exc:
            return outgoing_message(
                f"Full-access job not started.\nReason: {exc}",
                reply_to_message_id=message.message_id,
            )
        return None
