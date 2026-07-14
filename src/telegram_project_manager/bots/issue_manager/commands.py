from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from telegram_project_manager.bots.issue_manager.executor import IssueExecutionService
from telegram_project_manager.bots.issue_manager.planner import IssuePlanner
from telegram_project_manager.bots.issue_manager.schemas import (
    BODY_MODE_ORIGINAL,
    IssueDraft,
    IssueDraftValidationError,
)
from telegram_project_manager.integrations.gh.runner import GhError
from telegram_project_manager.integrations.gh.repository_context import RepositoryContextError
from telegram_project_manager.platform.llm.client import LlmError
from telegram_project_manager.platform.permissions import PermissionService
from telegram_project_manager.platform.responses import truncate
from telegram_project_manager.platform.router import IncomingAttachment, IncomingMessage
from telegram_project_manager.platform.storage.db import Database
from telegram_project_manager.platform.telegram_bot import TelegramBotApiError


SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif"}
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 10_000_000
MAX_TOTAL_IMAGE_BYTES = 20_000_000


class IssueManager:
    def __init__(self, db: Database, planner: IssuePlanner, execution: IssueExecutionService) -> None:
        self.db = db
        self.permissions = PermissionService(db)
        self.planner = planner
        self.execution = execution

    async def handle(self, message: IncomingMessage) -> str | None:
        command, _, rest = message.text.strip().partition(" ")
        command = command.split("@", 1)[0].lower()
        if command == "/issue":
            return self.create(message, rest.strip())
        if command == "/edit":
            draft_id, _, feedback = rest.strip().partition(" ")
            if not draft_id.startswith("i-"):
                return "Usage: /edit i-12345678 <feedback> (images optional)"
            return self.revise(message, draft_id, feedback.strip())
        if command == "/confirm" and rest.strip().startswith("i-"):
            return self.confirm(message, rest.strip())
        if command == "/cancel" and rest.strip().startswith("i-"):
            return self.cancel(message, rest.strip())
        if command.startswith("/"):
            return None
        if message.reply_to_draft_id:
            return self.revise(message, message.reply_to_draft_id, message.text.strip())
        return None

    def create(self, message: IncomingMessage, request_text: str) -> str:
        admin_error = self.permissions.require_admin(message.user_id)
        if admin_error:
            return admin_error
        if not request_text:
            return "Usage: /issue <prompt> (text or photo/album caption)"
        attachment_error = self._validate_attachments(message)
        if attachment_error:
            return attachment_error
        chat = self.db.get_chat_settings(message.chat_id)
        repo = str(chat.get("active_repo") or "")
        if not repo:
            return "No active repo for this chat. Admin: /repo set owner/repository"
        if not self.db.is_repo_allowed(repo):
            return "Active repo is not in allowed repo list. Admin: /repo allow owner/repository"
        default_branch = str(chat.get("default_branch") or "main")
        try:
            draft_id, issue = self.planner.create_draft(
                request_text=request_text,
                chat_id=message.chat_id,
                user_id=message.user_id,
                repo=repo,
                default_branch=default_branch,
                local_repo_path=str(chat.get("local_repo_path") or ""),
                attachments=message.attachments,
            )
        except RepositoryContextError as exc:
            self.db.audit("issue.context", "failed", {"repo": repo, "error": str(exc)})
            return f"Issue draft not created.\nReason: {exc}"
        except (LlmError, IssueDraftValidationError, ValueError) as exc:
            self.db.audit("issue.plan", "failed", {"repo": repo, "error": str(exc)})
            return f"Issue draft not created.\nReason: {exc}"
        return self._format_preview(
            heading="Issue draft created.",
            draft_id=draft_id,
            repo=repo,
            issue=issue,
            revision=1,
            image_count=len(message.attachments),
        )

    def revise(self, message: IncomingMessage, draft_id: str, feedback_text: str) -> str:
        admin_error = self.permissions.require_admin(message.user_id)
        if admin_error:
            return admin_error
        record = self.db.get_issue_draft(draft_id)
        record_error = self._validate_editable_record(record, message)
        if record_error:
            if record and record_error.startswith("Issue draft expired"):
                self.db.update_issue_draft_status(draft_id, "expired")
            return record_error
        assert record is not None

        existing_unique_ids = {
            str(item["telegram_file_unique_id"]) for item in record["attachments"]
        }
        unique_attachments = []
        for item in message.attachments:
            if item.file_unique_id in existing_unique_ids:
                continue
            existing_unique_ids.add(item.file_unique_id)
            unique_attachments.append(item)
        new_attachments = tuple(unique_attachments)
        attachment_error = self._validate_combined_attachments(
            record["attachments"], new_attachments
        )
        if attachment_error:
            return attachment_error
        if not feedback_text and not new_attachments:
            return "No changes supplied. Add feedback or a new image."

        try:
            if feedback_text:
                revisions = self.db.get_issue_draft_revisions(draft_id)
                feedback_history = [
                    str(item["feedback_text"])
                    for item in revisions
                    if str(item["feedback_text"]).strip()
                ]
                issue = self.planner.revise_draft(
                    record=record,
                    feedback_history=feedback_history,
                    new_feedback=feedback_text,
                    local_repo_path=str(
                        self.db.get_chat_settings(message.chat_id).get("local_repo_path") or ""
                    ),
                )
            else:
                issue = IssueDraft.from_llm(dict(record["issue_json"]))
            revision = self.db.revise_issue_draft(
                draft_id=draft_id,
                telegram_chat_id=message.chat_id,
                telegram_user_id=message.user_id,
                feedback_text=feedback_text,
                issue_json=issue.to_json(),
                attachments=[
                    {
                        "telegram_file_id": item.file_id,
                        "telegram_file_unique_id": item.file_unique_id,
                        "mime_type": item.mime_type,
                        "file_size": item.file_size,
                    }
                    for item in new_attachments
                ],
                expires_at=int(time.time()) + 3600,
            )
        except RepositoryContextError as exc:
            self.db.audit("issue.revise", "failed", {"error": str(exc)}, draft_id)
            return f"Issue draft not revised.\nReason: {exc}"
        except (LlmError, IssueDraftValidationError, ValueError) as exc:
            self.db.audit("issue.revise", "failed", {"error": str(exc)}, draft_id)
            return f"Issue draft not revised.\nReason: {exc}"

        image_count = len(record["attachments"]) + len(new_attachments)
        self.db.audit(
            "issue.revise",
            "ok",
            {
                "revision": revision,
                "feedback": bool(feedback_text),
                "images_added": len(new_attachments),
            },
            draft_id,
        )
        return self._format_preview(
            heading="Issue draft revised.",
            draft_id=draft_id,
            repo=str(record["repo"]),
            issue=issue,
            revision=revision,
            image_count=image_count,
        )

    def confirm(self, message: IncomingMessage, draft_id: str) -> str:
        admin_error = self.permissions.require_admin(message.user_id)
        if admin_error:
            return admin_error
        try:
            result = self.execution.execute(draft_id, message.user_id)
        except (ValueError, GhError, TelegramBotApiError) as exc:
            self.db.audit("issue.create", "failed", {"error": str(exc)}, draft_id)
            return f"Issue not created.\nReason: {exc}"
        return "\n".join(
            [
                "Issue created.",
                f"Repo: {result.repo}",
                f"Issue: #{result.number}",
                f"Title: {result.title}",
                f"Link: {result.url}",
            ]
        )

    def cancel(self, message: IncomingMessage, draft_id: str) -> str:
        admin_error = self.permissions.require_admin(message.user_id)
        if admin_error:
            return admin_error
        record = self.db.get_issue_draft(draft_id)
        if not record:
            return "Issue draft not found."
        if record["status"] != "pending":
            return f"Issue draft is not pending. Current status: {record['status']}"
        self.db.update_issue_draft_status(draft_id, "cancelled")
        self.db.audit("issue.cancel", "ok", {}, draft_id)
        return f"Issue draft cancelled: {draft_id}"

    @staticmethod
    def _validate_attachments(message: IncomingMessage) -> str | None:
        return IssueManager._validate_combined_attachments((), message.attachments)

    @staticmethod
    def _validate_combined_attachments(
        existing: Sequence[dict[str, Any]],
        new: Sequence[IncomingAttachment],
    ) -> str | None:
        if len(existing) + len(new) > MAX_IMAGES:
            return f"Too many images. Maximum: {MAX_IMAGES}."
        total = sum(int(item["file_size"]) for item in existing)
        for attachment in new:
            if attachment.mime_type not in SUPPORTED_IMAGE_TYPES:
                return f"Unsupported image type: {attachment.mime_type}."
            if attachment.file_size > MAX_IMAGE_BYTES:
                return "Each image must be 10 MB or smaller."
            total += attachment.file_size
        if total > MAX_TOTAL_IMAGE_BYTES:
            return "Issue images must be 20 MB or smaller in total."
        return None

    @staticmethod
    def _validate_editable_record(record: dict | None, message: IncomingMessage) -> str | None:
        if not record:
            return "Issue draft not found."
        if int(record["telegram_chat_id"]) != message.chat_id:
            return "Issue draft belongs to a different chat."
        if int(record["telegram_user_id"]) != message.user_id:
            return "Only the original author can edit this issue draft."
        if record["status"] != "pending":
            return f"Issue draft is not pending. Current status: {record['status']}"
        if int(record["expires_at"]) < int(time.time()):
            return "Issue draft expired. Create a new draft with /issue."
        return None

    @staticmethod
    def _format_preview(
        *,
        heading: str,
        draft_id: str,
        repo: str,
        issue: IssueDraft,
        revision: int,
        image_count: int,
    ) -> str:
        lines = [
            heading,
            f"Draft ID: {draft_id}",
            f"Revision: {revision}",
            f"Repo: {repo}",
            f"Title: {issue.title}",
        ]
        if issue.body_mode == BODY_MODE_ORIGINAL:
            lines.extend(["Body mode: original prompt", f"Body: {issue.raw_body}"])
        else:
            lines.extend(
                [
                    f"Summary: {issue.summary}",
                    f"Actual behavior: {issue.actual_behavior}",
                    f"Expected behavior: {issue.expected_behavior}",
                    f"Codebase context: {issue.codebase_context}",
                    f"Relevant files: {len(issue.relevant_files)}",
                    f"Possible causes: {len(issue.possible_causes)}",
                    f"Context commit: {issue.context_commit_sha[:12]}",
                ]
            )
        lines.extend(
            [
                f"Images: {image_count}",
                "",
                "Reply to this preview with feedback or images, or run:",
                f"/edit {draft_id} <feedback>",
                f"/confirm {draft_id}",
                f"/cancel {draft_id}",
            ]
        )
        return truncate(
            "\n".join(lines)
        )
