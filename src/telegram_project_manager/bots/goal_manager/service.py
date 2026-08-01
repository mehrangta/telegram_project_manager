from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
import uuid
from collections.abc import Callable
from typing import Any

from openai_codex.types import ReasoningEffort

from telegram_project_manager.bots.code_manager.codex_sdk import (
    CODEX_JOB_SANDBOX,
    CodexSdkAdapter,
    CodexSdkError,
)
from telegram_project_manager.bots.do_manager.workspace import DoWorkspaceService
from telegram_project_manager.bots.goal_manager.progress import (
    GoalProgressReporter,
    redact,
    safe_error,
)
from telegram_project_manager.bots.goal_manager.prompts import (
    goal_developer_instructions,
    goal_prompt,
)
from telegram_project_manager.bots.goal_manager.schemas import (
    GOAL_RESPONSE_SCHEMA,
    GoalTurnResult,
    GoalValidationError,
)
from telegram_project_manager.integrations.git.local_repository import LocalRepositoryError
from telegram_project_manager.platform.storage.db import Database

GOAL_TIMEOUT_SECONDS = 10 * 60 * 60
GOAL_RUN_INTERVAL_SECONDS = 5 * 60
GOAL_OBJECTIVE_MAX_LENGTH = 3500


class GoalService:
    def __init__(
        self,
        *,
        db: Database,
        codex: CodexSdkAdapter,
        reporter: GoalProgressReporter,
        workspaces: DoWorkspaceService,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.db = db
        self.codex = codex
        self.reporter = reporter
        self.workspaces = workspaces
        self.clock = clock or (lambda: int(time.time()))

    def validate_repo(self, *, source_path: str, repo: str) -> str:
        return self.workspaces.validate_source(source_path=source_path, repo=repo)

    async def submit(
        self,
        *,
        chat_id: int,
        user_id: int,
        thread_id: int | None,
        repo: str,
        branch: str,
        source_path: str,
        objective: str,
    ) -> str:
        objective = objective.strip()
        if not objective:
            raise ValueError("Goal description cannot be empty.")
        if len(objective) > GOAL_OBJECTIVE_MAX_LENGTH:
            raise ValueError(f"Goal description exceeds {GOAL_OBJECTIVE_MAX_LENGTH} characters.")
        if self.db.get_scope_goal(chat_id, thread_id):
            raise ValueError("This chat or topic already has a goal. Use /goal edit or /goal clear.")
        source_path = self.validate_repo(source_path=source_path, repo=repo)
        goal_id = f"g-{uuid.uuid4().hex[:8]}"
        workspace_path = str(self.workspaces.root / repo.lower().replace("/", "--"))
        try:
            self.db.create_goal(
                {
                    "id": goal_id,
                    "telegram_chat_id": chat_id,
                    "telegram_user_id": user_id,
                    "telegram_thread_id": thread_id,
                    "repo": repo,
                    "default_branch": branch,
                    "source_repo_path": source_path,
                    "workspace_path": workspace_path,
                    "objective": objective,
                    "status": "preparing",
                    "next_run_at": None,
                    "latest_activity": "Preparing goal status",
                }
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("This chat or topic already has a goal. Use /goal edit or /goal clear.") from exc
        try:
            await self.reporter.create(goal_id)
        except Exception:
            self.db.delete_goal(goal_id)
            raise
        self.db.update_goal(
            goal_id,
            {
                "status": "active",
                "next_run_at": self.clock(),
                "latest_activity": "Waiting for goal worker",
            },
            allowed_statuses=("preparing",),
        )
        await self.reporter.refresh(goal_id, force=True)
        self.db.audit(
            "goal.set",
            "ok",
            {"actor": user_id, "chat_id": chat_id, "repo": repo, "branch": branch},
            goal_id,
        )
        return goal_id

    def render_scope(self, *, chat_id: int, thread_id: int | None) -> str:
        goal = self.db.get_scope_goal(chat_id, thread_id)
        if not goal:
            scope = "topic" if thread_id is not None else "chat"
            raise ValueError(f"No goal is set for this {scope}.")
        return GoalProgressReporter.render(
            goal, self.db.list_goal_events(str(goal["id"]), limit=5)
        )

    async def edit(self, *, chat_id: int, thread_id: int | None, objective: str) -> str:
        objective = objective.strip()
        if not objective:
            raise ValueError("Usage: /goal edit <updated goal>")
        if len(objective) > GOAL_OBJECTIVE_MAX_LENGTH:
            raise ValueError(f"Goal description exceeds {GOAL_OBJECTIVE_MAX_LENGTH} characters.")
        goal = self._scope_goal(chat_id, thread_id)
        if not self.db.edit_goal(str(goal["id"]), objective):
            raise ValueError("Completed goals cannot be edited. Use /goal clear, then /goal set.")
        self.db.audit("goal.edit", "ok", {"chat_id": chat_id}, str(goal["id"]))
        await self._refresh(str(goal["id"]))
        return "Goal updated. A running turn will restart with the new objective."

    async def pause(self, *, chat_id: int, thread_id: int | None) -> str:
        goal = self._scope_goal(chat_id, thread_id)
        status = str(goal["status"])
        if status == "paused":
            return "Goal is already paused."
        if status not in {"active", "running"}:
            raise ValueError(f"Goal cannot be paused while {status}.")
        self.db.request_goal_pause(str(goal["id"]))
        self.db.audit("goal.pause", "ok", {"chat_id": chat_id}, str(goal["id"]))
        await self._refresh(str(goal["id"]))
        return "Goal pause requested." if status == "running" else "Goal paused."

    async def resume(self, *, chat_id: int, thread_id: int | None) -> str:
        goal = self._scope_goal(chat_id, thread_id)
        status = str(goal["status"])
        if status in {"active", "running"}:
            return "Goal is already active."
        if not self.db.resume_goal(str(goal["id"])):
            raise ValueError(f"Goal cannot be resumed while {status}.")
        self.db.audit("goal.resume", "ok", {"chat_id": chat_id}, str(goal["id"]))
        await self._refresh(str(goal["id"]))
        return "Goal resumed."

    async def clear(self, *, chat_id: int, thread_id: int | None) -> str:
        goal = self._scope_goal(chat_id, thread_id)
        result = self.db.request_goal_clear(str(goal["id"]))
        self.db.audit("goal.clear", result, {"chat_id": chat_id}, str(goal["id"]))
        if result == "requested":
            await self._refresh(str(goal["id"]))
            return "Goal clear requested. The active turn is stopping."
        await self.reporter.cleared(goal)
        return "Goal cleared."

    def queue_snapshot(
        self, *, chat_id: int, thread_id: int | None
    ) -> dict[str, tuple[dict[str, Any], ...]]:
        goals = self.db.list_goals(
            chat_id=chat_id,
            thread_id=thread_id,
            exact_thread=True,
            statuses=("active", "running"),
        )
        return {
            "running": tuple(self._queue_item(goal) for goal in goals if goal["status"] == "running"),
            "queued": tuple(self._queue_item(goal) for goal in goals if goal["status"] == "active"),
        }

    def due_goals(self) -> list[dict[str, Any]]:
        return self.db.list_goals(statuses=("active",), due_at=self.clock(), limit=100)

    def claim(self, goal_id: str) -> bool:
        return self.db.claim_goal(goal_id, self.clock())

    def lane(self, goal: dict[str, Any]) -> str:
        return str(goal["repo"]).lower()

    def control_action(self, goal_id: str) -> str:
        goal = self.db.get_goal(goal_id)
        return str(goal.get("control_action") or "") if goal else "clear"

    async def interrupt(self, goal_id: str) -> None:
        await self.codex.interrupt(goal_id)

    def mark_stopped(self, goal_id: str) -> None:
        goal = self.db.get_goal(goal_id)
        if not goal:
            return
        action = str(goal.get("control_action") or "")
        if action == "clear" or goal["status"] == "clearing":
            self.db.delete_goal(goal_id)
            return
        if action == "pause":
            self.db.update_goal(
                goal_id,
                {
                    "status": "paused", "control_action": None, "next_run_at": None,
                    "error": None, "latest_activity": "Goal paused",
                },
                allowed_statuses=("running",),
            )
            return
        if action == "restart":
            self.db.update_goal(
                goal_id,
                {
                    "status": "active", "control_action": None, "next_run_at": self.clock(),
                    "error": None, "latest_activity": "Restarting with updated objective",
                },
                allowed_statuses=("running",),
            )
            return
        self.db.update_goal(
            goal_id,
            {
                "status": "blocked", "control_action": None, "next_run_at": None,
                "error": "Goal worker stopped during an active Codex turn.",
                "latest_activity": "Blocked after worker shutdown",
            },
            allowed_statuses=("running",),
        )

    async def recover(self) -> None:
        clearing = self.db.list_goals(statuses=("clearing",), limit=100)
        blocked, _ = self.db.recover_goals()
        for goal in clearing:
            await self.reporter.cleared(goal)
        for goal_id in blocked:
            await self._refresh(goal_id)

    async def execute(self, goal_id: str) -> None:
        goal = self.db.get_goal(goal_id)
        if not goal:
            return
        try:
            await self.reporter.refresh(goal_id, force=True)
            if not self.db.is_repo_allowed(str(goal["repo"])):
                raise ValueError("Goal repository is no longer in the allowed repo list.")
            workspace = await asyncio.to_thread(
                self.workspaces.prepare,
                source_path=str(goal["source_repo_path"]),
                repo=str(goal["repo"]),
                branch=str(goal["default_branch"]),
            )
            self.db.update_goal(
                goal_id,
                {"workspace_path": str(workspace), "latest_activity": "Codex starting"},
                allowed_statuses=("running",),
            )

            async def on_thread(thread_id: str) -> None:
                self.db.update_goal(goal_id, {"codex_thread_id": thread_id})

            _, raw = await self.codex.run_turn(
                job_id=goal_id,
                cwd=str(workspace),
                prompt=goal_prompt(goal),
                output_schema=GOAL_RESPONSE_SCHEMA,
                sandbox=CODEX_JOB_SANDBOX,
                effort=ReasoningEffort.high,
                model_role="code",
                developer_instructions=goal_developer_instructions(goal_id, str(goal["repo"])),
                thread_id=str(goal.get("codex_thread_id") or "") or None,
                timeout_seconds=GOAL_TIMEOUT_SECONDS,
                on_progress=lambda event: self.reporter.activity(goal_id, event),
                on_thread=on_thread,
            )
            if await self._finish_control(goal_id):
                return
            result = GoalTurnResult.from_json(raw)
            await self._apply_result(goal_id, goal, result)
        except asyncio.CancelledError:
            raise
        except (CodexSdkError, GoalValidationError, LocalRepositoryError, OSError, ValueError) as exc:
            if not await self._finish_control(goal_id):
                await self._block(goal_id, goal, exc)
        except Exception as exc:
            logging.exception("Unexpected goal worker failure %s", goal_id)
            if not await self._finish_control(goal_id):
                await self._block(goal_id, goal, exc)

    async def _apply_result(
        self, goal_id: str, goal: dict[str, Any], result: GoalTurnResult
    ) -> None:
        now = self.clock()
        values: dict[str, Any] = {
            "latest_summary": redact(result.summary),
            "next_step": redact(result.next_step),
            "control_action": None,
            "error": None,
        }
        if result.status == "continue":
            values.update(
                status="active",
                next_run_at=now + GOAL_RUN_INTERVAL_SECONDS,
                latest_activity="Goal turn completed; next turn scheduled",
            )
            heading = "Codex goal check-in"
        elif result.status == "complete":
            values.update(
                status="completed",
                next_run_at=None,
                completed_at=now,
                latest_activity="Goal completed",
            )
            heading = "Codex goal completed"
        else:
            values.update(
                status="blocked",
                next_run_at=None,
                error=redact(result.next_step or result.summary),
                latest_activity="Goal blocked",
            )
            heading = "Codex goal blocked"
        if not self.db.update_goal(goal_id, values, allowed_statuses=("running",)):
            await self._finish_control(goal_id)
            return
        self.db.add_goal_event(goal_id, result.status, {"text": redact(result.summary)})
        self.db.audit(
            "goal.turn",
            result.status,
            {"repo": str(goal["repo"]), "revision": int(goal["objective_revision"])},
            goal_id,
        )
        await self.reporter.refresh(goal_id, force=True)
        await self.reporter.checkin(goal_id, heading, result.summary)

    async def _finish_control(self, goal_id: str) -> bool:
        goal = self.db.get_goal(goal_id)
        if not goal:
            return True
        action = str(goal.get("control_action") or "")
        status = str(goal["status"])
        if action == "clear" or status == "clearing":
            await self.reporter.cleared(goal)
            self.db.delete_goal(goal_id)
            return True
        if action == "pause":
            self.db.update_goal(
                goal_id,
                {
                    "status": "paused", "control_action": None, "next_run_at": None,
                    "latest_activity": "Goal paused", "error": None,
                },
                allowed_statuses=("running",),
            )
            await self.reporter.refresh(goal_id, force=True)
            return True
        if action == "restart":
            self.db.update_goal(
                goal_id,
                {
                    "status": "active", "control_action": None, "next_run_at": self.clock(),
                    "latest_activity": "Restarting with updated objective", "error": None,
                },
                allowed_statuses=("running",),
            )
            await self.reporter.refresh(goal_id, force=True)
            return True
        return False

    async def _block(self, goal_id: str, goal: dict[str, Any], exc: BaseException) -> None:
        error = safe_error(exc)
        if not self.db.update_goal(
            goal_id,
            {
                "status": "blocked", "control_action": None, "next_run_at": None,
                "error": error, "latest_activity": "Goal turn failed",
            },
            allowed_statuses=("running",),
        ):
            return
        self.db.audit(
            "goal.turn", "failed", {"repo": str(goal["repo"]), "error": error}, goal_id
        )
        await self.reporter.refresh(goal_id, force=True)
        await self.reporter.checkin(goal_id, "Codex goal blocked", f"Reason: {error}")

    async def _refresh(self, goal_id: str) -> None:
        try:
            await self.reporter.refresh(goal_id, force=True)
        except Exception as exc:
            self.db.audit("goal.progress", "failed", {"error": safe_error(exc)}, goal_id)

    def _scope_goal(self, chat_id: int, thread_id: int | None) -> dict[str, Any]:
        goal = self.db.get_scope_goal(chat_id, thread_id)
        if not goal:
            scope = "topic" if thread_id is not None else "chat"
            raise ValueError(f"No goal is set for this {scope}.")
        return goal

    @staticmethod
    def _queue_item(goal: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(goal["id"]),
            "repo": str(goal["repo"]),
            "branch": str(goal["default_branch"]),
            "status": str(goal["status"]),
            "objective": redact(" ".join(str(goal["objective"]).split()))[:120],
            "created_at": int(goal["created_at"]),
            "updated_at": int(goal["updated_at"]),
        }
