from __future__ import annotations

import asyncio
import logging
import re
import shutil
import uuid
from pathlib import Path

from telegram_project_manager.bots.commit_manager.schemas import (
    PlanValidationError,
    validate_repo,
)
from telegram_project_manager.integrations.gh.runner import GhError, GhRunner
from telegram_project_manager.integrations.git.local_repository import (
    LocalRepositoryError,
    LocalRepositoryService,
)
from telegram_project_manager.platform.router import IncomingMessage
from telegram_project_manager.platform.storage.db import Database
from telegram_project_manager.platform.telegram_bot import TelegramBotApi


CLONE_TIMEOUT_SECONDS = 900
_URL_CREDENTIALS = re.compile(r"(https?://)[^/@\s]+@", re.IGNORECASE)


class RepositorySetupError(RuntimeError):
    pass


class RepositorySetupService:
    """Builds managed Git caches without blocking Telegram polling."""

    def __init__(
        self,
        *,
        db: Database,
        gh: GhRunner,
        repositories: LocalRepositoryService,
        bot: TelegramBotApi,
    ) -> None:
        self.db = db
        self.gh = gh
        self.repositories = repositories
        self.bot = bot
        self.cache_root = (db.path.parent / "repos").resolve()
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def start(self, message: IncomingMessage, repo: str) -> str:
        try:
            validate_repo(repo)
        except PlanValidationError as exc:
            return str(exc)
        key = repo.lower()
        active = self._tasks.get(key)
        if active is not None and not active.done():
            return f"Repository setup already in progress: {repo}"
        task = asyncio.create_task(
            self._run(message, repo),
            name=f"repo-setup-{repo.replace('/', '--')}",
        )
        self._tasks[key] = task
        task.add_done_callback(lambda completed: self._remove_task(key, completed))
        return "\n".join(
            [
                "Repository setup started.",
                f"Repo: {repo}",
                "The bot will reply when the managed cache is ready.",
            ]
        )

    async def shutdown(self) -> None:
        tasks = [task for task in self._tasks.values() if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _remove_task(self, key: str, completed: asyncio.Task[None]) -> None:
        if self._tasks.get(key) is completed:
            self._tasks.pop(key, None)

    async def _run(self, message: IncomingMessage, requested_repo: str) -> None:
        try:
            repo, default_branch = await self._repository_metadata(requested_repo)
            path, reused = await self._prepare_cache(repo, default_branch)
            self.db.complete_repo_setup(
                message.chat_id,
                message.thread_id,
                repo,
                default_branch,
                str(path),
                message.user_id,
            )
            self.db.audit(
                "repo.setup",
                "ok",
                {"repo": repo, "path": str(path), "reused": reused},
            )
            result = "\n".join(
                [
                    "Repository setup complete.",
                    f"Repo: {repo}",
                    f"Default branch: {default_branch}",
                    f"Local cache: {path}",
                    f"Cache: {'reused and refreshed' if reused else 'downloaded'}",
                ]
            )
        except (GhError, LocalRepositoryError, OSError, RepositorySetupError) as exc:
            reason = _safe_error(exc)
            self.db.audit(
                "repo.setup",
                "failed",
                {"repo": requested_repo, "error": reason},
            )
            result = "\n".join(
                [
                    "Repository setup failed.",
                    f"Repo: {requested_repo}",
                    f"Reason: {reason}",
                ]
            )
        except Exception as exc:
            logging.exception("Unexpected repository setup failure for %s", requested_repo)
            reason = _safe_error(exc)
            self.db.audit(
                "repo.setup",
                "failed",
                {"repo": requested_repo, "error": reason},
            )
            result = "\n".join(
                [
                    "Repository setup failed.",
                    f"Repo: {requested_repo}",
                    f"Reason: {reason}",
                ]
            )
        try:
            await asyncio.to_thread(
                self.bot.send_message,
                message.chat_id,
                result,
                message.thread_id,
                reply_to_message_id=message.message_id,
            )
        except Exception:
            logging.exception("Failed to send repository setup result for %s", requested_repo)

    async def _repository_metadata(self, requested_repo: str) -> tuple[str, str]:
        result = await asyncio.to_thread(
            self.gh.run,
            ["repo", "view", requested_repo, "--json", "nameWithOwner,defaultBranchRef"],
        )
        try:
            payload = result.json()
        except (TypeError, ValueError) as exc:
            raise RepositorySetupError("GitHub returned invalid repository metadata.") from exc
        if not isinstance(payload, dict):
            raise RepositorySetupError("GitHub returned invalid repository metadata.")
        repo = str(payload.get("nameWithOwner") or "").strip()
        branch_value = payload.get("defaultBranchRef")
        branch = (
            str(branch_value.get("name") or "").strip()
            if isinstance(branch_value, dict)
            else ""
        )
        try:
            validate_repo(repo)
        except PlanValidationError as exc:
            raise RepositorySetupError("GitHub returned an invalid repository name.") from exc
        if not branch:
            raise RepositorySetupError("Repository has no default branch.")
        return repo, branch

    async def _prepare_cache(self, repo: str, default_branch: str) -> tuple[Path, bool]:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        destination = self.cache_root / f"{repo.replace('/', '--')}.git"
        if destination.is_symlink():
            raise RepositorySetupError(
                f"Managed cache destination is a symbolic link: {destination}"
            )
        if destination.exists():
            path = await asyncio.to_thread(self.repositories.validate, destination, repo)
            await asyncio.to_thread(self.repositories.fetch, path, default_branch)
            return path, True

        temporary = self.cache_root / (
            f".{repo.replace('/', '--')}.git.tmp-{uuid.uuid4().hex}"
        )
        try:
            await asyncio.to_thread(
                self.gh.run,
                [
                    "repo",
                    "clone",
                    repo,
                    str(temporary),
                    "--",
                    "--bare",
                ],
                timeout_seconds=CLONE_TIMEOUT_SECONDS,
            )
            path = await asyncio.to_thread(self.repositories.validate, temporary, repo)
            await asyncio.to_thread(self.repositories.fetch, path, default_branch)
            temporary.rename(destination)
            return destination.resolve(), False
        finally:
            if temporary.exists():
                await asyncio.to_thread(shutil.rmtree, temporary, True)


def _safe_error(exc: BaseException) -> str:
    value = str(exc).strip() or exc.__class__.__name__
    return _URL_CREDENTIALS.sub(r"\1***@", value)[:1000]
