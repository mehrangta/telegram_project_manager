from __future__ import annotations

from telegram_project_manager.bots.goal_manager.service import GoalService
from telegram_project_manager.integrations.git.local_repository import LocalRepositoryError
from telegram_project_manager.platform.permissions import PermissionService
from telegram_project_manager.platform.responses import OutgoingMessage, outgoing_message
from telegram_project_manager.platform.router import IncomingMessage
from telegram_project_manager.platform.storage.db import Database
from telegram_project_manager.platform.telegram_bot import TelegramBotApiError

GOAL_USAGE = (
    "Usage: /goal set <description> | /goal view | /goal edit <updated goal> | "
    "/goal pause | /goal resume | /goal clear"
)


class GoalManager:
    def __init__(self, *, db: Database, service: GoalService) -> None:
        self.db = db
        self.service = service
        self.permissions = PermissionService(db)

    async def handle(self, message: IncomingMessage) -> str | OutgoingMessage | None:
        command, _, rest = message.text.strip().partition(" ")
        if command.split("@", 1)[0].lower() != "/goal":
            return None
        admin_error = self.permissions.require_admin(message.user_id)
        if admin_error:
            return admin_error
        if message.attachments:
            return outgoing_message(
                "Goal commands do not accept attachments.\n" + GOAL_USAGE,
                reply_to_message_id=message.message_id,
            )
        action, _, tail = rest.strip().partition(" ")
        action = action.lower() or "view"
        tail = tail.strip()
        try:
            if action == "view":
                if tail:
                    raise ValueError(GOAL_USAGE)
                text = self.service.render_scope(
                    chat_id=message.chat_id, thread_id=message.thread_id
                )
            elif action == "set":
                if not tail:
                    raise ValueError("Usage: /goal set <description>")
                settings = self.db.get_scope_settings(message.chat_id, message.thread_id)
                repo = str(settings.get("active_repo") or "")
                if not repo:
                    scope = "topic" if message.thread_id is not None else "chat"
                    raise ValueError(
                        f"No active repo for this {scope}. Admin: /repo set owner/repository"
                    )
                if not self.db.is_repo_allowed(repo):
                    raise ValueError(
                        "Active repo is not in allowed repo list. Admin: /repo allow owner/repository"
                    )
                await self.service.submit(
                    chat_id=message.chat_id,
                    user_id=message.user_id,
                    thread_id=message.thread_id,
                    repo=repo,
                    branch=str(settings.get("default_branch") or "main"),
                    source_path=str(settings.get("local_repo_path") or ""),
                    objective=tail,
                )
                return None
            elif action == "edit":
                text = await self.service.edit(
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    objective=tail,
                )
            elif action == "pause":
                if tail:
                    raise ValueError("Usage: /goal pause")
                text = await self.service.pause(
                    chat_id=message.chat_id, thread_id=message.thread_id
                )
            elif action == "resume":
                if tail:
                    raise ValueError("Usage: /goal resume")
                text = await self.service.resume(
                    chat_id=message.chat_id, thread_id=message.thread_id
                )
            elif action == "clear":
                if tail:
                    raise ValueError("Usage: /goal clear")
                text = await self.service.clear(
                    chat_id=message.chat_id, thread_id=message.thread_id
                )
            else:
                raise ValueError(GOAL_USAGE)
        except (ValueError, LocalRepositoryError, TelegramBotApiError) as exc:
            return outgoing_message(str(exc), reply_to_message_id=message.message_id)
        return outgoing_message(text, reply_to_message_id=message.message_id)
