from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai_codex.types import ReasoningEffort

from telegram_project_manager.bots.code_manager.codex_sdk import (
    CODEX_JOB_SANDBOX,
    CodexSdkAdapter,
    CodexSdkError,
)
from telegram_project_manager.bots.code_manager.workspace import GitWorkspaceService, WorkspaceError
from telegram_project_manager.bots.ideas.prompts import (
    BRAINSTORM_DEVELOPER_INSTRUCTIONS,
    BRAINSTORM_PROMPT,
)
from telegram_project_manager.bots.ideas.schedule import advance_run_at
from telegram_project_manager.bots.ideas.schemas import (
    BRAINSTORM_RESPONSE_SCHEMA,
    BrainstormIdea,
    BrainstormResponse,
)
from telegram_project_manager.integrations.git.local_repository import LocalRepositoryError
from telegram_project_manager.platform.responses import TELEGRAM_TEXT_LIMIT, outgoing_message
from telegram_project_manager.platform.storage.db import Database
from telegram_project_manager.platform.telegram_bot import TelegramBotApi, TelegramBotApiError

BRAINSTORM_TIMEOUT_SECONDS = 2 * 60 * 60
BRAINSTORM_STALE_SECONDS = BRAINSTORM_TIMEOUT_SECONDS + 5 * 60
BRAINSTORM_SCHEDULER_SECONDS = 60.0
BRAINSTORM_COMPLETION_NOTICE_SECONDS = 5.0
MAX_OUTSTANDING_BRAINSTORMS = 10
MAX_CONCURRENT_BRAINSTORMS = 1


@dataclass
class BrainstormQueueEntry:
    brainstorm_id: str
    chat_id: int
    thread_id: int | None
    repo: str
    branch: str
    trigger: str
    submitted_at: int
    status: str = "queued"


