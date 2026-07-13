import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from telegram_project_manager.platform.llm.client import LlmError, OpenAICompatibleClient
from telegram_project_manager.platform.secrets import SecretStore
from telegram_project_manager.platform.storage.db import Database


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
                patch("telegram_project_manager.platform.llm.client.ChatOpenAI") as chat_openai,
            ):
                bound = chat_openai.return_value.bind.return_value
                bound.invoke.return_value.content = "{}"
                self.assertEqual(client.chat_json("system", "user"), {})

            chat_openai.assert_called_once_with(
                model="test-model",
                api_key="test-key",
                base_url="https://provider.example.test/v1",
                temperature=0.1,
                timeout=90,
                max_retries=2,
            )
            chat_openai.return_value.bind.assert_called_once_with(response_format={"type": "json_object"})
            bound.invoke.assert_called_once_with(
                [
                    ("system", "system"),
                    ("human", "user"),
                ]
            )

    def test_wraps_langchain_errors(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = Database(root / "bot.db")
            db.initialize()
            db.set_setting("openai_model", "test-model")
            secrets_path = root / "secrets.json"
            secrets_path.write_text(json.dumps({"OPENAI_API_KEY": "test-key"}), encoding="utf-8")
            client = OpenAICompatibleClient(db, SecretStore(secrets_path))

            with patch("telegram_project_manager.platform.llm.client.ChatOpenAI") as chat_openai:
                chat_openai.return_value.bind.return_value.invoke.side_effect = RuntimeError("provider unavailable")
                with self.assertRaisesRegex(LlmError, "provider unavailable"):
                    client.chat_json("system", "user")

    def test_replays_persistent_memory_for_same_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = Database(root / "bot.db")
            db.initialize()
            db.set_setting("openai_model", "test-model")
            secrets_path = root / "secrets.json"
            secrets_path.write_text(json.dumps({"OPENAI_API_KEY": "test-key"}), encoding="utf-8")
            client = OpenAICompatibleClient(db, SecretStore(secrets_path))
            prompts = []
            responses = iter(['{"turn":1}', '{"turn":2}'])

            def respond(prompt):
                prompts.append(prompt.to_messages())
                return AIMessage(content=next(responses))

            with patch("telegram_project_manager.platform.llm.client.ChatOpenAI") as chat_openai:
                chat_openai.return_value.bind.return_value = RunnableLambda(respond)
                self.assertEqual(client.chat_json("system", "first", memory_key="chat:1"), {"turn": 1})
                self.assertEqual(client.chat_json("system", "second", memory_key="chat:1"), {"turn": 2})

            self.assertEqual(
                [(message.type, message.content) for message in prompts[1]],
                [
                    ("system", "system"),
                    ("human", "first"),
                    ("ai", '{"turn":1}'),
                    ("human", "second"),
                ],
            )


if __name__ == "__main__":
    unittest.main()
