from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from telegram_project_manager.platform.router import IncomingMessage, TelegramRouter


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

    def send_message(self, chat_id: int, text: str, message_thread_id: int | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        self._call("sendMessage", payload)

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
    if not isinstance(message, dict) or not isinstance(message.get("text"), str):
        return None
    sender = message.get("from")
    chat = message.get("chat")
    if not isinstance(sender, dict) or not isinstance(chat, dict):
        return None
    if not isinstance(sender.get("id"), int) or not isinstance(chat.get("id"), int):
        return None
    return IncomingMessage(
        chat_id=chat["id"],
        user_id=sender["id"],
        username=str(sender.get("username") or ""),
        text=message["text"].strip(),
        is_private=chat.get("type") == "private",
    )


async def run_polling(bot: TelegramBotApi, router: TelegramRouter) -> None:
    await asyncio.to_thread(bot.delete_webhook)
    me = await asyncio.to_thread(bot.get_me)
    router.set_bot_username(str(me.get("username") or ""))
    logging.info("bot running as @%s", router.bot_username)

    offset: int | None = None
    while True:
        try:
            updates = await asyncio.to_thread(bot.get_updates, offset)
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                incoming = incoming_message_from_update(update)
                if incoming is None:
                    continue
                response = await router.handle_message(incoming)
                if response:
                    message = update["message"]
                    thread_id = message.get("message_thread_id")
                    await asyncio.to_thread(
                        bot.send_message,
                        incoming.chat_id,
                        response,
                        thread_id if isinstance(thread_id, int) else None,
                    )
        except TelegramBotApiError:
            logging.exception("Telegram polling failed; retrying in 5 seconds")
            await asyncio.sleep(5)
