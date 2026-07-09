from __future__ import annotations

import time

from telegram_project_manager.bots.commit_manager.schemas import CommitPlan
from telegram_project_manager.integrations.gh.commits import CommitResult, GhCommitExecutor
from telegram_project_manager.platform.storage.db import Database


class CommitExecutionService:
    def __init__(self, db: Database, executor: GhCommitExecutor) -> None:
        self.db = db
        self.executor = executor

    def execute(self, plan_id: str, user_id: int) -> CommitResult:
        record = self.db.get_plan(plan_id)
        if not record:
            raise ValueError("Plan not found.")
        if record["telegram_user_id"] != user_id:
            raise ValueError("Only the requesting user can confirm this plan.")
        if record["status"] != "pending":
            raise ValueError(f"Plan is not pending. Current status: {record['status']}")
        if int(record["expires_at"]) < int(time.time()):
            self.db.update_plan_status(plan_id, "expired")
            raise ValueError("Plan expired.")
        plan = CommitPlan.from_llm(
            record["plan_json"],
            fallback_repo=record["repo"],
            fallback_branch=record["base_branch"],
            target_branch=record["target_branch"],
        )
        result = self.executor.create_commit(plan)
        self.db.update_plan_status(plan_id, "committed")
        self.db.audit(
            "commit.create",
            "ok",
            {"repo": result.repo, "branch": result.branch, "sha": result.sha, "url": result.commit_url},
            plan_id,
        )
        return result

