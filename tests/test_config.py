import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from telegram_project_manager.main import main
from telegram_project_manager.platform.config import normalize_config_value
from telegram_project_manager.platform.storage.db import Database


class ConfigTests(unittest.TestCase):
    def test_normalizes_openai_base_url(self):
        self.assertEqual(
            normalize_config_value("openai_base_url", " https://llm.example.test/v1/ "),
            "https://llm.example.test/v1",
        )

    def test_rejects_invalid_openai_base_url(self):
        with self.assertRaisesRegex(ValueError, "absolute HTTP or HTTPS URL"):
            normalize_config_value("openai_base_url", "llm.example.test/v1")

    def test_cli_sets_openai_base_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bot.db"
            argv = [
                "telegram-project-manager",
                "--db",
                str(db_path),
                "config",
                "set",
                "openai_base_url",
                "https://llm.example.test/v1/",
            ]
            with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                main()

            db = Database(db_path)
            self.assertEqual(db.get_setting("openai_base_url"), "https://llm.example.test/v1")


if __name__ == "__main__":
    unittest.main()
