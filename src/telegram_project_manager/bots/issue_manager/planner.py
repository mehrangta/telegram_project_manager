from __future__ import annotations

import time
import uuid

from telegram_project_manager.bots.issue_manager.prompts import SYSTEM_PROMPT, build_user_prompt
from telegram_project_manager.bots.issue_manager.schemas import ISSUE_DRAFT_RESPONSE_SCHEMA, IssueDraft
from telegram_project_manager.platform.llm.client import OpenAICompatibleClient
from telegram_project_manager.platform.router import IncomingAttachment
from telegram_project_manager.platform.storage.db import Database


def issue_memory_session_id(chat_id: int) -> str:
    return f"issue-manager:{chat_id}"


class IssuePlanner:
    def __init__(self, db: Database, llm: OpenAICompatibleClient) -> None:
        self.db = db
        self.llm = llm

    def create_draft(
        self,
        *,
        request_text: str,
        chat_id: int,
        user_id: int,
        repo: str,
        default_branch: str,
        attachments: tuple[IncomingAttachment, ...],
    ) -> tuple[str, IssueDraft]:
        raw = self.llm.chat_json(
            SYSTEM_PROMPT,
            build_user_prompt(request_text, repo),
            memory_key=issue_memory_session_id(chat_id),
            response_schema=ISSUE_DRAFT_RESPONSE_SCHEMA,
        )
        issue = IssueDraft.from_llm(raw)
        draft_id = f"i-{uuid.uuid4().hex[:8]}"
        now = int(time.time())
        self.db.create_issue_draft(
            {
                "id": draft_id,
                "telegram_chat_id": chat_id,
                "telegram_user_id": user_id,
                "repo": repo,
                "default_branch": default_branch,
                "request_text": request_text,
                "issue_json": issue.to_json(),
                "status": "pending",
                "created_at": now,
                "expires_at": now + 3600,
            },
            [
                {
                    "position": position,
                    "telegram_file_id": item.file_id,
                    "telegram_file_unique_id": item.file_unique_id,
                    "mime_type": item.mime_type,
                    "file_size": item.file_size,
                }
                for position, item in enumerate(attachments)
            ],
        )
        self.db.audit("issue.plan", "ok", {"repo": repo, "images": len(attachments)}, draft_id)
        return draft_id, issue
