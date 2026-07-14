from __future__ import annotations

import time
import uuid

from telegram_project_manager.bots.issue_manager.prompts import (
    SYSTEM_PROMPT,
    build_revision_prompt,
    build_user_prompt,
)
from telegram_project_manager.bots.issue_manager.schemas import ISSUE_DRAFT_RESPONSE_SCHEMA, IssueDraft
from telegram_project_manager.integrations.gh.repository_context import RepositoryContextService
from telegram_project_manager.platform.llm.client import OpenAICompatibleClient
from telegram_project_manager.platform.router import IncomingAttachment
from telegram_project_manager.platform.storage.db import Database


def issue_memory_session_id(chat_id: int, repo: str) -> str:
    return f"issue-manager:{chat_id}:{repo}"


class IssuePlanner:
    def __init__(
        self,
        db: Database,
        llm: OpenAICompatibleClient,
        repository_context: RepositoryContextService,
    ) -> None:
        self.db = db
        self.llm = llm
        self.repository_context = repository_context

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
        context = self.repository_context.collect(
            repo=repo, branch=default_branch, request_text=request_text
        )
        raw = self.llm.chat_json(
            SYSTEM_PROMPT,
            build_user_prompt(request_text, repo, context.to_prompt()),
            memory_key=issue_memory_session_id(chat_id, repo),
            response_schema=ISSUE_DRAFT_RESPONSE_SCHEMA,
        )
        issue = IssueDraft.from_llm(
            raw,
            context_branch=context.branch,
            context_commit_sha=context.commit_sha,
            allowed_paths=context.paths,
        )
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
        self.db.audit(
            "issue.plan",
            "ok",
            {
                "repo": repo,
                "images": len(attachments),
                "context_commit_sha": context.commit_sha,
                "context_paths": sorted(context.paths),
            },
            draft_id,
        )
        return draft_id, issue

    def revise_draft(
        self,
        *,
        record: dict,
        feedback_history: list[str],
        new_feedback: str,
    ) -> IssueDraft:
        search_text = "\n".join(
            [str(record["request_text"]), *feedback_history, new_feedback]
        )
        context = self.repository_context.collect(
            repo=str(record["repo"]),
            branch=str(record["default_branch"]),
            request_text=search_text,
        )
        raw = self.llm.chat_json(
            SYSTEM_PROMPT,
            build_revision_prompt(
                original_request=str(record["request_text"]),
                current_issue=dict(record["issue_json"]),
                feedback_history=feedback_history,
                new_feedback=new_feedback,
                repo=str(record["repo"]),
                repository_context=context.to_prompt(),
            ),
            response_schema=ISSUE_DRAFT_RESPONSE_SCHEMA,
        )
        return IssueDraft.from_llm(
            raw,
            context_branch=context.branch,
            context_commit_sha=context.commit_sha,
            allowed_paths=context.paths,
        )
