from __future__ import annotations

import time

from telegram_project_manager.integrations.gh.issues import GhIssueExecutor, IssueResult
from telegram_project_manager.platform.storage.db import Database


class IssueExecutionService:
    def __init__(self, db: Database, executor: GhIssueExecutor) -> None:
        self.db = db
        self.executor = executor

    def execute(
        self,
        draft_id: str,
        user_id: int,
        chat_id: int,
        thread_id: int | None,
    ) -> IssueResult:
        record = self.db.get_issue_draft(draft_id)
        if not record:
            raise ValueError("Issue draft not found.")
        if int(record["telegram_user_id"]) != user_id:
            raise ValueError("Only the requesting admin can confirm this issue draft.")
        if int(record["telegram_chat_id"]) != chat_id or record.get("telegram_thread_id") != thread_id:
            raise ValueError("Issue draft belongs to a different chat or topic.")
        if record["status"] == "created" and record.get("github_issue_url"):
            return IssueResult(
                repo=str(record["repo"]),
                number=int(record["github_issue_number"]),
                url=str(record["github_issue_url"]),
                title=str(record["issue_json"]["title"]),
            )
        if record["status"] != "pending":
            raise ValueError(f"Issue draft is not pending. Current status: {record['status']}")
        if int(record["expires_at"]) < int(time.time()):
            self.db.update_issue_draft_status(draft_id, "expired")
            raise ValueError("Issue draft expired.")
        result, paths = self.executor.create_issue(record)
        if paths:
            self.db.set_issue_attachment_paths(draft_id, paths)
        self.db.update_issue_draft_status(draft_id, "created", result.number, result.url)
        self.db.audit(
            "issue.create",
            "ok",
            {"repo": result.repo, "number": result.number, "url": result.url},
            draft_id,
        )
        return result
