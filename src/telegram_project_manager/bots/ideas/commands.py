from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any, Callable

from telegram_project_manager.bots.code_manager.workspace import WorkspaceError
from telegram_project_manager.bots.commit_manager.schemas import PlanValidationError, validate_repo
from telegram_project_manager.bots.ideas.schedule import (
    format_interval,
    format_utc_time,
    next_run_at,
    parse_interval,
    parse_utc_time,
)
from telegram_project_manager.bots.ideas.service import BrainstormService
from telegram_project_manager.integrations.git.local_repository import LocalRepositoryError
from telegram_project_manager.platform.permissions import PermissionService
from telegram_project_manager.platform.responses import OutgoingMessage, outgoing_message
from telegram_project_manager.platform.router import IncomingMessage
from telegram_project_manager.platform.storage.db import Database
from telegram_project_manager.platform.telegram_bot import TelegramBotApiError


class BrainstormManager:
    def __init__(
        self,
        *,
        db: Database,
        service: BrainstormService,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.db = db
        self.service = service
        self.permissions = PermissionService(db)
        self.clock = clock or (lambda: int(time.time()))

    async def handle(self, message: IncomingMessage) -> str | OutgoingMessage | None:
        command, _, rest = message.text.strip().partition(" ")
        command = command.split("@", 1)[0].lower()
        if command == "/brainstorm":
            return await self._run_manual(message, rest)
        if command == "/repo":
            parts = rest.split()
            if parts and parts[0].lower() == "brainstorm":
                return await self._configure(message, parts[1:])
        return None

    async def _run_manual(
        self, message: IncomingMessage, rest: str
    ) -> str | OutgoingMessage | None:
        admin_error = self.permissions.require_admin(message.user_id)
        if admin_error:
            return admin_error
        if rest.strip():
            return outgoing_message(
                "Usage: /brainstorm", reply_to_message_id=message.message_id
            )
        repository = self._resolve_manual_repository(message)
        if isinstance(repository, OutgoingMessage):
            return repository
        repo = str(repository["repo"])
        if not self.db.is_repo_allowed(repo):
            return outgoing_message(
                "Active repo is not in allowed repo list. Admin: /repo allow owner/repository",
                reply_to_message_id=message.message_id,
            )
        config = self.db.get_brainstorm_config(repo)
        if not config or not config["enabled"]:
            return outgoing_message(
                f"Brainstorming is disabled for {repo}. Admin: /repo brainstorm enable {repo}",
                reply_to_message_id=message.message_id,
            )
        try:
            await self.service.submit(
                chat_id=message.chat_id,
                user_id=message.user_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
                repo=repo,
                branch=str(repository.get("default_branch") or "main"),
                source_path=str(repository.get("local_repo_path") or ""),
            )
        except (LocalRepositoryError, ValueError, WorkspaceError) as exc:
            return outgoing_message(
                f"Repository brainstorm not started.\nReason: {exc}",
                reply_to_message_id=message.message_id,
            )
        except TelegramBotApiError:
            raise
        return None

    def _resolve_manual_repository(
        self, message: IncomingMessage
    ) -> dict[str, Any] | OutgoingMessage:
        settings = self.db.get_scope_settings(message.chat_id, message.thread_id)
        active_repo = str(settings.get("active_repo") or "")
        if active_repo:
            return {
                "repo": active_repo,
                "default_branch": settings.get("default_branch"),
                "local_repo_path": settings.get("local_repo_path"),
            }

        scope = "topic" if message.thread_id is not None else "chat"
        return outgoing_message(
            f"No active repo for this {scope}. Admin: /repo set owner/repository",
            reply_to_message_id=message.message_id,
        )

    async def _configure(self, message: IncomingMessage, parts: list[str]) -> str:
        admin_error = self.permissions.require_admin(message.user_id)
        if admin_error:
            return admin_error
        if len(parts) == 2 and parts[0].lower() == "show":
            repo = parts[1]
            error = self._repo_error(repo)
            return error or self._show(repo)
        if len(parts) == 2 and parts[0].lower() == "disable":
            repo = parts[1]
            error = self._repo_error(repo)
            if error:
                return error
            self.db.disable_brainstorm(repo, message.user_id)
            return (
                f"Brainstorming disabled for {repo}. "
                "The saved schedule and destination were preserved."
            )
        if len(parts) == 4 and parts[0].lower() == "schedule":
            repo, interval_value, time_value = parts[1:]
            error = self._repo_error(repo)
            if error:
                return error
            try:
                interval_days = parse_interval(interval_value)
                run_minute = parse_utc_time(time_value)
            except ValueError as exc:
                return str(exc)
            next_at = next_run_at(self.clock(), interval_days, run_minute)
            self.db.configure_brainstorm_schedule(
                repo, interval_days, run_minute, next_at, message.user_id
            )
            config = self.db.get_brainstorm_config(repo) or {}
            status = (
                f"Next run: {_format_timestamp(int(config['next_run_at']))}"
                if config.get("next_run_at")
                else "Schedule will start when brainstorming is enabled."
            )
            return (
                f"Brainstorm schedule set for {repo}: "
                f"{format_interval(interval_days)} at {format_utc_time(run_minute)}.\n{status}"
            )
        if len(parts) == 2 and parts[0].lower() == "enable":
            repo = parts[1]
            error = self._repo_error(repo)
            if error:
                return error
            settings = self.db.get_scope_settings(message.chat_id, message.thread_id)
            if str(settings.get("active_repo") or "") != repo:
                scope = "topic" if message.thread_id is not None else "chat"
                return f"Set {repo} as the active repo for this {scope} before enabling brainstorming."
            branch = str(settings.get("default_branch") or "main")
            source_path = str(settings.get("local_repo_path") or "")
            if not source_path:
                return "Configure the active repository cache before enabling brainstorming."
            try:
                resolved_source = await asyncio.to_thread(
                    self.service.validate_source, source_path=source_path, repo=repo
                )
            except (LocalRepositoryError, ValueError, WorkspaceError) as exc:
                return f"Brainstorming not enabled.\nReason: {exc}"
            config = self.db.get_brainstorm_config(repo) or {}
            next_at = None
            if config.get("interval_days") is not None:
                next_at = next_run_at(
                    self.clock(),
                    int(config["interval_days"]),
                    int(config["run_at_minute_utc"]),
                )
            self.db.enable_brainstorm(
                repo,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                default_branch=branch,
                local_repo_path=resolved_source,
                next_run_at=next_at,
                user_id=message.user_id,
            )
            destination = _destination(message.chat_id, message.thread_id)
            schedule = (
                f" Next run: {_format_timestamp(next_at)}."
                if next_at is not None
                else " No scheduled cadence is configured; manual runs are available."
            )
            return f"Brainstorming enabled for {repo}. Destination: {destination}.{schedule}"
        return (
            "Usage: /repo brainstorm show owner/repository | "
            "/repo brainstorm schedule owner/repository <daily|weekly|Nd> <HH:MM> | "
            "/repo brainstorm enable owner/repository | "
            "/repo brainstorm disable owner/repository"
        )

    def _repo_error(self, repo: str) -> str | None:
        try:
            validate_repo(repo)
        except PlanValidationError as exc:
            return str(exc)
        if not self.db.is_repo_allowed(repo):
            return "Repo is not allowed. Admin must run: /repo allow owner/repository"
        return None

    def _show(self, repo: str) -> str:
        config = self.db.get_brainstorm_config(repo)
        if not config:
            return "\n".join(
                [
                    f"Brainstorm repo: {repo}",
                    "Enabled: no",
                    "Schedule: not set",
                    "Destination: not set",
                    "Last run: never",
                ]
            )
        schedule = "not set"
        if config.get("interval_days") is not None:
            schedule = (
                f"{format_interval(int(config['interval_days']))} at "
                f"{format_utc_time(int(config['run_at_minute_utc']))}"
            )
        next_run = (
            _format_timestamp(int(config["next_run_at"]))
            if config.get("next_run_at") is not None
            else "not scheduled"
        )
        destination = (
            _destination(int(config["telegram_chat_id"]), config.get("telegram_thread_id"))
            if config.get("telegram_chat_id") is not None
            else "not set"
        )
        last_run = (
            _format_timestamp(int(config["last_run_at"]))
            if config.get("last_run_at") is not None
            else "never"
        )
        last_status = str(config.get("last_status") or "not run")
        if config.get("last_error"):
            last_status += f" ({config['last_error']})"
        return "\n".join(
            [
                f"Brainstorm repo: {repo}",
                f"Enabled: {'yes' if config['enabled'] else 'no'}",
                f"Schedule: {schedule}",
                f"Next run: {next_run}",
                f"Destination: {destination}",
                f"Branch: {config.get('default_branch') or 'not set'}",
                f"Local repo: {config.get('local_repo_path') or 'not set'}",
                f"Last run: {last_run}",
                f"Last status: {last_status}",
            ]
        )


def _destination(chat_id: int, thread_id: int | None) -> str:
    if thread_id is None:
        return f"chat {chat_id}"
    return f"chat {chat_id}, topic {thread_id}"


def _format_timestamp(value: int) -> str:
    return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%d %H:%M UTC")
