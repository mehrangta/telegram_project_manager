from __future__ import annotations

from telegram_project_manager.bots.ask_manager.service import AskService
from telegram_project_manager.bots.code_manager.workspace import WorkspaceError
from telegram_project_manager.platform.permissions import PermissionService
from telegram_project_manager.platform.responses import OutgoingMessage, outgoing_message
from telegram_project_manager.platform.router import IncomingMessage
from telegram_project_manager.platform.storage.db import Database
from telegram_project_manager.platform.telegram_bot import TelegramBotApiError


class AskManager:
    def __init__(self, *, db: Database, service: AskService) -> None:
        self.db = db
        self.service = service
        self.permissions = PermissionService(db)

    async def handle(self, message: IncomingMessage) -> str | OutgoingMessage | None:
        command, _, rest = message.text.strip().partition(" ")
        if command.split("@", 1)[0].lower() != "/ask":
            return None
        admin_error = self.permissions.require_admin(message.user_id)
        if admin_error:
            return admin_error
        question = rest.strip()
        if not question:
            return outgoing_message(
                "Usage: /ask <question about the active repository>",
                reply_to_message_id=message.message_id,
            )
        settings = self.db.get_scope_settings(message.chat_id, message.thread_id)
        repo = str(settings.get("active_repo") or "")
        if not repo:
            scope = "topic" if message.thread_id is not None else "chat"
            return outgoing_message(
                f"No active repo for this {scope}. Admin: /repo set owner/repository",
                reply_to_message_id=message.message_id,
            )
        if not self.db.is_repo_allowed(repo):
            return outgoing_message(
                "Active repo is not in allowed repo list. Admin: /repo allow owner/repository",
                reply_to_message_id=message.message_id,
            )
        try:
            await self.service.submit(
                chat_id=message.chat_id,
                user_id=message.user_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
                repo=repo,
                branch=str(settings.get("default_branch") or "main"),
                source_path=str(settings.get("local_repo_path") or ""),
                question=question,
            )
        except (ValueError, WorkspaceError) as exc:
            return outgoing_message(
                f"Repository question not started.\nReason: {exc}",
                reply_to_message_id=message.message_id,
            )
        except TelegramBotApiError:
            raise
        return None
