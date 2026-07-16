from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai_codex.types import ReasoningEffort

from telegram_project_manager.bots.code_manager.codex_sdk import (
    CODEX_JOB_SANDBOX,
    CodexSdkAdapter,
    CodexSdkError,
)
from telegram_project_manager.bots.do_manager.prompts import DO_DEVELOPER_INSTRUCTIONS
from telegram_project_manager.platform.responses import outgoing_message
from telegram_project_manager.platform.storage.db import Database
from telegram_project_manager.platform.telegram_bot import TelegramBotApi, TelegramBotApiError

DO_TIMEOUT_SECONDS = 2 * 60 * 60
MAX_OUTSTANDING_DO_JOBS = 10
MAX_CONCURRENT_DO_JOBS = 1


@dataclass(frozen=True)
class DoJobContext:
    chat_id: int
    user_id: int
    message_id: int | None


class DoService:
    def __init__(
        self,
        *,
        db: Database,
        codex: CodexSdkAdapter,
        bot: TelegramBotApi,
        working_directory: Path,
        max_outstanding: int = MAX_OUTSTANDING_DO_JOBS,
        max_concurrent: int = MAX_CONCURRENT_DO_JOBS,
    ) -> None:
        self.db = db
        self.codex = codex
        self.bot = bot
        self.working_directory = working_directory.resolve()
        self.max_outstanding = max_outstanding
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._contexts: dict[str, DoJobContext] = {}

    async def submit(
        self,
        *,
        chat_id: int,
        user_id: int,
        message_id: int | None,
        job: str,
    ) -> str:
        if len(self._tasks) >= self.max_outstanding:
            raise ValueError("Full-access job queue is full. Try again after another job completes.")
        do_id = f"d-{uuid.uuid4().hex[:8]}"
        await self._send(
            chat_id=chat_id,
            reply_to_message_id=message_id,
            text="Full-access job queued.",
        )
        context = DoJobContext(chat_id=chat_id, user_id=user_id, message_id=message_id)
        task = asyncio.create_task(
            self._run(do_id=do_id, context=context, job=job),
            name=f"full-access-do-{do_id}",
        )
        self._contexts[do_id] = context
        self._tasks[do_id] = task
        task.add_done_callback(lambda finished, key=do_id: self._task_finished(key, finished))
        self.db.audit(
            "do.execute",
            "queued",
            {"actor": user_id, "chat_id": chat_id},
            do_id,
        )
        return do_id

    async def shutdown(self) -> None:
        jobs = tuple(
            (do_id, task)
            for do_id, task in self._tasks.items()
            if not task.done()
        )
        for do_id, _ in jobs:
            context = self._contexts.get(do_id)
            self.db.audit(
                "do.execute",
                "interrupted",
                _audit_details(context),
                do_id,
            )
        if jobs:
            await asyncio.gather(
                *(self.codex.interrupt(do_id) for do_id, _ in jobs),
                return_exceptions=True,
            )
        for _, task in jobs:
            task.cancel()
        if jobs:
            await asyncio.gather(*(task for _, task in jobs), return_exceptions=True)
        self._tasks.clear()
        self._contexts.clear()

    async def _run(self, *, do_id: str, context: DoJobContext, job: str) -> None:
        try:
            async with self._semaphore:
                _, result = await self.codex.run_text_turn(
                    job_id=do_id,
                    cwd=str(self.working_directory),
                    prompt=job,
                    sandbox=CODEX_JOB_SANDBOX,
                    effort=ReasoningEffort.high,
                    model_role="code",
                    developer_instructions=DO_DEVELOPER_INSTRUCTIONS,
                    thread_id=None,
                    timeout_seconds=DO_TIMEOUT_SECONDS,
                    on_progress=_ignore_progress,
                    on_thread=_ignore_thread,
                )
            self.db.audit("do.execute", "ok", _audit_details(context), do_id)
            await self._send_result(do_id, context, result)
        except asyncio.CancelledError:
            raise
        except (CodexSdkError, ValueError) as exc:
            self.db.audit(
                "do.execute",
                "failed",
                {**_audit_details(context), "error": _safe_error(exc)},
                do_id,
            )
            await self._send_failure(do_id, context, exc)
        except Exception as exc:
            logging.exception("Unexpected full-access job failure %s", do_id)
            self.db.audit(
                "do.execute",
                "failed",
                {**_audit_details(context), "error": _safe_error(exc)},
                do_id,
            )
            await self._send_failure(do_id, context, exc)

    async def _send_result(self, do_id: str, context: DoJobContext, result: str) -> None:
        try:
            await self._send(
                chat_id=context.chat_id,
                reply_to_message_id=context.message_id,
                text=f"Full-access job completed.\n\n{_redact(result)}",
            )
        except TelegramBotApiError as exc:
            logging.exception("Failed to send full-access job result %s", do_id)
            self.db.audit(
                "do.reply",
                "failed",
                {**_audit_details(context), "error": _safe_error(exc)},
                do_id,
            )

    async def _send_failure(
        self,
        do_id: str,
        context: DoJobContext,
        exc: Exception,
    ) -> None:
        try:
            await self._send(
                chat_id=context.chat_id,
                reply_to_message_id=context.message_id,
                text=f"Full-access job failed.\nReason: {_safe_error(exc)}",
            )
        except TelegramBotApiError as send_exc:
            logging.exception("Failed to send full-access job error %s", do_id)
            self.db.audit(
                "do.reply",
                "failed",
                {**_audit_details(context), "error": _safe_error(send_exc)},
                do_id,
            )

    async def _send(
        self,
        *,
        chat_id: int,
        reply_to_message_id: int | None,
        text: str,
    ) -> None:
        outgoing = outgoing_message(text, reply_to_message_id=reply_to_message_id)
        await asyncio.to_thread(
            self.bot.send_message,
            chat_id,
            outgoing.text,
            None,
            parse_mode=outgoing.parse_mode,
            reply_markup=outgoing.reply_markup(),
            disable_link_preview=outgoing.disable_link_preview,
            reply_to_message_id=outgoing.reply_to_message_id,
        )

    def _task_finished(self, do_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(do_id) is task:
            self._tasks.pop(do_id, None)
            self._contexts.pop(do_id, None)


async def _ignore_progress(event: dict[str, Any]) -> None:
    del event


async def _ignore_thread(thread_id: str) -> None:
    del thread_id


def _audit_details(context: DoJobContext | None) -> dict[str, int]:
    if context is None:
        return {}
    return {"actor": context.user_id, "chat_id": context.chat_id}


def _redact(value: str) -> str:
    value = re.sub(
        r"sk-[A-Za-z0-9_-]+(?:\*+[A-Za-z0-9_-]+)?",
        "[REDACTED_API_KEY]",
        value,
    )
    return re.sub(r"(?i)(authorization:\s*bearer\s+)\S+", r"\1[REDACTED]", value)


def _safe_error(exc: Exception) -> str:
    return " ".join(_redact(str(exc)).split())[:1_000] or "unknown error"
