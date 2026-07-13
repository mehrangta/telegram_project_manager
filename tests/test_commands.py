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


if __name__ == "__main__":
    unittest.main()
