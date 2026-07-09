import tempfile
import unittest
from pathlib import Path

from telegram_project_manager.platform.permissions import PermissionService
from telegram_project_manager.platform.storage.db import Database


class PermissionTests(unittest.TestCase):
    def test_admin_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "bot.db")
            db.initialize()
            service = PermissionService(db)
            self.assertFalse(service.is_admin(123))
            db.upsert_user(123, "admin", "admin")
            self.assertTrue(service.is_admin(123))


if __name__ == "__main__":
    unittest.main()

