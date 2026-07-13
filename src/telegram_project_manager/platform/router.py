from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from telegram_project_manager.platform.storage.db import Database


@dataclass(frozen=True)
class IncomingMessage:
    chat_id: int
    user_id: int
    username: str
    text: str
    is_private: bool = False


class BotHandler(Protocol):
    async def handle(self, message: IncomingMessage) -> str | None:
        ...


class TelegramRouter:
    def __init__(self, db: Database, handlers: list[BotHandler]) -> None:
        self.db = db
        self.handlers = handlers
        self.bot_username = ""

    def set_bot_username(self, username: str) -> None:
        self.bot_username = username.lstrip("@")

    async def handle_message(self, message: IncomingMessage) -> str | None:
        if not self._should_handle(message.text, message.is_private):
            return None
        cleaned = self._strip_mention(message.text)
        message = IncomingMessage(
            chat_id=message.chat_id,
            user_id=message.user_id,
            username=message.username,
            text=cleaned,
            is_private=message.is_private,
        )
        for handler in self.handlers:
            response = await handler.handle(message)
            if response:
                return response
        return None

    def _should_handle(self, text: str, is_private: bool) -> bool:
        if is_private:
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
