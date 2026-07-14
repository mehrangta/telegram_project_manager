import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from telegram_project_manager.platform.llm.client import (
    COMMIT_PLAN_RESPONSE_SCHEMA,
    LlmError,
    OpenAICompatibleClient,
)
from telegram_project_manager.platform.storage.db import Database


class LlmClientTests(unittest.TestCase):
    def test_database_credentials_configure_langchain(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = Database(root / "bot.db")
            db.initialize()
            db.set_setting("openai_model", "test-model")
            db.set_setting("openai_base_url", "https://database.example.test/v1")
            db.set_secret("openai_api_key", "test-key")
            client = OpenAICompatibleClient(db)

            with patch("telegram_project_manager.platform.llm.client.ChatOpenAI") as chat_openai:
                structured = chat_openai.return_value.with_structured_output.return_value
                structured.invoke.return_value = {
                    "raw": AIMessage(content="{}"),
                    "parsed": {},
                    "parsing_error": None,
                }
                self.assertEqual(client.chat_json("system", "user"), {})

            chat_openai.assert_called_once_with(
                model="test-model",
                api_key="test-key",
                base_url="https://database.example.test/v1",
                temperature=0.1,
                timeout=90,
                max_retries=2,
            )
            chat_openai.return_value.with_structured_output.assert_called_once_with(
                COMMIT_PLAN_RESPONSE_SCHEMA,
                method="json_schema",
                include_raw=True,
            )
            structured.invoke.assert_called_once_with(
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
            db.set_secret("openai_api_key", "test-key")
            client = OpenAICompatibleClient(db)

            with patch("telegram_project_manager.platform.llm.client.ChatOpenAI") as chat_openai:
                chat_openai.return_value.with_structured_output.return_value.invoke.side_effect = RuntimeError(
                    "provider unavailable"
                )
                with self.assertRaisesRegex(LlmError, "provider unavailable"):
                    client.chat_json("system", "user")

    def test_replays_persistent_memory_for_same_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = Database(root / "bot.db")
            db.initialize()
            db.set_setting("openai_model", "test-model")
            db.set_secret("openai_api_key", "test-key")
            client = OpenAICompatibleClient(db)
            prompts = []
            responses = iter(['{"turn":1}', '{"turn":2}'])

            def respond(prompt):
                prompts.append(prompt.to_messages())
                content = next(responses)
                return {
                    "raw": AIMessage(content=content),
                    "parsed": __import__("json").loads(content),
                    "parsing_error": None,
                }

            with patch("telegram_project_manager.platform.llm.client.ChatOpenAI") as chat_openai:
                chat_openai.return_value.with_structured_output.return_value = RunnableLambda(respond)
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

    def test_requires_bot_managed_api_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.set_setting("openai_model", "test-model")
            with self.assertRaisesRegex(LlmError, "private chat"):
                OpenAICompatibleClient(db).chat_json("system", "user")


if __name__ == "__main__":
    unittest.main()
