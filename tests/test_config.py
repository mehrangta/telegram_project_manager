import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from telegram_project_manager.main import main
from telegram_project_manager.platform.config import normalize_config_value, resolve_codex_model
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

    def test_normalizes_codex_base_url(self):
        self.assertEqual(
            normalize_config_value("codex_base_url", " http://codex.example.test/ "),
            "http://codex.example.test",
        )

    def test_resolves_phase_specific_codex_models_with_legacy_fallback(self):
        settings = {"codex_model": "shared", "codex_plan_model": "planner"}

        def get_setting(key, default=""):
            return settings.get(key, default)

        self.assertEqual(resolve_codex_model(get_setting, "plan"), "planner")
        self.assertEqual(resolve_codex_model(get_setting, "code"), "shared")

    def test_cli_stores_phase_specific_codex_models(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bot.db"
            for key, value in (
                ("codex_plan_model", "planner-model"),
                ("codex_code_model", "coding-model"),
            ):
                argv = [
                    "telegram-project-manager", "--db", str(db_path), "config", "set",
                    key, value,
                ]
                with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                    main()

            db = Database(db_path)
            self.assertEqual(db.get_setting("codex_plan_model"), "planner-model")
            self.assertEqual(db.get_setting("codex_code_model"), "coding-model")

    def test_validates_llm_memory_limit(self):
        self.assertEqual(normalize_config_value("llm_memory_max_messages", " 8 "), "8")
        with self.assertRaisesRegex(ValueError, "at least 2"):
            normalize_config_value("llm_memory_max_messages", "1")
        with self.assertRaisesRegex(ValueError, "even number"):
            normalize_config_value("llm_memory_max_messages", "3")

    def test_normalizes_issue_body_llm_boolean(self):
        self.assertEqual(normalize_config_value("issue_body_llm_enabled", " TRUE "), "true")
        self.assertEqual(normalize_config_value("issue_body_llm_enabled", "false"), "false")
        with self.assertRaisesRegex(ValueError, "true or false"):
            normalize_config_value("issue_body_llm_enabled", "yes")

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

    def test_cli_stores_api_key_separately_and_redacts_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bot.db"
            set_argv = [
                "telegram-project-manager",
                "--db",
                str(db_path),
                "config",
                "set",
                "openai_api_key",
                "secret-value",
            ]
            with patch.object(sys, "argv", set_argv), redirect_stdout(io.StringIO()):
                main()

            output = io.StringIO()
            show_argv = ["telegram-project-manager", "--db", str(db_path), "config", "show"]
            with patch.object(sys, "argv", show_argv), redirect_stdout(output):
                main()

            db = Database(db_path)
            self.assertEqual(db.get_secret("openai_api_key"), "secret-value")
            self.assertNotIn("openai_api_key", db.all_settings())
            self.assertIn("openai_api_key=<set>", output.getvalue())
            self.assertNotIn("secret-value", output.getvalue())

    def test_cli_stores_and_redacts_codex_api_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bot.db"
            set_argv = [
                "telegram-project-manager", "--db", str(db_path), "config", "set",
                "codex_api_key", "codex-secret-value",
            ]
            with patch.object(sys, "argv", set_argv), redirect_stdout(io.StringIO()):
                main()
            output = io.StringIO()
            show_argv = ["telegram-project-manager", "--db", str(db_path), "config", "show"]
            with patch.object(sys, "argv", show_argv), redirect_stdout(output):
                main()
            db = Database(db_path)
            self.assertEqual(db.get_secret("codex_api_key"), "codex-secret-value")
            self.assertIn("codex_api_key=<set>", output.getvalue())
            self.assertNotIn("codex-secret-value", output.getvalue())


if __name__ == "__main__":
    unittest.main()
