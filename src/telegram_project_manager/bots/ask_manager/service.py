from __future__ import annotations

import asyncio
import html
import logging
import re
import shutil
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from openai_codex.types import ReasoningEffort

from telegram_project_manager.bots.ask_manager.images import stage_attachments
from telegram_project_manager.bots.ask_manager.prompts import (
    ASK_DEVELOPER_INSTRUCTIONS,
    ask_prompt,
)
from telegram_project_manager.bots.ask_manager.schemas import (
    ASK_RESPONSE_SCHEMA,
    AskResponse,
)
from telegram_project_manager.bots.code_manager.codex_sdk import (
    CODEX_JOB_SANDBOX,
    CodexSdkAdapter,
    CodexSdkError,
)
from telegram_project_manager.bots.code_manager.workspace import GitWorkspaceService, WorkspaceError
from telegram_project_manager.platform.responses import OutgoingMessage, outgoing_message
from telegram_project_manager.platform.router import IncomingAttachment
from telegram_project_manager.platform.storage.db import Database
from telegram_project_manager.platform.telegram_bot import TelegramBotApi, TelegramBotApiError


ASK_TIMEOUT_SECONDS = 2 * 60 * 60
MAX_OUTSTANDING_ASKS = 10
MAX_CONCURRENT_ASKS = 2
ASK_QUEUE_QUESTION_MAX_LENGTH = 120
TELEGRAM_ASK_TAGS = frozenset({"b", "i", "u", "s", "code", "pre", "blockquote"})
ASK_FENCED_CODE_RE = re.compile(r"```(?:[A-Za-z0-9_+.-]+)?\s*\n?(.*?)(?:```|\Z)", re.DOTALL)
ASK_INLINE_MARKUP_RE = re.compile(r"\*\*(.+?)\*\*|`([^`\n]+)`")


@dataclass
class AskQueueEntry:
    ask_id: str
    chat_id: int
    thread_id: int | None
    repo: str
    branch: str
    question: str
    image_count: int
    submitted_at: int
    status: str = "queued"


