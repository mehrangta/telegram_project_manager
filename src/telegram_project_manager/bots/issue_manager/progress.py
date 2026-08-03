from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

from telegram_project_manager.integrations.gh.issues import GhIssueReader, IssueResult
from telegram_project_manager.platform.responses import (
    CALLBACK_DATA_LIMIT,
    OutgoingMessage,
    callback_button,
    outgoing_message,
    url_button,
)
from telegram_project_manager.platform.storage.db import Database
from telegram_project_manager.platform.telegram_bot import TelegramBotApi, TelegramBotApiError

ISSUE_CONFIRMATION_REFRESH_SECONDS = 60.0


class IssueConfirmationError(RuntimeError):
    pass


def issue_confirmation_message(
    draft_id: str,
    result: IssueResult,
    *,
    reply_to_message_id: int | None = None,
    actions: bool = True,
    closed: bool = False,
) -> OutgoingMessage:
    text = "\n".join(
        [
            "Issue closed." if closed else "Issue created.",
            f"Repo: {result.repo}",
            f"Issue: #{result.number}",
            f"Title: {result.title}",
            f"Link: {result.url}",
        ]
    )
    if not actions:
        keyboard = ((url_button("↗ Issue", result.url),),)
    else:
        keyboard = (
            (
                callback_button("📝 Plan", _code_callback(result.repo, result.number)),
                callback_button(
                    "💻 Code",
                    _code_callback(result.repo, result.number, flag="--skip-plan"),
                ),
                callback_button(
                    "📝💻 Plan & Code",
                    _code_callback(result.repo, result.number, flag="--plan-and-code"),
                ),
                url_button("↗ Issue", result.url),
            ),
            (callback_button("✖️ Close", f"command:/close {draft_id}"),),
        )
    return outgoing_message(
        text,
        keyboard=keyboard,
        reply_to_message_id=reply_to_message_id,
    )


