from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any

from telegram_project_manager.platform.responses import OutgoingMessage, outgoing_message, truncate
from telegram_project_manager.platform.storage.db import Database
from telegram_project_manager.platform.telegram_bot import TelegramBotApi, TelegramBotApiError


class DoProgressReporter:
    def __init__(self, db: Database, bot: TelegramBotApi, min_interval: float = 3.0) -> None:
        self.db = db
        self.bot = bot
        self.min_interval = min_interval
        self._last_update: dict[str, float] = defaultdict(float)
        self._last_message: dict[str, OutgoingMessage] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def create(self, job_id: str) -> None:
        job = self.db.get_do_job(job_id)
        if not job:
            return
        outgoing = self.render_message(job)
        result = await asyncio.to_thread(
            self.bot.send_message,
            int(job["telegram_chat_id"]), outgoing.text, job.get("telegram_thread_id"),
            parse_mode=outgoing.parse_mode,
            reply_markup=outgoing.reply_markup(),
            disable_link_preview=outgoing.disable_link_preview,
        )
        self.db.update_do_job(job_id, {"telegram_message_id": int(result["message_id"])})

    async def activity(self, job_id: str, event: dict[str, Any], *, force: bool = False) -> None:
        summary = summarize_event(event)
        if summary:
            self.db.update_do_job(job_id, {"latest_activity": summary})
            self.db.add_do_job_event(
                job_id, str(event.get("kind") or "progress"), {"text": summary}
            )
        try:
            await self.refresh(job_id, force=force)
        except TelegramBotApiError as exc:
            logging.warning("Failed to refresh do job %s: %s", job_id, exc)
            self.db.audit("do.progress", "failed", {"error": _safe_error(exc)}, job_id)

    async def refresh(self, job_id: str, *, force: bool = False) -> None:
        async with self._locks[job_id]:
            now = time.monotonic()
            if not force and now - self._last_update[job_id] < self.min_interval:
                return
            job = self.db.get_do_job(job_id)
            if not job or not job.get("telegram_message_id"):
                return
            outgoing = self.render_message(job)
            if outgoing == self._last_message.get(job_id):
                return
            try:
                await asyncio.to_thread(
                    self.bot.edit_message_text,
                    int(job["telegram_chat_id"]), int(job["telegram_message_id"]),
                    outgoing.text,
                    parse_mode=outgoing.parse_mode,
                    reply_markup=outgoing.reply_markup(include_empty=True),
                    disable_link_preview=outgoing.disable_link_preview,
                )
            except TelegramBotApiError as exc:
                if "message is not modified" not in str(exc).lower():
                    raise
            self._last_message[job_id] = outgoing
            self._last_update[job_id] = now

    def render_message(self, job: dict[str, Any]) -> OutgoingMessage:
        return outgoing_message(
            self.render(job, self.db.list_do_job_events(str(job["id"]), limit=5)),
            expandable_prefixes=("Recent activity:", "Error:"),
        )

    @staticmethod
    def render(job: dict[str, Any], events: list[dict[str, Any]] | None = None) -> str:
        status = str(job["status"])
        icon = {
            "completed": "✅", "failed": "❌", "interrupted": "⚠️",
            "running": "⚙️", "queued": "🧭", "preparing": "🧭",
        }.get(status, "ℹ️")
        created = int(job.get("created_at") or time.time())
        elapsed = max(0, int(time.time()) - created)
        lines = [
            f"{icon} Codex do job",
            f"Do Job ID: {job['id']}",
            f"Mode: {job['mode']}",
            f"Status: {status}",
            f"Elapsed: {elapsed // 60}m {elapsed % 60}s",
        ]
        if job.get("repo"):
            lines.extend([f"Repo: {job['repo']}", f"Branch: {job.get('default_branch') or 'main'}"])
        if int(job.get("image_count") or 0):
            lines.append(f"Images: {job['image_count']}")
        if job.get("latest_activity"):
            lines.append(f"Activity: {job['latest_activity']}")
        recent = []
        for event in events or []:
            summary = event.get("summary")
            text = str(summary.get("text") or "") if isinstance(summary, dict) else ""
            if text:
                recent.append(f"- {text[:500]}")
        if recent:
            lines.extend(["", "Recent activity:", *recent])
        if job.get("error"):
            lines.extend(["", f"Error: {job['error']}"])
        lines.extend(["", f"Status command: /do status {job['id']}"])
        return truncate("\n".join(lines), 4096)


def summarize_event(event: dict[str, Any]) -> str:
    kind = str(event.get("kind") or "")
    if kind in {"phase", "error", "connection"}:
        return str(event.get("text") or "")[:500]
    if kind == "command":
        return f"Command {event.get('status')}: {str(event.get('text') or '')[:240]}"
    if kind == "files":
        paths = [str(item) for item in event.get("paths") or []]
        return f"Files {event.get('status')}: {', '.join(paths[:8]) or 'changes detected'}"
    if kind == "plan":
        steps = event.get("steps") or []
        active = next(
            (str(item.get("step")) for item in steps if isinstance(item, dict) and item.get("status") == "inProgress"),
            "Plan updated",
        )
        return active[:500]
    return ""


def _safe_error(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:1000] or exc.__class__.__name__
