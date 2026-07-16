from __future__ import annotations

from telegram_project_manager.bots.do_manager.service import DoService
from telegram_project_manager.platform.permissions import PermissionService
from telegram_project_manager.platform.responses import OutgoingMessage, outgoing_message
from telegram_project_manager.platform.router import IncomingMessage
from telegram_project_manager.platform.storage.db import Database


class DoManager:
    def __init__(self, *, db: Database, service: DoService) -> None:
        self.permissions = PermissionService(db)
        self.service = service

    async def handle(self, message: IncomingMessage) -> str | OutgoingMessage | None:
        command, _, rest = message.text.strip().partition(" ")
        if command.split("@", 1)[0].lower() != "/do":
            return None
        admin_error = self.permissions.require_admin(message.user_id)
        if admin_error:
            return admin_error
        if not message.is_private:
            return outgoing_message(
                "The /do command is available only in private admin chats.",
                reply_to_message_id=message.message_id,
            )
        job = rest.strip()
        if not job:
            return outgoing_message(
                "Usage: /do <job>",
                reply_to_message_id=message.message_id,
            )
        try:
            await self.service.submit(
                chat_id=message.chat_id,
                user_id=message.user_id,
                message_id=message.message_id,
                job=job,
            )
        except ValueError as exc:
            return outgoing_message(
                f"Full-access job not started.\nReason: {exc}",
                reply_to_message_id=message.message_id,
            )
        return None