class IssueConfirmationService:
    def __init__(
        self,
        *,
        db: Database,
        bot: TelegramBotApi,
        reader: GhIssueReader,
        interval_seconds: float = ISSUE_CONFIRMATION_REFRESH_SECONDS,
    ) -> None:
        self.db = db
        self.bot = bot
        self.reader = reader
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def publish(
        self,
        *,
        draft_id: str,
        result: IssueResult,
        reply_to_message_id: int | None,
    ) -> None:
        async with self._locks[draft_id]:
            try:
                record = self.db.get_issue_draft(draft_id)
            except Exception as exc:
                raise IssueConfirmationError(_safe_error(exc)) from exc
            if not record or str(record["status"]) != "created":
                raise IssueConfirmationError("Issue is not available for live confirmation")
            outgoing = issue_confirmation_message(
                draft_id,
                result,
                reply_to_message_id=reply_to_message_id,
            )
            existing_id = record.get("telegram_confirmation_message_id")
            if existing_id is not None:
                message_id = int(existing_id)
                try:
                    await self._edit(record, message_id, outgoing)
                    return
                except TelegramBotApiError as exc:
                    if _message_not_modified(exc):
                        return
                    if not _permanent_message_error(exc):
                        raise IssueConfirmationError(_safe_error(exc)) from exc
                    try:
                        self.db.clear_issue_confirmation_message(draft_id, message_id)
                    except Exception as clear_exc:
                        raise IssueConfirmationError(_safe_error(clear_exc)) from clear_exc
                except Exception as exc:
                    raise IssueConfirmationError(_safe_error(exc)) from exc

            try:
                response = await asyncio.to_thread(
                    self.bot.send_message,
                    int(record["telegram_chat_id"]),
                    outgoing.text,
                    record.get("telegram_thread_id"),
                    parse_mode=outgoing.parse_mode,
                    reply_markup=outgoing.reply_markup(),
                    disable_link_preview=outgoing.disable_link_preview,
                    reply_to_message_id=outgoing.reply_to_message_id,
                )
                message_id = int(response["message_id"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise IssueConfirmationError(_safe_error(exc)) from exc

            try:
                stored = self.db.set_issue_confirmation_message(draft_id, message_id)
            except Exception as exc:
                await self._delete_untracked(record, message_id)
                raise IssueConfirmationError(_safe_error(exc)) from exc
            if not stored:
                await self._delete_untracked(record, message_id)
                raise IssueConfirmationError("Issue confirmation could not be registered")

    async def retire(self, draft_id: str) -> None:
        async with self._locks[draft_id]:
            try:
                record = self.db.get_issue_draft(draft_id)
            except Exception:
                logging.exception(
                    "Failed to load issue confirmation for retirement: draft_id=%s",
                    draft_id,
                )
                return
            if not record or record.get("telegram_confirmation_message_id") is None:
                return
            message_id = int(record["telegram_confirmation_message_id"])
            outgoing = issue_confirmation_message(
                draft_id,
                _result_from_record(record),
                actions=False,
                closed=True,
            )
            try:
                await self._edit(record, message_id, outgoing)
            except asyncio.CancelledError:
                raise
            except TelegramBotApiError as exc:
                if not (_message_not_modified(exc) or _permanent_message_error(exc)):
                    self._audit_failure("issue.confirmation.retire", record, exc)
                    return
            except Exception as exc:
                self._audit_failure("issue.confirmation.retire", record, exc)
                return
            try:
                self.db.clear_issue_confirmation_message(draft_id, message_id)
            except Exception:
                logging.exception(
                    "Failed to clear retired issue confirmation: draft_id=%s message_id=%s",
                    draft_id,
                    message_id,
                )

    async def recover(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="issue-confirmation-refresh")

    async def shutdown(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def refresh(self) -> None:
        for target in self.db.list_issue_confirmation_messages():
            await self._refresh_target(target)

    async def _run(self) -> None:
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Unexpected issue confirmation refresh failure")
            await asyncio.sleep(self.interval_seconds)

    async def _refresh_target(self, target: dict[str, Any]) -> None:
        draft_id = str(target["id"])
        async with self._locks[draft_id]:
            current = self.db.get_issue_draft(draft_id)
            if not _same_target(current, target):
                return
            try:
                state = await asyncio.to_thread(
                    self.reader.get_issue_state,
                    str(current["repo"]),
                    int(current["github_issue_number"]),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._audit_failure("issue.confirmation.refresh", current, exc)
                return
            if state == "open":
                return

            message_id = int(current["telegram_confirmation_message_id"])
            outgoing = issue_confirmation_message(
                draft_id,
                _result_from_record(current),
                actions=False,
                closed=True,
            )
            try:
                await self._edit(current, message_id, outgoing)
            except asyncio.CancelledError:
                raise
            except TelegramBotApiError as exc:
                if not (_message_not_modified(exc) or _permanent_message_error(exc)):
                    self._audit_failure("issue.confirmation.refresh", current, exc)
                    return
            except Exception as exc:
                self._audit_failure("issue.confirmation.refresh", current, exc)
                return
            self.db.close_issue_confirmation(draft_id, message_id)

    async def _edit(
        self,
        record: dict[str, Any],
        message_id: int,
        outgoing: OutgoingMessage,
    ) -> None:
        await asyncio.to_thread(
            self.bot.edit_message_text,
            int(record["telegram_chat_id"]),
            message_id,
            outgoing.text,
            parse_mode=outgoing.parse_mode,
            reply_markup=outgoing.reply_markup(include_empty=True),
            disable_link_preview=outgoing.disable_link_preview,
        )

    async def _delete_untracked(self, record: dict[str, Any], message_id: int) -> None:
        try:
            await asyncio.to_thread(
                self.bot.delete_message,
                int(record["telegram_chat_id"]),
                message_id,
            )
        except Exception:
            logging.exception(
                "Failed to remove untracked issue confirmation: draft_id=%s message_id=%s",
                record["id"],
                message_id,
            )

    def _audit_failure(
        self,
        action: str,
        record: dict[str, Any],
        exc: BaseException,
    ) -> None:
        self.db.audit(
            action,
            "failed",
            {
                "repo": str(record["repo"]),
                "number": int(record["github_issue_number"]),
                "error": _safe_error(exc),
            },
            str(record["id"]),
        )


def _code_callback(repo: str, number: int, *, flag: str = "") -> str:
    command = f"/code {flag}".rstrip()
    callback = f"command:{command} {repo}#{number}"
    if len(callback.encode("utf-8")) > CALLBACK_DATA_LIMIT:
        callback = f"command:{command} #{number}"
    return callback


def _result_from_record(record: dict[str, Any]) -> IssueResult:
    return IssueResult(
        repo=str(record["repo"]),
        number=int(record["github_issue_number"]),
        url=str(record["github_issue_url"]),
        title=str(record["issue_json"]["title"]),
    )


def _same_target(current: dict[str, Any] | None, target: dict[str, Any]) -> bool:
    return bool(
        current
        and str(current["status"]) == "created"
        and current.get("telegram_confirmation_message_id") is not None
        and int(current["telegram_confirmation_message_id"])
        == int(target["telegram_confirmation_message_id"])
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
