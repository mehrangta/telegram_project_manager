from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any

from telegram_project_manager.bots.code_manager.schemas import CodePlan
from telegram_project_manager.platform.responses import OutgoingMessage, outgoing_message, truncate
from telegram_project_manager.platform.storage.db import Database
from telegram_project_manager.platform.telegram_bot import TelegramBotApi, TelegramBotApiError


class CodeProgressReporter:
    def __init__(self, db: Database, bot: TelegramBotApi, min_interval: float = 3.0) -> None:
        self.db = db
        self.bot = bot
        self.min_interval = min_interval
        self._last_update: dict[str, float] = defaultdict(float)
        self._last_message: dict[str, OutgoingMessage] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def create(self, job_id: str) -> None:
        job = self.db.get_code_job(job_id)
        if not job:
            return
        outgoing = self.render_message(job)
        result = await asyncio.to_thread(
            self.bot.send_message,
            int(job["telegram_chat_id"]),
            outgoing.text,
            job.get("telegram_thread_id"),
            parse_mode=outgoing.parse_mode,
            reply_markup=outgoing.reply_markup(),
            disable_link_preview=outgoing.disable_link_preview,
        )
        self.db.update_code_job(job_id, {"telegram_message_id": int(result["message_id"])})

    async def activity(self, job_id: str, event: dict[str, Any], *, force: bool = False) -> None:
        activity = _event_summary(event)
        if activity:
            self.db.update_code_job(job_id, {"latest_activity": activity})
            self.db.add_code_job_event(job_id, str(event.get("kind") or "progress"), {"text": activity})
        await self.refresh(job_id, force=force)

    async def refresh(self, job_id: str, *, force: bool = False) -> None:
        async with self._locks[job_id]:
            now = time.monotonic()
            if not force and now - self._last_update[job_id] < self.min_interval:
                return
            job = self.db.get_code_job(job_id)
            if not job or not job.get("telegram_message_id"):
                return
            outgoing = self.render_message(job)
            if outgoing == self._last_message.get(job_id):
                return
            try:
                await asyncio.to_thread(
                    self.bot.edit_message_text,
                    int(job["telegram_chat_id"]),
                    int(job["telegram_message_id"]),
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

    async def notify_terminal(self, job_id: str) -> None:
        job = self.db.get_code_job(job_id)
        if not job or job["status"] not in {"ready", "failed"}:
            return
        outcome = "✅ Code job ready" if job["status"] == "ready" else "❌ Code job failed"
        lines = [outcome, f"Code Job ID: {job['id']}"]
        if job.get("pull_request_url"):
            lines.append(f"Pull request: {job['pull_request_url']}")
        if job["status"] == "ready" and not job.get("deployment_merge_sha"):
            lines.append(f"Deploy: /deploy {job['id']}")
        elif job["status"] == "failed":
            lines.extend(
                [
                    f"Retry: /code retry {job['id']}",
                    f"Status: /code status {job['id']}",
                ]
            )
        outgoing = outgoing_message("\n".join(lines))
        await asyncio.to_thread(
            self.bot.send_message,
            int(job["telegram_chat_id"]),
            outgoing.text,
            job.get("telegram_thread_id"),
            parse_mode=outgoing.parse_mode,
            reply_markup=outgoing.reply_markup(),
            disable_link_preview=outgoing.disable_link_preview,
        )

    async def notify_deployment(self, job_id: str) -> None:
        job = self.db.get_code_job(job_id)
        if not job or job.get("deployment_status") not in {"succeeded", "failed"}:
            return
        succeeded = job["deployment_status"] == "succeeded"
        lines = [
            "✅ Deployment succeeded" if succeeded else "❌ Deployment failed",
            f"Code Job ID: {job['id']}",
            f"Repo: {job['repo']}",
        ]
        if job.get("deployment_merge_sha"):
            lines.append(f"Merge commit: {str(job['deployment_merge_sha'])[:12]}")
        if job.get("deployment_run_url"):
            lines.append(f"Deployment: {job['deployment_run_url']}")
        if job.get("deployment_error"):
            lines.append(f"Error: {job['deployment_error']}")
        lines.append(f"Status command: /code status {job['id']}")
        outgoing = outgoing_message("\n".join(lines), expandable_prefixes=("Error:",))
        await asyncio.to_thread(
            self.bot.send_message,
            int(job["telegram_chat_id"]),
            outgoing.text,
            job.get("telegram_thread_id"),
            parse_mode=outgoing.parse_mode,
            reply_markup=outgoing.reply_markup(),
            disable_link_preview=outgoing.disable_link_preview,
        )

    @staticmethod
    def render_message(job: dict[str, Any]) -> OutgoingMessage:
        return outgoing_message(
            CodeProgressReporter.render(job),
            expandable_prefixes=("Plan revision", "Deployment error:", "Error:"),
        )

    @staticmethod
    def render(job: dict[str, Any]) -> str:
        created = int(job.get("created_at") or time.time())
        elapsed = max(0, int(time.time()) - created)
        lines = [
            _status_heading(str(job["status"]), str(job.get("deployment_status") or "")),
            f"Code Job ID: {job['id']}",
            f"Issue: {job['repo']}#{job['issue_number']} — {job['issue_title']}",
            f"Issue link: https://github.com/{job['repo']}/issues/{job['issue_number']}",
            f"Status: {str(job['status']).replace('_', ' ')}",
            f"Elapsed: {elapsed // 60}m {elapsed % 60}s",
        ]
        if job.get("latest_activity"):
            lines.append(f"Activity: {job['latest_activity']}")
        plan_raw = job.get("plan_json")
        if isinstance(plan_raw, dict):
            try:
                plan = CodePlan.from_json(plan_raw)
                lines.extend(["", f"Plan revision {job.get('plan_revision') or 1}: {plan.summary}"])
                lines.extend(f"{index}. {step.title}" for index, step in enumerate(plan.steps, 1))
                if plan.questions:
                    lines.extend(["", "Open questions:", *(f"- {item}" for item in plan.questions)])
            except ValueError:
                pass
        if job.get("pull_request_url"):
            lines.append(f"Pull request: {job['pull_request_url']}")
        checks = job.get("ci_checks_json")
        if isinstance(checks, list):
            passed = sum(
                isinstance(item, dict) and item.get("bucket") in {"pass", "skipping"}
                for item in checks
            )
            pending = sum(
                isinstance(item, dict) and item.get("bucket") == "pending" for item in checks
            )
            failed = sum(
                isinstance(item, dict) and item.get("bucket") in {"fail", "cancel"}
                for item in checks
            )
            lines.append(
                f"CI checks: ✅ {passed}  ⏳ {pending}  ❌ {failed}"
            )
            attempts = int(job.get("ci_repair_attempts") or 0)
            if attempts:
                lines.append(f"CI repair attempts: {attempts}")
        if job.get("deployment_status"):
            lines.append(f"Deployment: {str(job['deployment_status']).replace('_', ' ')}")
        if job.get("deployment_merge_sha"):
            lines.append(f"Merge commit: {str(job['deployment_merge_sha'])[:12]}")
        if job.get("deployment_run_url"):
            lines.append(f"Deployment run: {job['deployment_run_url']}")
        if job.get("deployment_error"):
            lines.extend(["", f"Deployment error: {job['deployment_error']}"])
        if job.get("error"):
            lines.extend(["", f"Error: {job['error']}"])
        status = str(job["status"])
        if status == "awaiting_approval":
            lines.extend(
                [
                    "",
                    "Reply with feedback, approve, or discard; or run:",
                    f"/code approve {job['id']}",
                    f"/code edit {job['id']} <feedback>",
                    f"/code discard {job['id']}",
                ]
            )
        elif status in {"failed", "interrupted"}:
            lines.extend(["", f"Retry: /code retry {job['id']}", f"Discard: /code discard {job['id']}"])
        elif status == "ready" and not job.get("deployment_merge_sha"):
            lines.extend(
                [
                    "",
                    f"Rebase onto latest base: /code rebase {job['id']}",
                    f"Deploy: /deploy {job['id']}",
                ]
            )
        return truncate("\n".join(lines), 4096)


def _status_heading(status: str, deployment_status: str) -> str:
    if deployment_status == "succeeded":
        return "✅ Codex code job"
    if deployment_status == "failed" or status == "failed":
        return "❌ Codex code job"
    if deployment_status:
        return "⚙️ Codex code job"
    if status == "interrupted":
        return "⚠️ Codex code job"
    if status == "awaiting_approval":
        return "⏸️ Codex code job"
    if status == "waiting_checks":
        return "🧪 Codex code job"
    if status == "ready":
        return "✅ Codex code job"
    return "🧭 Codex code job"


def _event_summary(event: dict[str, Any]) -> str:
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
