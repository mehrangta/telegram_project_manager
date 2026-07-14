import tempfile
import unittest
from pathlib import Path

from telegram_project_manager.bots.commit_manager.commands import CommitManager, split_command
from telegram_project_manager.platform.permissions import PermissionService
from telegram_project_manager.platform.router import IncomingMessage
from telegram_project_manager.platform.storage.db import Database


class CommandTests(unittest.TestCase):
    def test_split_command(self):
        self.assertEqual(split_command("/commit add readme"), ("/commit", "add readme"))

    def test_split_command_with_bot_username(self):
        self.assertEqual(split_command("/commit@MyBot add readme"), ("/commit", "add readme"))

    def test_openai_api_key_requires_private_admin_chat_and_is_redacted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.upsert_user(10, "admin", "admin")
            manager = object.__new__(CommitManager)
            manager.db = db
            manager.permissions = PermissionService(db)
            group_message = IncomingMessage(1, 10, "admin", "", is_private=False)
            private_message = IncomingMessage(10, 10, "admin", "", is_private=True)

            self.assertIn("private chat", manager.config(group_message, "set openai_api_key secret-value"))
            self.assertFalse(db.has_secret("openai_api_key"))
            self.assertEqual(
                manager.config(private_message, "set openai_api_key secret-value"),
                "Config set: openai_api_key",
            )
            shown = manager.config(private_message, "show")
            self.assertIn("openai_api_key=<set>", shown)
            self.assertNotIn("secret-value", shown)

    def test_admin_sets_and_clears_chat_local_repository(self):
        class Repositories:
            def validate(self, path, repo):
                self.last = (path, repo)
                return Path(path)

        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.upsert_user(10, "admin", "admin")
            db.allow_repo("owner/repo", 10)
            db.set_chat_repo(20, "owner/repo", 10)
            manager = object.__new__(CommitManager)
            manager.db = db
            manager.permissions = PermissionService(db)
            manager.repositories = Repositories()
            message = IncomingMessage(20, 10, "admin", "")
            cache = str(Path(temp_dir) / "cache.git")

            self.assertIn("cache set", manager.repo(message, f"local set {cache}"))
            self.assertEqual(db.get_chat_settings(20)["local_repo_path"], cache)
            self.assertIn("(ok)", manager.repo(message, "show"))
            self.assertIn("cache cleared", manager.repo(message, "local clear"))
            self.assertIsNone(db.get_chat_settings(20)["local_repo_path"])


if __name__ == "__main__":
    unittest.main()