class BrainstormService:
    def __init__(
        self,
        *,
        db: Database,
        codex: CodexSdkAdapter,
        workspaces: GitWorkspaceService,
        bot: TelegramBotApi,
        max_outstanding: int = MAX_OUTSTANDING_BRAINSTORMS,
        max_concurrent: int = MAX_CONCURRENT_BRAINSTORMS,
        scheduler_seconds: float = BRAINSTORM_SCHEDULER_SECONDS,
        completion_notice_seconds: float = BRAINSTORM_COMPLETION_NOTICE_SECONDS,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.db = db
        self.codex = codex
        self.workspaces = workspaces
        self.bot = bot
        self.max_outstanding = max_outstanding
        self.scheduler_seconds = scheduler_seconds
        self.completion_notice_seconds = completion_notice_seconds
        self.clock = clock or (lambda: int(time.time()))
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._notification_tasks: set[asyncio.Task[None]] = set()
        self._queue_entries: dict[str, BrainstormQueueEntry] = {}
        self._scheduler_task: asyncio.Task[None] | None = None
        self._root = (db.path.parent / "brainstorm-jobs").resolve()

    def validate_source(self, *, source_path: str, repo: str) -> str:
        return self.workspaces.validate_source(source_path=source_path, repo=repo)

    async def submit(
        self,
        *,
        chat_id: int,
        user_id: int,
        thread_id: int | None,
        reply_to_message_id: int | None,
        repo: str,
        branch: str,
        source_path: str,
    ) -> str:
        if len(self._tasks) >= self.max_outstanding:
            raise ValueError("Repository brainstorm queue is full. Try again later.")
        resolved_source = await asyncio.to_thread(
            self.validate_source, source_path=source_path, repo=repo
        )
        brainstorm_id = f"b-{uuid.uuid4().hex[:8]}"
        now = self.clock()
        claimed = self.db.claim_brainstorm_run(
            repo,
            brainstorm_id,
            "manual",
            now,
            now - BRAINSTORM_STALE_SECONDS,
        )
        if not claimed:
            raise ValueError("Brainstorming is disabled or already running for this repository.")
        try:
            status_message_id = await self._send(
                chat_id=chat_id,
                thread_id=thread_id,
                reply_to_message_id=reply_to_message_id,
                text="\n".join(
                    [
                        "Repository brainstorm queued.",
                        f"Brainstorm ID: {brainstorm_id}",
                        f"Repo: {repo}",
                        f"Branch: {branch}",
                    ]
                ),
            )
        except Exception:
            self.db.finish_brainstorm_run(
                repo, brainstorm_id, "failed", "Telegram acknowledgement failed.", self.clock()
            )
            raise
        try:
            self._start_task(
                brainstorm_id=brainstorm_id,
                chat_id=chat_id,
                user_id=user_id,
                thread_id=thread_id,
                reply_to_message_id=reply_to_message_id,
                status_message_id=status_message_id,
                repo=repo,
                branch=branch,
                source_path=resolved_source,
                trigger="manual",
            )
        except Exception:
            await self._send_failure(
                chat_id,
                thread_id,
                reply_to_message_id,
                status_message_id,
                "Brainstorm task could not be started.",
            )
            raise
        self.db.audit(
            "brainstorm.run",
            "queued",
            {"repo": repo, "branch": branch, "actor": user_id, "trigger": "manual"},
        )
        return brainstorm_id

    async def recover(self) -> None:
        if self._scheduler_task is not None and not self._scheduler_task.done():
            return
        self._scheduler_task = asyncio.create_task(
            self._scheduler_loop(), name="brainstorm-scheduler"
        )

    async def shutdown(self) -> None:
        scheduler = self._scheduler_task
        self._scheduler_task = None
        if scheduler is not None:
            scheduler.cancel()
            await asyncio.gather(scheduler, return_exceptions=True)
        active = tuple(self._tasks.items())
        for brainstorm_id, _ in active:
            await self.codex.interrupt(brainstorm_id)
        for _, task in active:
            task.cancel()
        if active:
            await asyncio.gather(*(task for _, task in active), return_exceptions=True)
        notifications = tuple(self._notification_tasks)
        for task in notifications:
            task.cancel()
        if notifications:
            await asyncio.gather(*notifications, return_exceptions=True)
        self._tasks.clear()
        self._notification_tasks.clear()
        self._queue_entries.clear()

    def queue_snapshot(
        self, *, chat_id: int, thread_id: int | None
    ) -> dict[str, tuple[dict[str, Any], ...]]:
        entries = sorted(
            (
                entry
                for entry in self._queue_entries.values()
                if entry.chat_id == chat_id and entry.thread_id == thread_id
            ),
            key=lambda entry: entry.submitted_at,
        )
        return {
            "running": tuple(
                _queue_item(entry) for entry in entries if entry.status == "running"
            ),
            "queued": tuple(
                _queue_item(entry) for entry in entries if entry.status == "queued"
            ),
        }

    async def run_due_once(self) -> None:
        if len(self._tasks) >= self.max_outstanding:
            return
        now = self.clock()
        for config in self.db.list_due_brainstorms(now):
            if len(self._tasks) >= self.max_outstanding:
                break
            repo = str(config["repo"])
            interval_days = int(config["interval_days"])
            scheduled_for = int(config["next_run_at"])
            brainstorm_id = f"b-{uuid.uuid4().hex[:8]}"
            claimed = self.db.claim_brainstorm_run(
                repo,
                brainstorm_id,
                "scheduled",
                now,
                now - BRAINSTORM_STALE_SECONDS,
                scheduled_for=scheduled_for,
                next_run_at=advance_run_at(scheduled_for, now, interval_days),
            )
            if not claimed:
                continue
            chat_id = claimed.get("telegram_chat_id")
            if not isinstance(chat_id, int):
                self.db.audit(
                    "brainstorm.result",
                    "failed",
                    {
                        "repo": repo,
                        "trigger": "scheduled",
                        "error": "Scheduled Telegram destination is not configured.",
                    },
                )
                self.db.finish_brainstorm_run(
                    repo,
                    brainstorm_id,
                    "failed",
                    "Scheduled Telegram destination is not configured.",
                    self.clock(),
                )
                continue
            self._start_task(
                brainstorm_id=brainstorm_id,
                chat_id=chat_id,
                user_id=int(claimed.get("updated_by_user_id") or 0),
                thread_id=claimed.get("telegram_thread_id"),
                reply_to_message_id=None,
                status_message_id=None,
                repo=repo,
                branch=str(claimed.get("default_branch") or "main"),
                source_path=str(claimed.get("local_repo_path") or ""),
                trigger="scheduled",
            )
            self.db.audit(
                "brainstorm.run",
                "queued",
                {
                    "repo": repo,
                    "branch": str(claimed.get("default_branch") or "main"),
                    "trigger": "scheduled",
                    "scheduled_for": scheduled_for,
                },
            )

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await self.run_due_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Unexpected repository brainstorm scheduler failure")
            await asyncio.sleep(self.scheduler_seconds)

    def _start_task(
        self,
        *,
        brainstorm_id: str,
        chat_id: int,
        user_id: int,
        thread_id: int | None,
        reply_to_message_id: int | None,
        status_message_id: int | None,
        repo: str,
        branch: str,
        source_path: str,
        trigger: str,
    ) -> None:
        self._queue_entries[brainstorm_id] = BrainstormQueueEntry(
            brainstorm_id=brainstorm_id,
            chat_id=chat_id,
            thread_id=thread_id,
            repo=repo,
            branch=branch,
            trigger=trigger,
            submitted_at=time.monotonic_ns(),
        )
        operation = self._run(
            brainstorm_id=brainstorm_id,
            chat_id=chat_id,
            user_id=user_id,
            thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
            status_message_id=status_message_id,
            repo=repo,
            branch=branch,
            source_path=source_path,
            trigger=trigger,
        )
        try:
            task = asyncio.create_task(operation, name=f"repository-brainstorm-{brainstorm_id}")
        except Exception:
            operation.close()
            self._queue_entries.pop(brainstorm_id, None)
            self.db.finish_brainstorm_run(
                repo, brainstorm_id, "failed", "Brainstorm task could not be started.", self.clock()
            )
            raise
        self._tasks[brainstorm_id] = task
        task.add_done_callback(
            lambda finished, key=brainstorm_id: self._task_finished(key, finished)
        )

    @asynccontextmanager
    async def _queue_slot(self, brainstorm_id: str) -> AsyncIterator[None]:
        async with self._semaphore:
            entry = self._queue_entries.get(brainstorm_id)
            if entry:
                entry.status = "running"
            try:
                yield
            finally:
                self._queue_entries.pop(brainstorm_id, None)

    async def _run(
        self,
        *,
        brainstorm_id: str,
        chat_id: int,
        user_id: int,
        thread_id: int | None,
        reply_to_message_id: int | None,
        status_message_id: int | None,
        repo: str,
        branch: str,
        source_path: str,
        trigger: str,
    ) -> None:
        del user_id
        workspace = self._root / brainstorm_id / "repo"
        cleanup_source = source_path
        status = "failed"
        error = ""
        try:
            async with self._queue_slot(brainstorm_id):
                resolved_source = await asyncio.to_thread(
                    self.validate_source, source_path=source_path, repo=repo
                )
                cleanup_source = resolved_source
                commit = await asyncio.to_thread(
                    self.workspaces.prepare_read_only,
                    source_path=resolved_source,
                    repo=repo,
                    base_branch=branch,
                    path=workspace,
                )
                _, raw = await self.codex.run_turn(
                    job_id=brainstorm_id,
                    cwd=str(workspace),
                    prompt=BRAINSTORM_PROMPT,
                    output_schema=BRAINSTORM_RESPONSE_SCHEMA,
                    sandbox=CODEX_JOB_SANDBOX,
                    effort=ReasoningEffort.high,
                    model_role="plan",
                    developer_instructions=BRAINSTORM_DEVELOPER_INSTRUCTIONS,
                    thread_id=None,
                    timeout_seconds=BRAINSTORM_TIMEOUT_SECONDS,
                    on_progress=_ignore_progress,
                    on_thread=_ignore_thread,
                )
                response = BrainstormResponse.from_json(raw)
                ideas = tuple(
                    _with_existing_sources(workspace, idea) for idea in response.ideas
                )
                await self._deliver(
                    chat_id=chat_id,
                    thread_id=thread_id,
                    reply_to_message_id=reply_to_message_id,
                    status_message_id=status_message_id,
                    text=_render_result(repo, branch, commit, ideas, trigger),
                )
                status = "ok"
                self.db.audit(
                    "brainstorm.result",
                    "ok",
                    {"repo": repo, "branch": branch, "trigger": trigger},
                )
                if trigger == "manual":
                    await self._send_completion_notice(chat_id, thread_id)
        except asyncio.CancelledError:
            status = "cancelled"
            error = "Brainstorm interrupted during service shutdown."
            await self._send_cancelled(
                chat_id,
                thread_id,
                reply_to_message_id,
                status_message_id,
                error,
            )
            raise
        except TelegramBotApiError as exc:
            error = _safe_error(exc)
            logging.exception("Failed to send repository brainstorm %s", brainstorm_id)
            self.db.audit(
                "brainstorm.result",
                "failed",
                {"repo": repo, "trigger": trigger, "error": error},
            )
        except (CodexSdkError, LocalRepositoryError, WorkspaceError, ValueError) as exc:
            error = _safe_error(exc)
            self.db.audit(
                "brainstorm.result",
                "failed",
                {"repo": repo, "trigger": trigger, "error": error},
            )
            await self._send_failure(
                chat_id,
                thread_id,
                reply_to_message_id,
                status_message_id,
                error,
            )
        except Exception as exc:
            error = _safe_error(exc)
            logging.exception("Unexpected repository brainstorm failure %s", brainstorm_id)
            self.db.audit(
                "brainstorm.result",
                "failed",
                {"repo": repo, "trigger": trigger, "error": error},
            )
            await self._send_failure(
                chat_id,
                thread_id,
                reply_to_message_id,
                status_message_id,
                error,
            )
        finally:
            self.db.finish_brainstorm_run(repo, brainstorm_id, status, error, self.clock())
            try:
                await asyncio.to_thread(
                    self.workspaces.cleanup_read_only,
                    source_path=cleanup_source,
                    path=workspace,
                )
            except Exception:
                logging.exception("Failed to clean repository brainstorm workspace %s", brainstorm_id)
            await asyncio.to_thread(shutil.rmtree, workspace.parent, True)

    async def _send_failure(
        self,
        chat_id: int,
        thread_id: int | None,
        reply_to_message_id: int | None,
        status_message_id: int | None,
        error: str,
    ) -> None:
        try:
            await self._deliver(
                chat_id=chat_id,
                thread_id=thread_id,
                reply_to_message_id=reply_to_message_id,
                status_message_id=status_message_id,
                text=f"Repository brainstorm failed.\nReason: {error}",
            )
        except TelegramBotApiError:
            logging.exception("Failed to send repository brainstorm error")

    async def _send_cancelled(
        self,
        chat_id: int,
        thread_id: int | None,
        reply_to_message_id: int | None,
        status_message_id: int | None,
        error: str,
    ) -> None:
        try:
            await self._deliver(
                chat_id=chat_id,
                thread_id=thread_id,
                reply_to_message_id=reply_to_message_id,
                status_message_id=status_message_id,
                text=f"Repository brainstorm cancelled.\nReason: {error}",
            )
        except TelegramBotApiError:
            logging.exception("Failed to update cancelled repository brainstorm")

    async def _send_completion_notice(
        self,
        chat_id: int,
        thread_id: int | None,
    ) -> None:
        try:
            message_id = await self._send(
                chat_id=chat_id,
                thread_id=thread_id,
                reply_to_message_id=None,
                text="✅ Repository brainstorm complete.",
            )
        except Exception:
            logging.exception("Failed to send repository brainstorm completion notification")
            return
        task = asyncio.create_task(
            self._delete_completion_notice(chat_id, message_id),
            name=f"brainstorm-completion-notice-{message_id}",
        )
        self._notification_tasks.add(task)
        task.add_done_callback(self._notification_finished)

    async def _delete_completion_notice(self, chat_id: int, message_id: int) -> None:
        try:
            await asyncio.sleep(self.completion_notice_seconds)
        except asyncio.CancelledError:
            pass
        try:
            await asyncio.to_thread(self.bot.delete_message, chat_id, message_id)
        except TelegramBotApiError as exc:
            if "message to delete not found" not in str(exc).lower():
                logging.warning(
                    "Failed to delete repository brainstorm completion notification: %s",
                    exc,
                )
        except Exception:
            logging.exception("Failed to delete repository brainstorm completion notification")

    async def _send(
        self,
        *,
        chat_id: int,
        thread_id: int | None,
        reply_to_message_id: int | None,
        text: str,
    ) -> int:
        outgoing = outgoing_message(text, reply_to_message_id=reply_to_message_id)
        result = await asyncio.to_thread(
            self.bot.send_message,
            chat_id,
            outgoing.text,
            thread_id,
            parse_mode=outgoing.parse_mode,
            reply_markup=outgoing.reply_markup(),
            disable_link_preview=outgoing.disable_link_preview,
            reply_to_message_id=outgoing.reply_to_message_id,
        )
        return int(result["message_id"])

    async def _deliver(
        self,
        *,
        chat_id: int,
        thread_id: int | None,
        reply_to_message_id: int | None,
        status_message_id: int | None,
        text: str,
    ) -> None:
        if status_message_id is None:
            await self._send(
                chat_id=chat_id,
                thread_id=thread_id,
                reply_to_message_id=reply_to_message_id,
                text=text,
            )
            return
        outgoing = outgoing_message(text)
        try:
            await asyncio.to_thread(
                self.bot.edit_message_text,
                chat_id,
                status_message_id,
                outgoing.text,
                parse_mode=outgoing.parse_mode,
                reply_markup=outgoing.reply_markup(include_empty=True),
                disable_link_preview=outgoing.disable_link_preview,
            )
        except TelegramBotApiError as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    def _task_finished(self, brainstorm_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(brainstorm_id) is task:
            self._tasks.pop(brainstorm_id, None)
        self._queue_entries.pop(brainstorm_id, None)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logging.exception("Unhandled repository brainstorm task failure %s", brainstorm_id)

    def _notification_finished(self, task: asyncio.Task[None]) -> None:
        self._notification_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logging.exception("Unhandled repository brainstorm notification failure")


async def _ignore_progress(event: dict[str, Any]) -> None:
    del event


async def _ignore_thread(thread_id: str) -> None:
    del thread_id


def _with_existing_sources(workspace: Path, idea: BrainstormIdea) -> BrainstormIdea:
    sources: list[str] = []
    root = workspace.resolve()
    for source in idea.sources:
        candidate = (root / source).resolve()
        if candidate.is_relative_to(root) and candidate.exists():
            sources.append(source)
    return BrainstormIdea(
        title=idea.title,
        opportunity=idea.opportunity,
        proposal=idea.proposal,
        value=idea.value,
        sources=tuple(sources),
    )


def _render_result(
    repo: str,
    branch: str,
    commit: str,
    ideas: tuple[BrainstormIdea, ...],
    trigger: str,
) -> str:
    lines = [
        "Repository brainstorm",
        f"Repo: {repo}",
        f"Branch: {branch}",
        f"Commit: {commit[:12]}",
        f"Trigger: {trigger}",
    ]
    for index, idea in enumerate(ideas, start=1):
        lines.extend(
            [
                "",
                f"{index}. {idea.title}",
                f"Opportunity: {idea.opportunity}",
                f"Proposal: {idea.proposal}",
                f"Value: {idea.value}",
            ]
        )
        if idea.sources:
            lines.append("Sources: " + ", ".join(idea.sources))
    result = "\n".join(lines)
    if len(result) > TELEGRAM_TEXT_LIMIT:
        raise ValueError(
            f"Repository brainstorm exceeds Telegram's {TELEGRAM_TEXT_LIMIT}-character limit"
        )
    return result


def _queue_item(entry: BrainstormQueueEntry) -> dict[str, Any]:
    return {
        "id": entry.brainstorm_id,
        "repo": entry.repo,
        "branch": entry.branch,
        "trigger": entry.trigger,
        "status": entry.status,
    }


def _safe_error(exc: BaseException) -> str:
    value = " ".join(str(exc).split())[:1000] or exc.__class__.__name__
    value = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED_API_KEY]", value)
    return re.sub(r"(?i)(authorization:\s*bearer\s+)\S+", r"\1[REDACTED]", value)
