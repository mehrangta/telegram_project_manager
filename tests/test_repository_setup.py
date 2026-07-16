import json
import tempfile
import unittest
from pathlib import Path

from telegram_project_manager.bots.commit_manager.commands import CommitManager
from telegram_project_manager.bots.commit_manager.repository_setup import (
    CLONE_TIMEOUT_SECONDS,
    RepositorySetupService,
)
from telegram_project_manager.integrations.gh.runner import GhError, GhResult
from telegram_project_manager.integrations.git.local_repository import LocalRepositoryError
from telegram_project_manager.platform.permissions import PermissionService
from telegram_project_manager.platform.router import IncomingMessage
from telegram_project_manager.platform.storage.db import Database


class FakeGh:
    def __init__(self, *, fail_clone: bool = False) -> None:
        self.fail_clone = fail_clone
        self.calls: list[tuple[list[str], int | None]] = []

    def run(
        self,
        args: list[str],
        input_json=None,
        check: bool = True,
        timeout_seconds: int | None = None,
    ) -> GhResult:
        del input_json, check
        self.calls.append((args, timeout_seconds))
        if args[:2] == ["repo", "view"]:
            payload = {
                "nameWithOwner": "Owner/Repo",
                "defaultBranchRef": {"name": "stable"},
            }
            return GhResult(args, 0, json.dumps(payload), "", 1)
        if args[:2] == ["repo", "clone"]:
            Path(args[3]).mkdir(parents=True)
            if self.fail_clone:
                raise GhError(GhResult(args, 1, "", "clone denied", 1))
            return GhResult(args, 0, "", "", 1)
        raise AssertionError(f"Unexpected gh args: {args}")


class FakeRepositories:
    def __init__(self, *, validation_error: str = "") -> None:
        self.validation_error = validation_error
        self.validations: list[tuple[Path, str]] = []
        self.fetches: list[tuple[Path, str]] = []

    def validate(self, path, repo):
        resolved = Path(path).resolve()
        self.validations.append((resolved, repo))
        if self.validation_error:
            raise LocalRepositoryError(self.validation_error)
        return resolved

    def fetch(self, path, branch):
        resolved = Path(path).resolve()
        self.fetches.append((resolved, branch))
        return resolved, "a" * 40


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, int | None, int | None]] = []

    def send_message(
        self,
        chat_id,
        text,
        message_thread_id=None,
        *,
        reply_to_message_id=None,
        **kwargs,
    ):
        del kwargs
        self.messages.append(
            (chat_id, text, message_thread_id, reply_to_message_id)
        )
        return {"message_id": len(self.messages)}


class RepositorySetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_clones_validates_and_transactionally_sets_topic_repository(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            gh = FakeGh()
            repositories = FakeRepositories()
            bot = FakeBot()
            service = RepositorySetupService(
                db=db, gh=gh, repositories=repositories, bot=bot
            )
            message = IncomingMessage(
                20,
                10,
                "admin",
                "/repo setup owner/repo",
                message_id=30,
                thread_id=40,
            )

            response = await service.start(message, "owner/repo")
            await service.shutdown()

            expected = (Path(temp_dir) / "repos" / "Owner--Repo.git").resolve()
            settings = db.get_scope_settings(20, 40)
            self.assertIn("setup started", response.lower())
            self.assertTrue(db.is_repo_allowed("Owner/Repo"))
            self.assertEqual(settings["active_repo"], "Owner/Repo")
            self.assertEqual(settings["default_branch"], "stable")
            self.assertEqual(settings["local_repo_path"], str(expected))
            self.assertTrue(expected.is_dir())
            clone = next(call for call in gh.calls if call[0][:2] == ["repo", "clone"])
            self.assertEqual(clone[1], CLONE_TIMEOUT_SECONDS)
            self.assertEqual(clone[0][:3], ["repo", "clone", "Owner/Repo"])
            self.assertEqual(clone[0][-2:], ["--", "--bare"])
            self.assertNotIn("--no-upstream", clone[0])
            self.assertEqual(repositories.fetches[0][1], "stable")
            self.assertIn("Cache: downloaded", bot.messages[0][1])
            self.assertEqual(bot.messages[0][2:], (40, 30))

    async def test_reuses_matching_existing_cache_without_clone(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            destination = Path(temp_dir) / "repos" / "Owner--Repo.git"
            destination.mkdir(parents=True)
            gh = FakeGh()
            repositories = FakeRepositories()
            bot = FakeBot()
            service = RepositorySetupService(
                db=db, gh=gh, repositories=repositories, bot=bot
            )

            await service.start(
                IncomingMessage(20, 10, "admin", "", message_id=30),
                "owner/repo",
            )
            await service.shutdown()

            self.assertFalse(any(args[:2] == ["repo", "clone"] for args, _ in gh.calls))
            self.assertEqual(repositories.fetches, [(destination.resolve(), "stable")])
            self.assertIn("reused and refreshed", bot.messages[0][1])

    async def test_invalid_existing_cache_is_untouched_and_settings_do_not_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.allow_repo("old/repo", 10)
            db.set_scope_repo(20, None, "old/repo", 10, "main")
            db.set_scope_local_repo(20, None, "/old/cache.git", 10)
            destination = Path(temp_dir) / "repos" / "Owner--Repo.git"
            destination.mkdir(parents=True)
            bot = FakeBot()
            service = RepositorySetupService(
                db=db,
                gh=FakeGh(),
                repositories=FakeRepositories(validation_error="origin mismatch"),
                bot=bot,
            )

            await service.start(IncomingMessage(20, 10, "admin", ""), "owner/repo")
            await service.shutdown()

            settings = db.get_scope_settings(20, None)
            self.assertTrue(destination.is_dir())
            self.assertEqual(settings["active_repo"], "old/repo")
            self.assertEqual(settings["local_repo_path"], "/old/cache.git")
            self.assertFalse(db.is_repo_allowed("Owner/Repo"))
            self.assertIn("origin mismatch", bot.messages[0][1])

    async def test_failed_clone_removes_temporary_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            bot = FakeBot()
            service = RepositorySetupService(
                db=db,
                gh=FakeGh(fail_clone=True),
                repositories=FakeRepositories(),
                bot=bot,
            )

            await service.start(IncomingMessage(20, 10, "admin", ""), "owner/repo")
            await service.shutdown()

            cache_root = Path(temp_dir) / "repos"
            self.assertEqual(list(cache_root.iterdir()), [])
            self.assertFalse(db.is_repo_allowed("Owner/Repo"))
            self.assertIn("clone denied", bot.messages[0][1])

    async def test_duplicate_setup_is_rejected_while_first_task_is_active(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            service = RepositorySetupService(
                db=db,
                gh=FakeGh(),
                repositories=FakeRepositories(),
                bot=FakeBot(),
            )
            message = IncomingMessage(20, 10, "admin", "")

            first = await service.start(message, "owner/repo")
            duplicate = await service.start(message, "OWNER/REPO")
            await service.shutdown()

            self.assertIn("setup started", first.lower())
            self.assertIn("already in progress", duplicate)


class RepositorySetupCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_repo_setup_dispatches_to_background_service(self):
        class Setup:
            def __init__(self):
                self.calls = []

            async def start(self, message, repo):
                self.calls.append((message.chat_id, message.thread_id, repo))
                return "queued"

        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.upsert_user(10, "admin", "admin")
            setup = Setup()
            manager = object.__new__(CommitManager)
            manager.db = db
            manager.permissions = PermissionService(db)
            manager.repository_setup = setup

            response = await manager.handle(
                IncomingMessage(
                    20,
                    10,
                    "admin",
                    "/repo setup owner/repo",
                    thread_id=40,
                )
            )

            self.assertEqual(response, "queued")
            self.assertEqual(setup.calls, [(20, 40, "owner/repo")])


if __name__ == "__main__":
    unittest.main()
