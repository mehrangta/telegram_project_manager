from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
from collections import defaultdict
from typing import Any

from telegram_project_manager.integrations.gh.issues import GhIssueReader, IssueSummary
from telegram_project_manager.platform.responses import (
    COPY_TEXT_LIMIT,
    OutgoingMessage,
    copy_button,
)
from telegram_project_manager.platform.storage.db import Database
from telegram_project_manager.platform.telegram_bot import TelegramBotApi, TelegramBotApiError

ISSUE_LIST_REFRESH_SECONDS = 60.0


class IssueListError(RuntimeError):
    pass


class IssueListService:
    def __init__(
        self,
        *,
        db: Database,
        bot: TelegramBotApi,
        reader: GhIssueReader,
        interval_seconds: float = ISSUE_LIST_REFRESH_SECONDS,
    ) -> None:
        self.db = db
        self.bot = bot
        self.reader = reader
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._locks: dict[tuple[int, int | None], asyncio.Lock] = defaultdict(asyncio.Lock)

    async def publish(self, *, chat_id: int, thread_id: int | None, repo: str) -> None:
        async with self._locks[(chat_id, thread_id)]:
            try:
                issues = await asyncio.to_thread(self.reader.list_open_issues, repo)
                outgoing = self.render(repo, issues)
                render_hash = self.render_hash(outgoing)
                result = await asyncio.to_thread(
                    self.bot.send_message,
                    chat_id,
                    outgoing.text,
                    thread_id,
                    parse_mode=outgoing.parse_mode,
                    reply_markup=outgoing.reply_markup(),
                    disable_link_preview=outgoing.disable_link_preview,
                )
                message_id = int(result["message_id"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise IssueListError(_safe_error(exc)) from exc

            try:
                self.db.upsert_issue_list_message(
                    chat_id,
                    thread_id,
                    message_id,
                    repo,
                    render_hash,
                )
            except Exception as exc:
                try:
                    await asyncio.to_thread(self.bot.delete_message, chat_id, message_id)
                except Exception:
                    logging.exception(
                        "Failed to remove untracked issue list: chat_id=%s message_id=%s",
                        chat_id,
                        message_id,
                    )
                raise IssueListError(_safe_error(exc)) from exc

    async def recover(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="issue-list-refresh")

    async def shutdown(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def refresh(self) -> None:
        targets = self.db.list_issue_list_messages()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for target in targets:
            repo = str(target["repo"])
            if not self.db.is_repo_allowed(repo):
                self.db.delete_issue_list_message(
                    int(target["telegram_chat_id"]),
                    target.get("telegram_thread_id"),
                    int(target["telegram_message_id"]),
                    repo,
                )
                continue
            grouped[repo].append(target)

        for repo, repo_targets in grouped.items():
            try:
                issues = await asyncio.to_thread(self.reader.list_open_issues, repo)
                outgoing = self.render(repo, issues)
                render_hash = self.render_hash(outgoing)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._audit_refresh_failure(repo, None, None, exc)
                continue
            for target in repo_targets:
                if str(target["render_hash"]) == render_hash:
                    continue
                await self._refresh_target(target, outgoing, render_hash)

    async def _run(self) -> None:
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.exception("Unexpected issue list refresh failure")
                self._audit_refresh_failure("", None, None, exc)
            await asyncio.sleep(self.interval_seconds)

    async def _refresh_target(
        self,
        target: dict[str, Any],
        outgoing: OutgoingMessage,
        render_hash: str,
    ) -> None:
        chat_id = int(target["telegram_chat_id"])
        thread_id = target.get("telegram_thread_id")
        message_id = int(target["telegram_message_id"])
        repo = str(target["repo"])
        async with self._locks[(chat_id, thread_id)]:
            current = self.db.get_issue_list_message(chat_id, thread_id)
            if not _same_target(current, message_id, repo):
                return
            try:
                await asyncio.to_thread(
                    self.bot.edit_message_text,
                    chat_id,
                    message_id,
                    outgoing.text,
                    parse_mode=outgoing.parse_mode,
                    reply_markup=outgoing.reply_markup(include_empty=True),
                    disable_link_preview=outgoing.disable_link_preview,
                )
            except asyncio.CancelledError:
                raise
            except TelegramBotApiError as exc:
                if _message_not_modified(exc):
                    self.db.update_issue_list_render_hash(
                        chat_id, thread_id, message_id, repo, render_hash
                    )
                elif _permanent_message_error(exc):
                    self.db.delete_issue_list_message(chat_id, thread_id, message_id, repo)
                else:
                    self._audit_refresh_failure(repo, chat_id, thread_id, exc)
                return
            except Exception as exc:
                self._audit_refresh_failure(repo, chat_id, thread_id, exc)
                return
            self.db.update_issue_list_render_hash(
                chat_id, thread_id, message_id, repo, render_hash
            )

    def _audit_refresh_failure(
        self,
        repo: str,
        chat_id: int | None,
        thread_id: int | None,
        exc: BaseException,
    ) -> None:
        details: dict[str, Any] = {"repo": repo, "error": _safe_error(exc)}
        if chat_id is not None:
            details["chat_id"] = chat_id
            details["thread_id"] = thread_id
        self.db.audit("issues.refresh", "failed", details)

    @staticmethod
    def render(repo: str, issues: list[IssueSummary]) -> OutgoingMessage:
        escaped_repo = html.escape(repo)
        if not issues:
            return OutgoingMessage(text=f"No open issues for {escaped_repo}.")

        lines = [f"Open issues for {escaped_repo}:"]
        keyboard = []
        for issue in issues:
            command = f"/code {repo}#{issue.number}"
            if len(command) > COPY_TEXT_LIMIT:
                command = f"/code #{issue.number}"
            lines.append(
                f'- <a href="{html.escape(issue.url, quote=True)}">#{issue.number}</a> — '
                f"{html.escape(issue.title)}\n  <code>{html.escape(command)}</code>"
            )
            keyboard.append((copy_button(f"📋 Code #{issue.number}", command),))
        return OutgoingMessage(text="\n".join(lines), keyboard=tuple(keyboard))

    @staticmethod
    def render_hash(outgoing: OutgoingMessage) -> str:
        payload = {
            "text": outgoing.text,
            "parse_mode": outgoing.parse_mode,
            "reply_markup": outgoing.reply_markup(include_empty=True),
            "disable_link_preview": outgoing.disable_link_preview,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _same_target(target: dict[str, Any] | None, message_id: int, repo: str) -> bool:
    return bool(
        target
        and int(target["telegram_message_id"]) == message_id
        and str(target["repo"]) == repo
    )


def _message_not_modified(exc: BaseException) -> bool:
    return "message is not modified" in str(exc).lower()


def _permanent_message_error(exc: BaseException) -> bool:
    value = str(exc).lower()
    return any(
        marker in value
        for marker in (
            "message to edit not found",
            "message can't be edited",
            "message can not be edited",
            "message_id_invalid",
            "chat not found",
            "bot was blocked",
            "not enough rights",
        )
    )


def _safe_error(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:1000] or exc.__class__.__name__
