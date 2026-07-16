from __future__ import annotations

import json

from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from telegram_project_manager.platform.storage.db import Database


DEFAULT_MEMORY_MAX_MESSAGES = 12


def memory_session_id(chat_id: int, thread_id: int | None = None) -> str:
    if thread_id is None:
        return f"commit-manager:{chat_id}"
    return f"commit-manager:{chat_id}:{thread_id}"


class SQLiteChatMessageHistory(BaseChatMessageHistory):
    def __init__(self, db: Database, session_id: str, max_messages: int = DEFAULT_MEMORY_MAX_MESSAGES) -> None:
        self.db = db
        self.session_id = session_id
        bounded_limit = max(2, max_messages)
        self.max_messages = bounded_limit if bounded_limit % 2 == 0 else bounded_limit - 1

    @property
    def messages(self) -> list[BaseMessage]:
        result: list[BaseMessage] = []
        for message in self.db.list_llm_messages(self.session_id, self.max_messages):
            if message["role"] == "human":
                result.append(HumanMessage(content=message["content"]))
            elif message["role"] == "ai":
                result.append(AIMessage(content=message["content"]))
        return result

    def add_messages(self, messages: list[BaseMessage]) -> None:
        stored: list[tuple[str, str]] = []
        for message in messages:
            role = (
                "human"
                if isinstance(message, HumanMessage)
                else "ai"
                if isinstance(message, AIMessage)
                else None
            )
            if role is None:
                continue
            content = (
                message.content
                if isinstance(message.content, str)
                else json.dumps(message.content, ensure_ascii=False, separators=(",", ":"))
            )
            stored.append((role, content))
        self.db.add_llm_messages(self.session_id, stored, self.max_messages)

    def clear(self) -> None:
        self.db.clear_llm_messages(self.session_id)
