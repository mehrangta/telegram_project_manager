from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any

from telegram_project_manager.platform.router import IncomingAttachment, IncomingMessage, TelegramRouter


DRAFT_ID_PATTERN = re.compile(r"(?m)^Draft ID:\s*(i-[0-9a-f]{8})\s*$")
CODE_JOB_ID_PATTERN = re.compile(r"(?m)^Code Job ID:\s*(c-[0-9a-f]{8})\s*$")
ISSUE_REPO_PATTERN = re.compile(r"(?m)^Repo:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\s*$")
ISSUE_NUMBER_PATTERN = re.compile(r"(?m)^Issue:\s*#(\d+)\s*$")


class TelegramBotApiError(RuntimeError):
    pass


class TelegramBotApi:
    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"

    def get_me(self) -> dict[str, Any]:
        return self._call("getMe")

    def delete_webhook(self) -> None:
        self._call("deleteWebhook", {"drop_pending_updates": False})

    def get_updates(self, offset: int | None = None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": 50, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload, timeout=60)
        if not isinstance(result, list):
            raise TelegramBotApiError("Telegram Bot API getUpdates returned invalid data")
        return result

    def get_file(self, file_id: str) -> dict[str, Any]:
        result = self._call("getFile", {"file_id": file_id})
        if not isinstance(result, dict) or not isinstance(result.get("file_path"), str):
            raise TelegramBotApiError("Telegram Bot API getFile returned invalid data")
        return result

    def download_file(self, file_id: str, max_bytes: int = 10_000_000) -> bytes:
        file_info = self.get_file(file_id)
        file_path = str(file_info["file_path"])
        request = urllib.request.Request(f"{self.base_url.replace('/bot', '/file/bot')}/{file_path}")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                declared_size = response.headers.get("Content-Length")
                if declared_size and int(declared_size) > max_bytes:
                    raise TelegramBotApiError("Telegram image exceeds the size limit")
                content = response.read(max_bytes + 1)
        except TelegramBotApiError:
            raise
        except OSError as exc:
            raise TelegramBotApiError(f"Telegram file download failed: {exc}") from exc
        if len(content) > max_bytes:
            raise TelegramBotApiError("Telegram image exceeds the size limit")
        return content

    def send_message(
        self, chat_id: int, text: str, message_thread_id: int | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        result = self._call("sendMessage", payload)
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
            raise TelegramBotApiError("Telegram Bot API sendMessage returned invalid data")
        return result

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        self._call(
            "editMessageText",
            {"chat_id": chat_id, "message_id": message_id, "text": text},
        )

    def _call(self, method: str, payload: dict[str, Any] | None = None, timeout: int = 30) -> Any:
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=json.dumps(payload or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise TelegramBotApiError(f"Telegram Bot API {method} HTTP {exc.code}: {details[:500]}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise TelegramBotApiError(f"Telegram Bot API {method} failed: {exc}") from exc

        if not isinstance(body, dict) or not body.get("ok"):
            description = body.get("description", "invalid response") if isinstance(body, dict) else "invalid response"
            raise TelegramBotApiError(f"Telegram Bot API {method} failed: {description}")
        return body.get("result")


def incoming_message_from_update(update: dict[str, Any]) -> IncomingMessage | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    sender = message.get("from")
    chat = message.get("chat")
    if not isinstance(sender, dict) or not isinstance(chat, dict):
        return None
    if not isinstance(sender.get("id"), int) or not isinstance(chat.get("id"), int):
        return None
    text = message.get("text") if isinstance(message.get("text"), str) else message.get("caption")
    text = text if isinstance(text, str) else ""
    attachments = _attachments_from_message(message)
    if not text and not attachments:
        return None
    return IncomingMessage(
        chat_id=chat["id"],
        user_id=sender["id"],
        username=str(sender.get("username") or ""),
        text=text.strip(),
        is_private=chat.get("type") == "private",
        attachments=attachments,
        message_id=message.get("message_id") if isinstance(message.get("message_id"), int) else None,
        media_group_id=str(message["media_group_id"]) if message.get("media_group_id") is not None else None,
        thread_id=message.get("message_thread_id") if isinstance(message.get("message_thread_id"), int) else None,
        reply_to_draft_id=_reply_to_draft_id(message),
        reply_to_issue_ref=_reply_to_issue_ref(message),
        reply_to_code_job_id=_reply_to_code_job_id(message),
    )


def incoming_message_from_updates(updates: list[dict[str, Any]]) -> IncomingMessage | None:
    messages = [item for item in (incoming_message_from_update(update) for update in updates) if item]
    if not messages:
        return None
    messages.sort(key=lambda item: item.message_id or 0)
    base = next((item for item in messages if item.text), messages[0])
    attachments = tuple(attachment for item in messages for attachment in item.attachments)
    return IncomingMessage(
        chat_id=base.chat_id,
        user_id=base.user_id,
        username=base.username,
        text=base.text,
        is_private=base.is_private,
        attachments=attachments,
        message_id=base.message_id,
        media_group_id=base.media_group_id,
        thread_id=base.thread_id,
        reply_to_draft_id=next(
            (item.reply_to_draft_id for item in messages if item.reply_to_draft_id), None
        ),
        reply_to_issue_ref=next(
            (item.reply_to_issue_ref for item in messages if item.reply_to_issue_ref), None
        ),
        reply_to_code_job_id=next(
            (item.reply_to_code_job_id for item in messages if item.reply_to_code_job_id), None
        ),
    )


def _reply_to_draft_id(message: dict[str, Any]) -> str | None:
    text = _replied_bot_text(message)
    if text is None:
        return None
    match = DRAFT_ID_PATTERN.search(text)
    return match.group(1) if match else None


def _reply_to_code_job_id(message: dict[str, Any]) -> str | None:
    text = _replied_bot_text(message)
    if text is None:
        return None
    match = CODE_JOB_ID_PATTERN.search(text)
    return match.group(1) if match else None


def _reply_to_issue_ref(message: dict[str, Any]) -> str | None:
    text = _replied_bot_text(message)
    if text is None:
        return None
    repo = ISSUE_REPO_PATTERN.search(text)
    number = ISSUE_NUMBER_PATTERN.search(text)
    if not repo or not number:
        return None
    return f"{repo.group(1)}#{number.group(1)}"


def _replied_bot_text(message: dict[str, Any]) -> str | None:
    replied = message.get("reply_to_message")
    if not isinstance(replied, dict):
        return None
    sender = replied.get("from")
    if not isinstance(sender, dict) or sender.get("is_bot") is not True:
        return None
    text = replied.get("text") if isinstance(replied.get("text"), str) else replied.get("caption")
    return text if isinstance(text, str) else None


def _attachments_from_message(message: dict[str, Any]) -> tuple[IncomingAttachment, ...]:
    photo = message.get("photo")
    if isinstance(photo, list):
        sizes = [item for item in photo if isinstance(item, dict) and isinstance(item.get("file_id"), str)]
        if sizes:
            largest = max(
                sizes,
                key=lambda item: (
                    int(item.get("file_size") or 0),
                    int(item.get("width") or 0) * int(item.get("height") or 0),
                ),
            )
            return (
                IncomingAttachment(
                    file_id=str(largest["file_id"]),
                    file_unique_id=str(largest.get("file_unique_id") or largest["file_id"]),
                    mime_type="image/jpeg",
                    file_size=int(largest.get("file_size") or 0),
                ),
            )
    document = message.get("document")
    if isinstance(document, dict) and isinstance(document.get("file_id"), str):
        mime_type = str(document.get("mime_type") or "")
        if mime_type.startswith("image/"):
            return (
                IncomingAttachment(
                    file_id=str(document["file_id"]),
                    file_unique_id=str(document.get("file_unique_id") or document["file_id"]),
                    mime_type=mime_type,
                    file_size=int(document.get("file_size") or 0),
                ),
            )
    return ()


async def run_polling(bot: TelegramBotApi, router: TelegramRouter) -> None:
    await asyncio.to_thread(bot.delete_webhook)
    me = await asyncio.to_thread(bot.get_me)
    router.set_bot_username(str(me.get("username") or ""))
    logging.info("bot running as @%s", router.bot_username)

    offset: int | None = None
    pending_albums: dict[str, list[dict[str, Any]]] = {}
    album_tasks: dict[str, asyncio.Task[None]] = {}

    async def dispatch(incoming: IncomingMessage | None) -> None:
        if incoming is None:
            return
        response = await router.handle_message(incoming)
        if response:
            await asyncio.to_thread(bot.send_message, incoming.chat_id, response, incoming.thread_id)

    async def flush_album(media_group_id: str) -> None:
        await asyncio.sleep(0.75)
        updates = pending_albums.pop(media_group_id, [])
        album_tasks.pop(media_group_id, None)
        try:
            await dispatch(incoming_message_from_updates(updates))
        except TelegramBotApiError:
            logging.exception("Telegram album response failed")
        except Exception:
            logging.exception("Unexpected Telegram album processing failure")

    while True:
        try:
            updates = await asyncio.to_thread(bot.get_updates, offset)
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                raw_message = update.get("message")
                media_group_id = (
                    str(raw_message["media_group_id"])
                    if isinstance(raw_message, dict) and raw_message.get("media_group_id") is not None
                    else None
                )
                if media_group_id:
                    pending_albums.setdefault(media_group_id, []).append(update)
                    if media_group_id not in album_tasks:
                        album_tasks[media_group_id] = asyncio.create_task(flush_album(media_group_id))
                    continue
                incoming = incoming_message_from_update(update)
                await dispatch(incoming)
        except TelegramBotApiError:
            logging.exception("Telegram polling failed; retrying in 5 seconds")
            await asyncio.sleep(5)
