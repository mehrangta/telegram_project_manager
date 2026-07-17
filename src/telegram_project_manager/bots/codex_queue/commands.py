from __future__ import annotations

from collections.abc import Callable
from typing import Any

from telegram_project_manager.bots.ask_manager.service import AskService
from telegram_project_manager.bots.code_manager.service import CodeJobService
from telegram_project_manager.bots.do_manager.service import DoService
from telegram_project_manager.platform.permissions import PermissionService
from telegram_project_manager.platform.responses import OutgoingMessage, outgoing_message
from telegram_project_manager.platform.router import IncomingMessage
from telegram_project_manager.platform.storage.db import Database


QueueSnapshot = dict[str, tuple[dict[str, Any], ...]]


class CodexQueueManager:
    def __init__(
        self,
        *,
        db: Database,
        code_service: CodeJobService,
        ask_service: AskService,
        do_service: DoService,
    ) -> None:
        self.permissions = PermissionService(db)
        self.code_service = code_service
        self.ask_service = ask_service
        self.do_service = do_service

    async def handle(self, message: IncomingMessage) -> str | OutgoingMessage | None:
        command, _, rest = message.text.strip().partition(" ")
        if command.split("@", 1)[0].lower() != "/queue":
            return None
        admin_error = self.permissions.require_admin(message.user_id)
        if admin_error:
            return admin_error
        if rest.strip():
            return "Usage: /queue"

        code = self.code_service.queue_snapshot(
            chat_id=message.chat_id, thread_id=message.thread_id
        )
        asks = self.ask_service.queue_snapshot(
            chat_id=message.chat_id, thread_id=message.thread_id
        )
        do = self.do_service.queue_snapshot(
            chat_id=message.chat_id, thread_id=message.thread_id
        )
        if not any(
            (
                *code["running"],
                *code["queued"],
                *asks["running"],
                *asks["queued"],
                *do["running"],
                *do["queued"],
            )
        ):
            scope = "topic" if message.thread_id is not None else "chat"
            return f"No Codex work is running or queued for this {scope}."
        return outgoing_message(_render_queue(code, asks, do))


def _render_queue(code: QueueSnapshot, asks: QueueSnapshot, do: QueueSnapshot) -> str:
    lines = ["Codex queue"]
    _append_section(lines, "Code jobs", code, _render_code_item)
    _append_section(lines, "Repository questions", asks, _render_ask_item)
    _append_section(lines, "Full-access jobs", do, _render_do_item)
    return "\n".join(lines)


def _append_section(
    lines: list[str],
    title: str,
    snapshot: QueueSnapshot,
    render_item: Callable[[dict[str, Any]], str],
) -> None:
    if not snapshot["running"] and not snapshot["queued"]:
        return
    lines.extend(["", title])
    for state, label in (("running", "Running"), ("queued", "Queued")):
        items = snapshot[state]
        if not items:
            continue
        lines.append(f"{label} ({len(items)}):")
        lines.extend(f"- {render_item(item)}" for item in items)


def _render_code_item(item: dict[str, Any]) -> str:
    status = str(item["status"]).replace("_", " ")
    return f"{item['id']} {item['repo']}#{item['issue_number']} — {status}"


def _render_ask_item(item: dict[str, Any]) -> str:
    return (
        f"{item['id']} {item['repo']}@{item['branch']}"
        f"{_image_label(item)} — {item['question']}"
    )


def _render_do_item(item: dict[str, Any]) -> str:
    target = (
        f"{item['repo']}@{item['branch']}"
        if item["mode"] == "repo"
        else "host"
    )
    status = str(item["status"]).replace("_", " ")
    return f"{item['id']} {target}{_image_label(item)} — {status}: {item['request']}"


def _image_label(item: dict[str, Any]) -> str:
    images = int(item.get("image_count") or 0)
    if not images:
        return ""
    return f" · {images} image{'s' if images != 1 else ''}"
