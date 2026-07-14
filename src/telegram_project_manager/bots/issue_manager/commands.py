from __future__ import annotations

from telegram_project_manager.bots.issue_manager.executor import IssueExecutionService
from telegram_project_manager.bots.issue_manager.planner import IssuePlanner
from telegram_project_manager.bots.issue_manager.schemas import IssueDraftValidationError
from telegram_project_manager.integrations.gh.runner import GhError
from telegram_project_manager.integrations.gh.repository_context import RepositoryContextError
from telegram_project_manager.platform.llm.client import LlmError
from telegram_project_manager.platform.permissions import PermissionService
from telegram_project_manager.platform.responses import truncate
from telegram_project_manager.platform.router import IncomingMessage
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
        if command == "/confirm" and rest.strip().startswith("i-"):
            return self.confirm(message, rest.strip())
        if command == "/cancel" and rest.strip().startswith("i-"):
            return self.cancel(message, rest.strip())
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
                attachments=message.attachments,
            )
        except RepositoryContextError as exc:
            self.db.audit("issue.context", "failed", {"repo": repo, "error": str(exc)})
            return f"Issue draft not created.\nReason: {exc}"
        except (LlmError, IssueDraftValidationError, ValueError) as exc:
            self.db.audit("issue.plan", "failed", {"repo": repo, "error": str(exc)})
            return f"Issue draft not created.\nReason: {exc}"
        return truncate(
            "\n".join(
                [
                    "Issue draft created.",
                    f"Draft ID: {draft_id}",
                    f"Repo: {repo}",
                    f"Title: {issue.title}",
                    f"Summary: {issue.summary}",
                    f"Actual behavior: {issue.actual_behavior}",
                    f"Expected behavior: {issue.expected_behavior}",
                    f"Codebase context: {issue.codebase_context}",
                    f"Relevant files: {len(issue.relevant_files)}",
                    f"Possible causes: {len(issue.possible_causes)}",
                    f"Context commit: {issue.context_commit_sha[:12]}",
                    f"Images: {len(message.attachments)}",
                    "",
                    f"Run: /confirm {draft_id}",
                ]
            )
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
        if len(message.attachments) > MAX_IMAGES:
            return f"Too many images. Maximum: {MAX_IMAGES}."
        total = 0
        for attachment in message.attachments:
            if attachment.mime_type not in SUPPORTED_IMAGE_TYPES:
                return f"Unsupported image type: {attachment.mime_type}."
            if attachment.file_size > MAX_IMAGE_BYTES:
                return "Each image must be 10 MB or smaller."
            total += attachment.file_size
        if total > MAX_TOTAL_IMAGE_BYTES:
            return "Issue images must be 20 MB or smaller in total."
        return None
