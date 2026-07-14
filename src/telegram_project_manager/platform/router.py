from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from telegram_project_manager.platform.responses import OutgoingMessage
from telegram_project_manager.platform.storage.db import Database


@dataclass(frozen=True)
class IncomingAttachment:
    file_id: str
    file_unique_id: str
    mime_type: str
    file_size: int


@dataclass(frozen=True)
class IncomingMessage:
    chat_id: int
    user_id: int
    username: str
    text: str
    is_private: bool = False
    attachments: tuple[IncomingAttachment, ...] = ()
    message_id: int | None = None
    media_group_id: str | None = None
    thread_id: int | None = None
    reply_to_draft_id: str | None = None
    reply_to_issue_ref: str | None = None
    reply_to_code_job_id: str | None = None


class BotHandler(Protocol):
    async def handle(self, message: IncomingMessage) -> str | OutgoingMessage | None:
        ...


class TelegramRouter:
    def __init__(self, db: Database, handlers: list[BotHandler]) -> None:
        self.db = db
        self.handlers = handlers
        self.bot_username = ""

    def set_bot_username(self, username: str) -> None:
        self.bot_username = username.lstrip("@")

    async def handle_message(self, message: IncomingMessage) -> str | OutgoingMessage | None:
        if not self._should_handle(message):
            return None
        cleaned = self._strip_mention(message.text)
        message = IncomingMessage(
            chat_id=message.chat_id,
            user_id=message.user_id,
            username=message.username,
            text=cleaned,
            is_private=message.is_private,
            attachments=message.attachments,
            message_id=message.message_id,
            media_group_id=message.media_group_id,
            thread_id=message.thread_id,
            reply_to_draft_id=message.reply_to_draft_id,
            reply_to_issue_ref=message.reply_to_issue_ref,
            reply_to_code_job_id=message.reply_to_code_job_id,
        )
        for handler in self.handlers:
            response = await handler.handle(message)
            if response:
                return response
        return None

    def _should_handle(self, message: IncomingMessage) -> bool:
        text = message.text
        if message.is_private:
            return True
        if message.reply_to_draft_id or message.reply_to_issue_ref or message.reply_to_code_job_id:
            return True
        if text.startswith("/"):
            return True
        if text.lower().startswith("bot:"):
            return True
        return bool(self.bot_username and f"@{self.bot_username.lower()}" in text.lower())

    def _strip_mention(self, text: str) -> str:
        cleaned = text.strip()
        if cleaned.lower().startswith("bot:"):
            cleaned = cleaned[4:].strip()
        if self.bot_username:
            cleaned = cleaned.replace(f"@{self.bot_username}", "").replace(f"@{self.bot_username.lower()}", "")
        return cleaned.strip()
