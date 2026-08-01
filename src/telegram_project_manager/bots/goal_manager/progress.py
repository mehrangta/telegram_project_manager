from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from typing import Any

from telegram_project_manager.bots.do_manager.progress import summarize_event
from telegram_project_manager.platform.responses import (
    OutgoingMessage,
    callback_button,
    copy_button,
    outgoing_message,
    truncate,
)
from telegram_project_manager.platform.storage.db import Database
from telegram_project_manager.platform.telegram_bot import TelegramBotApi, TelegramBotApiError

GOAL_CHECKIN_INTERVAL_SECONDS = 5 * 60


class GoalProgressReporter:
    def __init__(
        self,
        db: Database,
        bot: TelegramBotApi,
        *,
        min_interval: float = 3.0,
        checkin_interval: float = GOAL_CHECKIN_INTERVAL_SECONDS,
    ) -> None:
        self.db = db
        self.bot = bot
        self.min_interval = min_interval
        self.checkin_interval = checkin_interval
        self._last_update: dict[str, float] = defaultdict(float)
        self._last_message: dict[str, OutgoingMessage] = {}
        self._last_checkin: dict[str, float] = defaultdict(float)
        self._last_checkin_text: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def create(self, goal_id: str) -> None:
        goal = self.db.get_goal(goal_id)
        if not goal:
            return
        outgoing = self.render_message(goal)
        result = await asyncio.to_thread(
            self.bot.send_message,
            int(goal["telegram_chat_id"]),
            outgoing.text,
            goal.get("telegram_thread_id"),
            parse_mode=outgoing.parse_mode,
            reply_markup=outgoing.reply_markup(),
            disable_link_preview=outgoing.disable_link_preview,
        )
        self.db.update_goal(goal_id, {"telegram_message_id": int(result["message_id"])})

    async def activity(self, goal_id: str, event: dict[str, Any], *, force: bool = False) -> None:
        summary = redact(summarize_event(event))
        if summary:
            if not self.db.update_goal(goal_id, {"latest_activity": summary}):
                return
            self.db.add_goal_event(
                goal_id, str(event.get("kind") or "progress"), {"text": summary}
            )
        try:
            await self.refresh(goal_id, force=force)
            if summary:
                await self._periodic_checkin(goal_id, summary)
        except TelegramBotApiError as exc:
            logging.warning("Failed to refresh goal %s: %s", goal_id, exc)
            self.db.audit("goal.progress", "failed", {"error": safe_error(exc)}, goal_id)

    async def refresh(self, goal_id: str, *, force: bool = False) -> None:
        async with self._locks[goal_id]:
            now = time.monotonic()
            if not force and now - self._last_update[goal_id] < self.min_interval:
                return
            goal = self.db.get_goal(goal_id)
            if not goal or not goal.get("telegram_message_id"):
                return
            outgoing = self.render_message(goal)
            if outgoing == self._last_message.get(goal_id):
                return
            try:
                await asyncio.to_thread(
                    self.bot.edit_message_text,
                    int(goal["telegram_chat_id"]),
                    int(goal["telegram_message_id"]),
                    outgoing.text,
                    parse_mode=outgoing.parse_mode,
                    reply_markup=outgoing.reply_markup(include_empty=True),
                    disable_link_preview=outgoing.disable_link_preview,
                )
            except TelegramBotApiError as exc:
                if "message is not modified" not in str(exc).lower():
                    raise
            self._last_message[goal_id] = outgoing
            self._last_update[goal_id] = now

    async def checkin(self, goal_id: str, heading: str, text: str) -> None:
        goal = self.db.get_goal(goal_id)
        if not goal:
            return
        outgoing = outgoing_message(
            f"{heading}\nGoal ID: {goal_id}\n\n{redact(text)}",
            reply_to_message_id=goal.get("telegram_message_id"),
        )
        try:
            await asyncio.to_thread(
                self.bot.send_message,
                int(goal["telegram_chat_id"]),
                outgoing.text,
                goal.get("telegram_thread_id"),
                parse_mode=outgoing.parse_mode,
                reply_markup=outgoing.reply_markup(),
                disable_link_preview=outgoing.disable_link_preview,
                reply_to_message_id=outgoing.reply_to_message_id,
            )
        except TelegramBotApiError as exc:
            self.db.audit("goal.checkin", "failed", {"error": safe_error(exc)}, goal_id)

    async def cleared(self, goal: dict[str, Any]) -> None:
        if not goal.get("telegram_message_id"):
            return
        outgoing = outgoing_message(
            f"🗑 Codex goal cleared\nGoal ID: {goal['id']}", keyboard=()
        )
        try:
            await asyncio.to_thread(
                self.bot.edit_message_text,
                int(goal["telegram_chat_id"]),
                int(goal["telegram_message_id"]),
                outgoing.text,
                parse_mode=outgoing.parse_mode,
                reply_markup=outgoing.reply_markup(include_empty=True),
                disable_link_preview=outgoing.disable_link_preview,
            )
        except TelegramBotApiError as exc:
            self.db.audit("goal.progress", "failed", {"error": safe_error(exc)}, str(goal["id"]))

    async def _periodic_checkin(self, goal_id: str, text: str) -> None:
        now = time.monotonic()
        if text == self._last_checkin_text.get(goal_id):
            return
        if now - self._last_checkin[goal_id] < self.checkin_interval:
            return
        self._last_checkin[goal_id] = now
        self._last_checkin_text[goal_id] = text
        await self.checkin(goal_id, "Codex goal progress", text)

    def render_message(self, goal: dict[str, Any]) -> OutgoingMessage:
        status = str(goal["status"])
        buttons = [copy_button("📋 Goal ID", str(goal["id"])), callback_button("ℹ️ View", "command:/goal view")]
        if status in {"active", "running"}:
            buttons.append(callback_button("⏸ Pause", "command:/goal pause"))
        elif status in {"paused", "blocked"}:
            buttons.append(callback_button("▶️ Resume", "command:/goal resume"))
        keyboard = tuple(tuple(buttons[index:index + 2]) for index in range(0, len(buttons), 2))
        return outgoing_message(
            self.render(goal, self.db.list_goal_events(str(goal["id"]), limit=5)),
            keyboard=keyboard,
            expandable_prefixes=("Objective:", "Latest check-in:", "Recent activity:", "Error:"),
        )

    @staticmethod
    def render(goal: dict[str, Any], events: list[dict[str, Any]] | None = None) -> str:
        status = str(goal["status"])
        icon = {
            "active": "🧭", "running": "⚙️", "paused": "⏸", "completed": "✅",
            "blocked": "⚠️", "clearing": "🗑", "preparing": "🧭",
        }.get(status, "ℹ️")
        created = int(goal.get("created_at") or time.time())
        elapsed = max(0, int(time.time()) - created)
        lines = [
            f"{icon} Codex goal",
            f"Goal ID: {goal['id']}",
            f"Status: {status}",
            f"Repo: {goal['repo']}",
            f"Branch: {goal.get('default_branch') or 'main'}",
            f"Revision: {goal.get('objective_revision') or 1}",
            f"Elapsed: {elapsed // 60}m {elapsed % 60}s",
            "",
            "Objective:",
            redact(str(goal["objective"])),
        ]
        if goal.get("latest_activity"):
            lines.extend(["", f"Activity: {redact(str(goal['latest_activity']))}"])
        if goal.get("latest_summary"):
            lines.extend(["", "Latest check-in:", redact(str(goal["latest_summary"]))])
        if goal.get("next_step"):
            lines.append(f"Next step: {redact(str(goal['next_step']))}")
        recent = []
        for event in events or []:
            summary = event.get("summary")
            text = str(summary.get("text") or "") if isinstance(summary, dict) else ""
            if text:
                recent.append(f"- {redact(text)[:500]}")
        if recent:
            lines.extend(["", "Recent activity:", *recent])
        if goal.get("error"):
            lines.extend(["", f"Error: {redact(str(goal['error']))}"])
        lines.extend(["", "/goal view"])
        if status in {"active", "running"}:
            lines.append("/goal pause")
        if status in {"paused", "blocked"}:
            lines.append("/goal resume")
        if status != "completed":
            lines.append("/goal edit <updated goal>")
        lines.append("/goal clear")
        return truncate("\n".join(lines), 4096)


def redact(value: str) -> str:
    value = re.sub(r"sk-[A-Za-z0-9_-]+(?:\*+[A-Za-z0-9_-]+)?", "[REDACTED_API_KEY]", value)
    return re.sub(r"(?i)(authorization:\s*bearer\s+)\S+", r"\1[REDACTED]", value)


def safe_error(exc: BaseException) -> str:
    return " ".join(redact(str(exc)).split())[:1000] or exc.__class__.__name__