class AskService:
    def __init__(
        self,
        *,
        db: Database,
        codex: CodexSdkAdapter,
        workspaces: GitWorkspaceService,
        bot: TelegramBotApi,
        max_outstanding: int = MAX_OUTSTANDING_ASKS,
        max_concurrent: int = MAX_CONCURRENT_ASKS,
    ) -> None:
        self.db = db
        self.codex = codex
        self.workspaces = workspaces
        self.bot = bot
        self.max_outstanding = max_outstanding
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._queue_entries: dict[str, AskQueueEntry] = {}
        self._root = (db.path.parent / "ask-jobs").resolve()

    async def submit(
        self,
        *,
        chat_id: int,
        user_id: int,
        thread_id: int | None,
        message_id: int | None,
        repo: str,
        branch: str,
        source_path: str,
        question: str,
        attachments: tuple[IncomingAttachment, ...] = (),
    ) -> str:
        if len(self._tasks) >= self.max_outstanding:
            raise ValueError("Repository question queue is full. Try again after another answer completes.")
        resolved_source = await asyncio.to_thread(
            self.workspaces.validate_source,
            source_path=source_path,
            repo=repo,
        )
        ask_id = f"a-{uuid.uuid4().hex[:8]}"
        acknowledgement = [
            "Repository question queued.",
            f"Repo: {repo}",
            f"Branch: {branch}",
        ]
        if attachments:
            acknowledgement.append(f"Images: {len(attachments)}")
        await self._send(
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to_message_id=message_id,
            text="\n".join(acknowledgement),
        )
        self._queue_entries[ask_id] = AskQueueEntry(
            ask_id=ask_id,
            chat_id=chat_id,
            thread_id=thread_id,
            repo=repo,
            branch=branch,
            question=_question_preview(question),
            image_count=len(attachments),
            submitted_at=time.monotonic_ns(),
        )
        operation = self._run(
            ask_id=ask_id,
            chat_id=chat_id,
            user_id=user_id,
            thread_id=thread_id,
            message_id=message_id,
            repo=repo,
            branch=branch,
            source_path=resolved_source,
            question=question,
            attachments=attachments,
        )
        try:
            task = asyncio.create_task(operation, name=f"repository-ask-{ask_id}")
        except Exception:
            operation.close()
            self._queue_entries.pop(ask_id, None)
            raise
        self._tasks[ask_id] = task
        task.add_done_callback(lambda finished, key=ask_id: self._task_finished(key, finished))
        self.db.audit(
            "ask.question",
            "queued",
            {"repo": repo, "branch": branch, "actor": user_id, "images": len(attachments)},
        )
        return ask_id

    async def shutdown(self) -> None:
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
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
            key=lambda entry: (entry.submitted_at, entry.ask_id),
        )
        return {
            "running": tuple(
                _ask_queue_item(entry) for entry in entries if entry.status == "running"
            ),
            "queued": tuple(
                _ask_queue_item(entry) for entry in entries if entry.status == "queued"
            ),
        }

    @asynccontextmanager
    async def _queue_slot(self, ask_id: str) -> AsyncIterator[None]:
        async with self._semaphore:
            entry = self._queue_entries.get(ask_id)
            if entry:
                entry.status = "running"
            try:
                yield
            finally:
                self._queue_entries.pop(ask_id, None)

    async def _run(
        self,
        *,
        ask_id: str,
        chat_id: int,
        user_id: int,
        thread_id: int | None,
        message_id: int | None,
        repo: str,
        branch: str,
        source_path: str,
        question: str,
        attachments: tuple[IncomingAttachment, ...],
    ) -> None:
        del user_id
        workspace = self._root / ask_id / "repo"
        try:
            async with self._queue_slot(ask_id):
                commit = await asyncio.to_thread(
                    self.workspaces.prepare_read_only,
                    source_path=source_path,
                    repo=repo,
                    base_branch=branch,
                    path=workspace,
                )
                image_paths = await asyncio.to_thread(
                    stage_attachments,
                    bot=self.bot,
                    attachments=attachments,
                    destination=workspace / ".codex" / "ask-images",
                )
                _, raw = await self.codex.run_turn(
                    job_id=ask_id,
                    cwd=str(workspace),
                    prompt=ask_prompt(question),
                    image_paths=image_paths,
                    output_schema=ASK_RESPONSE_SCHEMA,
                    sandbox=CODEX_JOB_SANDBOX,
                    effort=ReasoningEffort.high,
                    model_role="plan",
                    developer_instructions=ASK_DEVELOPER_INSTRUCTIONS,
                    thread_id=None,
                    timeout_seconds=ASK_TIMEOUT_SECONDS,
                    on_progress=_ignore_progress,
                    on_thread=_ignore_thread,
                )
                response = AskResponse.from_json(raw)
                sources = _existing_sources(workspace, response.sources)
                await self._send(
                    chat_id=chat_id,
                    thread_id=thread_id,
                    reply_to_message_id=message_id,
                    text=_render_answer(repo, branch, commit, response.answer, sources),
                    telegram_html=True,
                )
                self.db.audit(
                    "ask.answer",
                    "ok",
                    {
                        "repo": repo,
                        "branch": branch,
                        "sources": len(sources),
                        "images": len(attachments),
                    },
                )
        except asyncio.CancelledError:
            raise
        except (CodexSdkError, WorkspaceError, ValueError) as exc:
            self.db.audit(
                "ask.answer",
                "failed",
                {"repo": repo, "error": _safe_error(exc), "images": len(attachments)},
            )
            await self._send_failure(chat_id, thread_id, message_id, exc)
        except TelegramBotApiError:
            logging.exception("Failed to send repository answer %s", ask_id)
        except Exception as exc:
            logging.exception("Unexpected repository answer failure %s", ask_id)
            self.db.audit(
                "ask.answer",
                "failed",
                {"repo": repo, "error": _safe_error(exc), "images": len(attachments)},
            )
            await self._send_failure(chat_id, thread_id, message_id, exc)
        finally:
            try:
                await asyncio.to_thread(
                    self.workspaces.cleanup_read_only,
                    source_path=source_path,
                    path=workspace,
                )
            except Exception:
                logging.exception("Failed to clean repository question workspace %s", ask_id)
            await asyncio.to_thread(shutil.rmtree, workspace.parent, True)

    async def _send_failure(
        self,
        chat_id: int,
        thread_id: int | None,
        message_id: int | None,
        exc: Exception,
    ) -> None:
        try:
            await self._send(
                chat_id=chat_id,
                thread_id=thread_id,
                reply_to_message_id=message_id,
                text=f"Repository question failed.\nReason: {_safe_error(exc)}",
            )
        except TelegramBotApiError:
            logging.exception("Failed to send repository question error")

    async def _send(
        self,
        *,
        chat_id: int,
        thread_id: int | None,
        reply_to_message_id: int | None,
        text: str,
        telegram_html: bool = False,
    ) -> None:
        outgoing = (
            OutgoingMessage(text=text, reply_to_message_id=reply_to_message_id)
            if telegram_html
            else outgoing_message(text, reply_to_message_id=reply_to_message_id)
        )
        await asyncio.to_thread(
            self.bot.send_message,
            chat_id,
            outgoing.text,
            thread_id,
            parse_mode=outgoing.parse_mode,
            reply_markup=outgoing.reply_markup(),
            disable_link_preview=outgoing.disable_link_preview,
            reply_to_message_id=outgoing.reply_to_message_id,
        )

    def _task_finished(self, ask_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(ask_id) is task:
            self._tasks.pop(ask_id, None)
        self._queue_entries.pop(ask_id, None)


async def _ignore_progress(event: dict[str, Any]) -> None:
    del event


async def _ignore_thread(thread_id: str) -> None:
    del thread_id


def _question_preview(question: str) -> str:
    normalized = _redact(" ".join(question.split()))
    if len(normalized) <= ASK_QUEUE_QUESTION_MAX_LENGTH:
        return normalized
    return normalized[: ASK_QUEUE_QUESTION_MAX_LENGTH - 1].rstrip() + "…"


def _ask_queue_item(entry: AskQueueEntry) -> dict[str, Any]:
    return {
        "id": entry.ask_id,
        "repo": entry.repo,
        "branch": entry.branch,
        "question": entry.question,
        "image_count": entry.image_count,
    }


def _existing_sources(workspace: Path, sources: tuple[str, ...]) -> tuple[str, ...]:
    root = workspace.resolve()
    valid: list[str] = []
    for source in sources:
        relative = Path(source)
        if relative.is_absolute() or ".." in relative.parts:
            continue
        if relative.parts[:2] == (".codex", "ask-images"):
            continue
        candidate = (root / relative).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            valid.append(source)
    return tuple(valid)


def _render_answer(
    repo: str,
    branch: str,
    commit: str,
    answer: str,
    sources: tuple[str, ...],
) -> str:
    lines = [
        "ℹ️ <b>Repository answer.</b>",
        f"<b>Repo:</b> {html.escape(repo)}",
        f"<b>Branch:</b> {html.escape(branch)}",
        f"<b>Commit:</b> <code>{html.escape(commit[:12])}</code>",
        "",
        _render_telegram_answer(answer.replace("—", "-")),
    ]
    if sources:
        lines.extend(
            [
                "",
                "<b>Sources:</b>",
                *(f"• <code>{html.escape(source)}</code>" for source in sources),
            ]
        )
    return "\n".join(lines)


def _render_telegram_answer(answer: str) -> str:
    rendered: list[str] = []
    position = 0
    for match in ASK_FENCED_CODE_RE.finditer(answer):
        rendered.append(_sanitize_telegram_html(answer[position : match.start()]))
        rendered.append(f"<pre>{html.escape(match.group(1).strip())}</pre>")
        position = match.end()
    rendered.append(_sanitize_telegram_html(answer[position:]))
    return "".join(rendered).strip()


def _sanitize_telegram_html(value: str) -> str:
    parser = _TelegramAskHtmlParser()
    parser.feed(value)
    parser.close()
    return parser.rendered()


class _TelegramAskHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._open_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag not in TELEGRAM_ASK_TAGS:
            self._parts.append(html.escape(self.get_starttag_text()))
            return
        self._parts.append(f"<{tag}>")
        self._open_tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag not in TELEGRAM_ASK_TAGS or tag not in self._open_tags:
            self._parts.append(html.escape(f"</{tag}>"))
            return
        while self._open_tags:
            open_tag = self._open_tags.pop()
            self._parts.append(f"</{open_tag}>")
            if open_tag == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._open_tags and self._open_tags[-1] in {"code", "pre"}:
            self._parts.append(html.escape(data))
            return
        position = 0
        for match in ASK_INLINE_MARKUP_RE.finditer(data):
            self._parts.append(html.escape(data[position : match.start()]))
            bold, code = match.groups()
            if bold is not None:
                self._parts.append(f"<b>{html.escape(bold)}</b>")
            else:
                self._parts.append(f"<code>{html.escape(code or '')}</code>")
            position = match.end()
        self._parts.append(html.escape(data[position:]))

    def rendered(self) -> str:
        while self._open_tags:
            self._parts.append(f"</{self._open_tags.pop()}>")
        return "".join(self._parts)


def _safe_error(exc: Exception) -> str:
    value = " ".join(str(exc).split())[:1_000] or "unknown error"
    return _redact(value)


def _redact(value: str) -> str:
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED_API_KEY]", value)
    return re.sub(r"(?i)(authorization:\s*bearer\s+)\S+", r"\1[REDACTED]", value)
