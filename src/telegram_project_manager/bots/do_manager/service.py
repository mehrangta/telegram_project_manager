from __future__ import annotations

import asyncio
import logging
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from openai_codex.types import ReasoningEffort

from telegram_project_manager.bots.ask_manager.images import stage_attachments, validate_attachments
from telegram_project_manager.bots.code_manager.codex_sdk import (
    CODEX_JOB_SANDBOX,
    CodexSdkAdapter,
    CodexSdkError,
)
from telegram_project_manager.bots.do_manager.progress import DoProgressReporter
from telegram_project_manager.bots.do_manager.prompts import do_developer_instructions
from telegram_project_manager.bots.do_manager.workspace import DoWorkspaceService
from telegram_project_manager.integrations.git.local_repository import LocalRepositoryError
from telegram_project_manager.platform.responses import outgoing_message
from telegram_project_manager.platform.router import IncomingAttachment
from telegram_project_manager.platform.storage.db import Database
from telegram_project_manager.platform.telegram_bot import TelegramBotApi, TelegramBotApiError


DO_TIMEOUT_SECONDS = 10 * 60 * 60
MAX_OUTSTANDING_DO_JOBS = 10
MAX_CONCURRENT_DO_JOBS = 2
DO_QUEUE_REQUEST_MAX_LENGTH = 120
DO_JOB_ID_RE = re.compile(r"^d-[0-9a-f]{8}$")


