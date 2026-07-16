from __future__ import annotations

import asyncio
import logging
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
        self._plan_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

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
        if (
            job["status"] == "ready"
            and not job.get("deployment_merge_sha")
        ):
            lines.append(f"Merge: /merge {job['id']}")
            if (
                str(job.get("base_branch") or "") == "main"
                and self.db.is_repo_deploy_enabled(str(job["repo"]))
            ):
                lines.append(f"Deploy: /deploy {job['id']}")
        elif job["status"] == "failed":
            phase = str(job.get("resume_phase") or "unknown").replace("_", " ")
            lines.append(f"Failure phase: {phase}")
            if job.get("error"):
                lines.append(f"Error: {job['error']}")
            lines.extend(
                [
                    f"Retry: /code retry {job['id']}",
                    f"Status: /code status {job['id']}",
                ]
            )
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

    async def notify_plan_ready(self, job_id: str) -> None:
        job = self.db.get_code_job(job_id)
        if not job or job["status"] not in {"awaiting_clarification", "awaiting_approval"}:
            return
        plan_raw = job.get("plan_json")
        plan = CodePlan.from_json(plan_raw) if isinstance(plan_raw, dict) else None
        needs_answers = bool(plan and plan.questions)
        lines = [
            "❓ PR plan needs your answers" if needs_answers else "📝 PR plan ready for approval",
            f"Code Job ID: {job['id']}",
            f"Issue: {job['repo']}#{job['issue_number']}",
            f"Plan revision: {job.get('plan_revision') or 1}",
        ]
        if job.get("pull_request_url"):
            lines.append(f"Pull request: {job['pull_request_url']}")
        if needs_answers and plan:
            lines.extend(["", "Reply to this message with answers:"])
            for index, question in enumerate(plan.questions, 1):
                lines.extend(question.render(index))
            lines.extend(["", "Approval is blocked until all questions are resolved."])
        else:
            lines.append(f"Approve: /code approve {job['id']}")
        lines.extend(
            [
                f"Edit: /code edit {job['id']} <feedback>",
                f"Discard: /code discard {job['id']}",
            ]
        )
        outgoing = outgoing_message("\n".join(lines))
        async with self._plan_locks[job_id]:
            current = self.db.get_code_job(job_id)
            if not current or current["status"] not in {
                "awaiting_clarification", "awaiting_approval"
            }:
                return
            existing_id = current.get("telegram_plan_message_id")
            if existing_id and not await self._delete_plan_message(current):
                await asyncio.to_thread(
                    self.bot.edit_message_text,
                    int(current["telegram_chat_id"]),
                    int(existing_id),
                    outgoing.text,
                    parse_mode=outgoing.parse_mode,
                    reply_markup=outgoing.reply_markup(include_empty=True),
                    disable_link_preview=outgoing.disable_link_preview,
                )
                return
            result = await asyncio.to_thread(
                self.bot.send_message,
                int(current["telegram_chat_id"]),
                outgoing.text,
                current.get("telegram_thread_id"),
                parse_mode=outgoing.parse_mode,
                reply_markup=outgoing.reply_markup(),
                disable_link_preview=outgoing.disable_link_preview,
            )
            self.db.update_code_job(
                job_id, {"telegram_plan_message_id": int(result["message_id"])}
            )

    async def dismiss_plan_ready(self, job_id: str) -> bool:
        async with self._plan_locks[job_id]:
            job = self.db.get_code_job(job_id)
            if not job or not job.get("telegram_plan_message_id"):
                return True
            return await self._delete_plan_message(job)

    async def _delete_plan_message(self, job: dict[str, Any]) -> bool:
        message_id = int(job["telegram_plan_message_id"])
        try:
            await asyncio.to_thread(
                self.bot.delete_message,
                int(job["telegram_chat_id"]),
                message_id,
            )
        except TelegramBotApiError as exc:
            if "message to delete not found" not in str(exc).lower():
                logging.warning(
                    "Failed to delete plan notification for %s: %s", job["id"], exc
                )
                return False
        self.db.update_code_job(str(job["id"]), {"telegram_plan_message_id": None})
        return True

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

    async def notify_merge(self, job_id: str) -> None:
        job = self.db.get_code_job(job_id)
        if (
            not job
            or job.get("deployment_mode") != "merge"
            or job.get("deployment_status") not in {"merged", "failed"}
        ):
            return
        merged = job["deployment_status"] == "merged"
        lines = [
            "✅ Pull request merged" if merged else "❌ Pull request merge failed",
            f"Code Job ID: {job['id']}",
            f"Repo: {job['repo']}",
        ]
        if job.get("deployment_merge_sha"):
            lines.append(f"Merge commit: {str(job['deployment_merge_sha'])[:12]}")
        if job.get("deployment_error"):
            lines.append(f"Error: {job['deployment_error']}")
        if (
            merged
            and str(job.get("base_branch") or "") == "main"
            and self.db.is_repo_deploy_enabled(str(job["repo"]))
        ):
            lines.append(f"Deploy: /deploy {job['id']}")
        elif not merged:
            lines.append(f"Merge: /merge {job['id']}")
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

    def render_message(self, job: dict[str, Any]) -> OutgoingMessage:
        events = self.db.list_code_job_events(str(job["id"]), limit=5)
        return outgoing_message(
            CodeProgressReporter.render(
                job,
                events,
                deploy_enabled=self.db.is_repo_deploy_enabled(str(job["repo"])),
            ),
            expandable_prefixes=(
                "Recent activity:", "Plan revision", "Merge error:",
                "Deployment error:", "Error:"
            ),
        )

    @staticmethod
    def render(
        job: dict[str, Any],
        events: list[dict[str, Any]] | None = None,
        *,
        deploy_enabled: bool = False,
    ) -> str:
        created = int(job.get("created_at") or time.time())
        elapsed = max(0, int(time.time()) - created)
        lines = [
            _status_heading(
                str(job["status"]),
                str(job.get("deployment_status") or ""),
                str(job.get("deployment_mode") or ""),
            ),
            f"Code Job ID: {job['id']}",
            f"Issue: {job['repo']}#{job['issue_number']} — {job['issue_title']}",
            f"Issue link: https://github.com/{job['repo']}/issues/{job['issue_number']}",
            f"Status: {str(job['status']).replace('_', ' ')}",
            f"Elapsed: {elapsed // 60}m {elapsed % 60}s",
        ]
        if job.get("latest_activity"):
            lines.append(f"Activity: {job['latest_activity']}")
        if str(job.get("status") or "") == "failed":
            phase = str(job.get("resume_phase") or "unknown").replace("_", " ")
            failed_at = max(0, int(job.get("updated_at") or time.time()) - created)
            lines.extend(
                [
                    f"Failure phase: {phase}",
                    f"Failed after: {_duration(failed_at)}",
                ]
            )
        timeline = _recent_activity(events or [], created)
        if timeline:
            lines.extend(["", "Recent activity:", *timeline])
        plan_raw = job.get("plan_json")
        if isinstance(plan_raw, dict):
            try:
                plan = CodePlan.from_json(plan_raw)
                lines.extend(["", f"Plan revision {job.get('plan_revision') or 1}: {plan.summary}"])
                lines.extend(f"{index}. {step.title}" for index, step in enumerate(plan.steps, 1))
                if plan.questions:
                    lines.extend(["", "Open questions:"])
                    for index, question in enumerate(plan.questions, 1):
                        lines.extend(question.render(index))
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
        operation_mode = str(job.get("deployment_mode") or "")
        operation_status = str(job.get("deployment_status") or "")
        if operation_status:
            label = "Merge" if operation_mode == "merge" else "Deployment"
            lines.append(f"{label}: {operation_status.replace('_', ' ')}")
        if job.get("deployment_merge_sha"):
            lines.append(f"Merge commit: {str(job['deployment_merge_sha'])[:12]}")
        if job.get("deployment_run_url"):
            lines.append(f"Deployment run: {job['deployment_run_url']}")
        if job.get("deployment_error"):
            label = "Merge error" if operation_mode == "merge" else "Deployment error"
            lines.extend(["", f"{label}: {job['deployment_error']}"])
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
        elif status == "awaiting_clarification":
            lines.extend(
                [
                    "",
                    "Reply with answers or additional feedback; approval is blocked.",
                    f"/code edit {job['id']} <answers>",
                    f"/code discard {job['id']}",
                ]
            )
        elif status in {"failed", "interrupted"}:
            lines.extend(["", f"Retry: /code retry {job['id']}", f"Discard: /code discard {job['id']}"])
        elif status == "ready" and operation_status not in {
            "queued", "merging", "waiting_workflow", "dispatching", "deploying"
        }:
            if not job.get("deployment_merge_sha"):
                lines.extend(
                    [
                        "",
                        f"Rebase onto latest base: /code rebase {job['id']}",
                        f"Merge: /merge {job['id']}",
                    ]
                )
                if deploy_enabled and str(job.get("base_branch") or "") == "main":
                    lines.append(f"Deploy: /deploy {job['id']}")
            elif (
                deploy_enabled
                and str(job.get("base_branch") or "") == "main"
                and operation_status in {"merged", "failed"}
            ):
                lines.extend(["", f"Deploy: /deploy {job['id']}"])
        return truncate("\n".join(lines), 4096)


def _status_heading(status: str, deployment_status: str, deployment_mode: str = "") -> str:
    if deployment_status == "succeeded":
        return "✅ Codex code job"
    if deployment_status == "merged":
        return "✅ Codex code job"
    if deployment_status == "failed" or status == "failed":
        return "❌ Codex code job"
    if deployment_status:
        return "⚙️ Codex code job"
    if status == "interrupted":
        return "⚠️ Codex code job"
    if status in {"awaiting_clarification", "awaiting_approval"}:
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


def _recent_activity(events: list[dict[str, Any]], created_at: int) -> list[str]:
    items: list[str] = []
    previous = ""
    for event in events:
        summary = event.get("summary")
        text = str(summary.get("text") or "") if isinstance(summary, dict) else ""
        text = " ".join(text.split())[:500]
        if not text or text == previous:
            continue
        offset = max(0, int(event.get("created_at") or created_at) - created_at)
        items.append(f"- +{_duration(offset)}: {text}")
        previous = text
    return items


def _duration(seconds: int) -> str:
    minutes, seconds = divmod(max(0, seconds), 60)
    return f"{minutes}m {seconds}s"
