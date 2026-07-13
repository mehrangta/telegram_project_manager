import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_project_manager.platform.llm.client import OpenAICompatibleClient
from telegram_project_manager.platform.secrets import SecretStore
from telegram_project_manager.platform.storage.db import Database


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return b'{"choices":[{"message":{"content":"{}"}}]}'


class LlmClientTests(unittest.TestCase):
    def test_secrets_file_base_url_overrides_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = Database(root / "bot.db")
            db.initialize()
            db.set_setting("openai_model", "test-model")
            db.set_setting("openai_base_url", "https://database.example.test/v1")
            secrets_path = root / "secrets.json"
            secrets_path.write_text(
                json.dumps(
                    {
                        "OPENAI_API_KEY": "test-key",
                        "OPENAI_BASE_URL": "https://provider.example.test/v1/",
                    }
                ),
                encoding="utf-8",
            )
            client = OpenAICompatibleClient(db, SecretStore(secrets_path))

            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": "", "OPENAI_BASE_URL": ""}),
                patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen,
            ):
                self.assertEqual(client.chat_json("system", "user"), {})

            request = urlopen.call_args.args[0]
            self.assertEqual(
                request.full_url,
                "https://provider.example.test/v1/chat/completions",
            )


if __name__ == "__main__":
    unittest.main()