class DoService:
    def __init__(
        self,
        *,
        db: Database,
        codex: CodexSdkAdapter,
        bot: TelegramBotApi,
        reporter: DoProgressReporter,
        workspaces: DoWorkspaceService,
        host_working_directory: Path,
        payload_root: Path,
        max_outstanding: int = MAX_OUTSTANDING_DO_JOBS,
        max_concurrent: int = MAX_CONCURRENT_DO_JOBS,
    ) -> None:
        self.db = db
        self.codex = codex
        self.bot = bot
        self.reporter = reporter
        self.workspaces = workspaces
        self.host_working_directory = host_working_directory.resolve()
        self.payload_root = payload_root.resolve()
        self.max_outstanding = max_outstanding
        self.max_concurrent = max_concurrent
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._active_lanes: set[str] = set()

    def validate_repo(self, *, source_path: str, repo: str) -> str:
        return self.workspaces.validate_source(source_path=source_path, repo=repo)

    async def submit(
        self,
        *,
        chat_id: int,
        user_id: int,
        thread_id: int | None,
        message_id: int | None,
        mode: str,
        job: str,
        repo: str = "",
        branch: str = "main",
        source_path: str = "",
        attachments: tuple[IncomingAttachment, ...] = (),
    ) -> str:
        if self.db.count_active_do_jobs() >= self.max_outstanding:
            raise ValueError("Full-access job queue is full. Try again after another job completes.")
        validate_attachments(attachments)
        if mode not in {"repo", "host"}:
            raise ValueError("Unsupported do job mode.")
        if mode == "repo":
            source_path = self.validate_repo(source_path=source_path, repo=repo)
        do_id = f"d-{uuid.uuid4().hex[:8]}"
        lane = repo.lower() if mode == "repo" else "__host__"
        payload = self.payload_root / do_id
        payload.mkdir(parents=True, exist_ok=False)
        payload.chmod(0o700)
        request_path = payload / "request.txt"
        request_path.write_text(job, encoding="utf-8")
        request_path.chmod(0o600)
        image_paths: tuple[str, ...] = ()
        try:
            if attachments:
                image_paths = await asyncio.to_thread(
                    stage_attachments,
                    bot=self.bot,
                    attachments=attachments,
                    destination=payload / "images",
                )
            workspace_path = (
                str(self.workspaces.root / repo.lower().replace("/", "--"))
                if mode == "repo"
                else str(self.host_working_directory)
            )
            self.db.create_do_job(
                {
                    "id": do_id,
                    "telegram_chat_id": chat_id,
                    "telegram_user_id": user_id,
                    "telegram_thread_id": thread_id,
                    "mode": mode,
                    "lane": lane,
                    "repo": repo or None,
                    "default_branch": branch if mode == "repo" else None,
                    "source_repo_path": source_path or None,
                    "workspace_path": workspace_path,
                    "payload_path": str(payload),
                    "image_count": len(image_paths),
                    "status": "preparing",
                    "latest_activity": "Preparing secure job payload",
                }
            )
            await self.reporter.create(do_id)
            self.db.update_do_job(
                do_id,
                {"status": "queued", "latest_activity": "Waiting for do worker"},
                allowed_statuses=("preparing",),
            )
            await self.reporter.refresh(do_id, force=True)
            self.db.audit(
                "do.execute", "queued",
                {"actor": user_id, "chat_id": chat_id, "repo": repo, "mode": mode, "images": len(image_paths)},
                do_id,
            )
            return do_id
        except Exception:
            job_row = self.db.get_do_job(do_id)
            if job_row:
                self.db.update_do_job(
                    do_id,
                    {"status": "failed", "error": "Job submission failed."},
                    allowed_statuses=("preparing",),
                )
            await asyncio.to_thread(shutil.rmtree, payload, True)
            raise

    def status(self, *, chat_id: int, thread_id: int | None, job_id: str = "") -> str:
        if job_id:
            job = self.db.get_do_job(job_id)
            if not job:
                raise ValueError("Do job not found.")
            if int(job["telegram_chat_id"]) != chat_id or job.get("telegram_thread_id") != thread_id:
                raise ValueError("Do job belongs to a different chat or topic.")
        else:
            jobs = self.db.list_do_jobs(
                chat_id=chat_id, thread_id=thread_id, exact_thread=True, limit=1
            )
            if not jobs:
                raise ValueError("No do jobs found for this chat or topic.")
            job = jobs[0]
        return DoProgressReporter.render(
            job, self.db.list_do_job_events(str(job["id"]), limit=5)
        )

    def queue_snapshot(
        self, *, chat_id: int, thread_id: int | None
    ) -> dict[str, tuple[dict[str, Any], ...]]:
        jobs = list(
            reversed(
                self.db.list_do_jobs(
                    chat_id=chat_id,
                    thread_id=thread_id,
                    exact_thread=True,
                    statuses=("preparing", "queued", "running"),
                    limit=100,
                )
            )
        )
        return {
            "running": tuple(
                self._queue_item(job) for job in jobs if job["status"] == "running"
            ),
            "queued": tuple(
                self._queue_item(job)
                for job in jobs
                if job["status"] in {"preparing", "queued"}
            ),
        }

    def _queue_item(self, job: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(job["id"]),
            "mode": str(job["mode"]),
            "repo": str(job.get("repo") or ""),
            "branch": str(job.get("default_branch") or ""),
            "status": str(job["status"]),
            "image_count": int(job.get("image_count") or 0),
            "request": self._request_preview(str(job["id"])),
            "created_at": int(job["created_at"]),
            "updated_at": int(job["updated_at"]),
        }

    def _request_preview(self, job_id: str) -> str:
        if not DO_JOB_ID_RE.fullmatch(job_id):
            return "request unavailable"
        payload = self.payload_root / job_id
        request = payload / "request.txt"
        try:
            if payload.is_symlink() or request.is_symlink():
                return "request unavailable"
            resolved = request.resolve(strict=True)
            if not resolved.is_relative_to(self.payload_root) or not resolved.is_file():
                return "request unavailable"
            normalized = _redact(" ".join(resolved.read_text(encoding="utf-8").split()))
        except (OSError, UnicodeError):
            return "request unavailable"
        if len(normalized) <= DO_QUEUE_REQUEST_MAX_LENGTH:
            return normalized
        return normalized[: DO_QUEUE_REQUEST_MAX_LENGTH - 1].rstrip() + "…"

    async def run_worker(self) -> None:
        for job_id in self.db.mark_running_do_jobs_interrupted():
            await self.reporter.refresh(job_id, force=True)
        try:
            while True:
                self._reap_finished()
                available = self.max_concurrent - len(self._tasks)
                if available > 0:
                    queued = list(reversed(self.db.list_do_jobs(statuses=("queued",), limit=100)))
                    for job in queued:
                        lane = str(job["lane"])
                        if available <= 0:
                            break
                        if lane in self._active_lanes or not self.db.claim_do_job(str(job["id"])):
                            continue
                        job_id = str(job["id"])
                        self._active_lanes.add(lane)
                        task = asyncio.create_task(self._execute(job_id), name=f"do-worker-{job_id}")
                        self._tasks[job_id] = task
                        available -= 1
                await asyncio.sleep(0.5)
        finally:
            active = tuple(self._tasks.items())
            for job_id, _ in active:
                await self.codex.interrupt(job_id)
                self.db.update_do_job(
                    job_id,
                    {"status": "interrupted", "error": "Do worker stopped during execution."},
                    allowed_statuses=("running",),
                )
            for _, task in active:
                task.cancel()
            if active:
                await asyncio.gather(*(task for _, task in active), return_exceptions=True)
            self._tasks.clear()
            self._active_lanes.clear()

    def _reap_finished(self) -> None:
        for job_id, task in tuple(self._tasks.items()):
            if not task.done():
                continue
            job = self.db.get_do_job(job_id)
            if job:
                self._active_lanes.discard(str(job["lane"]))
            self._tasks.pop(job_id, None)
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logging.exception("Unhandled do worker task failure %s", job_id)

    async def _execute(self, do_id: str) -> None:
        job = self.db.get_do_job(do_id)
        if not job:
            return
        try:
            await self.reporter.refresh(do_id, force=True)
            if job["mode"] == "repo":
                workspace = await asyncio.to_thread(
                    self.workspaces.prepare,
                    source_path=str(job.get("source_repo_path") or ""),
                    repo=str(job.get("repo") or ""),
                    branch=str(job.get("default_branch") or "main"),
                )
            else:
                workspace = self.host_working_directory
            self.db.update_do_job(
                do_id,
                {"workspace_path": str(workspace), "latest_activity": "Codex starting"},
                allowed_statuses=("running",),
            )
            payload = Path(str(job["payload_path"])).resolve()
            request = (payload / "request.txt").read_text(encoding="utf-8")
            image_paths = tuple(str(path.resolve()) for path in sorted((payload / "images").glob("*")))
            _, result = await self.codex.run_text_turn(
                job_id=do_id,
                cwd=str(workspace),
                prompt=request,
                image_paths=image_paths,
                sandbox=CODEX_JOB_SANDBOX,
                effort=ReasoningEffort.high,
                model_role="code",
                developer_instructions=do_developer_instructions(
                    mode=str(job["mode"]), repo=str(job.get("repo") or ""), job_id=do_id
                ),
                thread_id=None,
                timeout_seconds=DO_TIMEOUT_SECONDS,
                on_progress=lambda event: self.reporter.activity(do_id, event),
                on_thread=_ignore_thread,
            )
            self.db.update_do_job(
                do_id,
                {"status": "completed", "latest_activity": "Codex completed"},
                allowed_statuses=("running",),
            )
            self.db.audit("do.execute", "ok", _audit_details(job), do_id)
            await self.reporter.refresh(do_id, force=True)
            await self._send_result(job, f"Full-access job completed.\n\n{_redact(result)}")
        except asyncio.CancelledError:
            raise
        except (CodexSdkError, LocalRepositoryError, OSError, ValueError) as exc:
            await self._fail(do_id, job, exc)
        except Exception as exc:
            logging.exception("Unexpected do worker failure %s", do_id)
            await self._fail(do_id, job, exc)
        finally:
            await self._cleanup_payload(job)

    async def _fail(self, do_id: str, job: dict[str, Any], exc: BaseException) -> None:
        error = _safe_error(exc)
        self.db.update_do_job(
            do_id,
            {"status": "failed", "error": error, "latest_activity": "Job failed"},
            allowed_statuses=("running",),
        )
        self.db.audit("do.execute", "failed", {**_audit_details(job), "error": error}, do_id)
        await self.reporter.refresh(do_id, force=True)
        await self._send_result(job, f"Full-access job failed.\nReason: {error}")

    async def _send_result(self, job: dict[str, Any], text: str) -> None:
        outgoing = outgoing_message(text, reply_to_message_id=job.get("telegram_message_id"))
        try:
            await asyncio.to_thread(
                self.bot.send_message,
                int(job["telegram_chat_id"]), outgoing.text, job.get("telegram_thread_id"),
                parse_mode=outgoing.parse_mode,
                reply_markup=outgoing.reply_markup(),
                disable_link_preview=outgoing.disable_link_preview,
                reply_to_message_id=outgoing.reply_to_message_id,
            )
        except TelegramBotApiError as exc:
            logging.exception("Failed to send do job result %s", job["id"])
            self.db.audit("do.reply", "failed", {"error": _safe_error(exc)}, str(job["id"]))

    async def _cleanup_payload(self, job: dict[str, Any]) -> None:
        payload = Path(str(job["payload_path"])).resolve()
        if payload != self.payload_root and payload.is_relative_to(self.payload_root):
            await asyncio.to_thread(shutil.rmtree, payload, True)


async def _ignore_thread(thread_id: str) -> None:
    del thread_id


def _audit_details(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "actor": int(job["telegram_user_id"]),
        "chat_id": int(job["telegram_chat_id"]),
        "repo": str(job.get("repo") or ""),
        "mode": str(job["mode"]),
        "images": int(job.get("image_count") or 0),
    }


def _redact(value: str) -> str:
    value = re.sub(r"sk-[A-Za-z0-9_-]+(?:\*+[A-Za-z0-9_-]+)?", "[REDACTED_API_KEY]", value)
    return re.sub(r"(?i)(authorization:\s*bearer\s+)\S+", r"\1[REDACTED]", value)


def _safe_error(exc: BaseException) -> str:
    return " ".join(_redact(str(exc)).split())[:1000] or exc.__class__.__name__
