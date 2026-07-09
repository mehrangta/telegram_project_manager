from __future__ import annotations

import time
import uuid

from telegram_project_manager.bots.commit_manager.prompts import SYSTEM_PROMPT, build_user_prompt
from telegram_project_manager.bots.commit_manager.schemas import CommitPlan
from telegram_project_manager.platform.llm.client import OpenAICompatibleClient
from telegram_project_manager.platform.storage.db import Database


class CommitPlanner:
    def __init__(self, db: Database, llm: OpenAICompatibleClient) -> None:
        self.db = db
        self.llm = llm

    def create_plan(
        self,
        *,
        request_text: str,
        chat_id: int,
        user_id: int,
        repo: str,
        base_branch: str,
    ) -> tuple[str, CommitPlan]:
        plan_id = uuid.uuid4().hex[:8]
        target_branch = f"bot/{user_id}/{plan_id}"
        max_files = int(self.db.get_setting("max_files_per_commit", "10"))
        max_bytes = int(self.db.get_setting("max_bytes_per_commit", "100000"))
        raw = self.llm.chat_json(
            SYSTEM_PROMPT,
            build_user_prompt(
                request_text=request_text,
                repo=repo,
                base_branch=base_branch,
                target_branch=target_branch,
                max_files=max_files,
                max_bytes=max_bytes,
            ),
        )
        plan = CommitPlan.from_llm(raw, fallback_repo=repo, fallback_branch=base_branch, target_branch=target_branch)
        plan.validate(max_files=max_files, max_bytes=max_bytes)
        now = int(time.time())
        self.db.create_plan(
            {
                "id": plan_id,
                "telegram_chat_id": chat_id,
                "telegram_user_id": user_id,
                "repo": plan.repo,
                "base_branch": plan.base_branch,
                "target_branch": plan.target_branch,
                "request_text": request_text,
                "plan_json": plan.to_json(),
                "status": "pending",
                "created_at": now,
                "expires_at": now + 3600,
            }
        )
        self.db.audit("plan.create", "ok", {"repo": repo, "target_branch": target_branch}, plan_id)
        return plan_id, plan

