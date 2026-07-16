from __future__ import annotations

import time
import uuid

from telegram_project_manager.bots.issue_manager.prompts import (
    SYSTEM_PROMPT,
    TITLE_SYSTEM_PROMPT,
    build_revision_prompt,
    build_title_prompt,
    build_title_revision_prompt,
    build_user_prompt,
)
from telegram_project_manager.bots.issue_manager.schemas import (
    BODY_MODE_ORIGINAL,
    ISSUE_DRAFT_RESPONSE_SCHEMA,
    ISSUE_TITLE_RESPONSE_SCHEMA,
    IssueDraft,
)
from telegram_project_manager.integrations.gh.repository_context import RepositoryContextService
from telegram_project_manager.platform.llm.client import OpenAICompatibleClient
from telegram_project_manager.platform.router import IncomingAttachment
from telegram_project_manager.platform.storage.db import Database


def issue_memory_session_id(chat_id: int, thread_id: int | None, repo: str) -> str:
    if thread_id is None:
        return f"issue-manager:{chat_id}:{repo}"
    return f"issue-manager:{chat_id}:{thread_id}:{repo}"


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
        local_repo_path: str,
        attachments: tuple[IncomingAttachment, ...],
        thread_id: int | None = None,
    ) -> tuple[str, IssueDraft]:
        if self.db.get_setting("issue_body_llm_enabled", "true") == "false":
            raw = self.llm.chat_json(
                TITLE_SYSTEM_PROMPT,
                build_title_prompt(request_text, repo),
                response_schema=ISSUE_TITLE_RESPONSE_SCHEMA,
            )
            issue = IssueDraft(
                title=str(raw.get("title") or "").strip(),
                summary="",
                actual_behavior="",
                expected_behavior="",
                body_mode=BODY_MODE_ORIGINAL,
                raw_body=request_text,
            )
            issue.validate()
            context_commit_sha = ""
            context_paths: list[str] = []
        else:
            context = self.repository_context.collect(
                repo=repo,
                branch=default_branch,
                request_text=request_text,
                source_path=local_repo_path,
            )
            raw = self.llm.chat_json(
                SYSTEM_PROMPT,
                build_user_prompt(request_text, repo, context.to_prompt()),
                memory_key=issue_memory_session_id(chat_id, thread_id, repo),
                response_schema=ISSUE_DRAFT_RESPONSE_SCHEMA,
            )
            issue = IssueDraft.from_llm(
                raw,
                context_branch=context.branch,
                context_commit_sha=context.commit_sha,
                allowed_paths=context.paths,
            )
            context_commit_sha = context.commit_sha
            context_paths = sorted(context.paths)
        draft_id = f"i-{uuid.uuid4().hex[:8]}"
        now = int(time.time())
        self.db.create_issue_draft(
            {
                "id": draft_id,
                "telegram_chat_id": chat_id,
                "telegram_thread_id": thread_id,
                "telegram_user_id": user_id,
                "repo": repo,
                "default_branch": default_branch,
                "local_repo_path": local_repo_path,
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
                "body_mode": issue.body_mode,
                "context_commit_sha": context_commit_sha,
                "context_paths": context_paths,
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
        local_repo_path: str,
    ) -> IssueDraft:
        current_issue = IssueDraft.from_llm(dict(record["issue_json"]))
        if current_issue.body_mode == BODY_MODE_ORIGINAL:
            raw = self.llm.chat_json(
                TITLE_SYSTEM_PROMPT,
                build_title_revision_prompt(
                    repo=str(record["repo"]),
                    raw_body=current_issue.raw_body,
                    current_title=current_issue.title,
                    feedback_history=feedback_history,
                    new_feedback=new_feedback,
                ),
                response_schema=ISSUE_TITLE_RESPONSE_SCHEMA,
            )
            revised = IssueDraft(
                title=str(raw.get("title") or "").strip(),
                summary="",
                actual_behavior="",
                expected_behavior="",
                body_mode=BODY_MODE_ORIGINAL,
                raw_body=current_issue.raw_body,
            )
            revised.validate()
            return revised
        search_text = "\n".join(
            [str(record["request_text"]), *feedback_history, new_feedback]
        )
        context = self.repository_context.collect(
            repo=str(record["repo"]),
            branch=str(record["default_branch"]),
            request_text=search_text,
            source_path=local_repo_path,
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
