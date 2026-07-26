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
    def configured_client(self, root: Path) -> tuple[Database, OpenAICompatibleClient]:
        db = Database(root / "bot.db")
        db.initialize()
        db.set_setting("openai_model", "test-model")
        db.set_secret("openai_api_key", "test-key")
        return db, OpenAICompatibleClient(db)

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

    def test_uses_caller_supplied_json_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.set_setting("openai_model", "test-model")
            db.set_secret("openai_api_key", "test-key")
            schema = {
                "title": "issue",
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
                "additionalProperties": False,
            }
            with patch("telegram_project_manager.platform.llm.client.ChatOpenAI") as chat_openai:
                structured = chat_openai.return_value.with_structured_output.return_value
                structured.invoke.return_value = {
                    "raw": AIMessage(content='{"title":"Bug"}'),
                    "parsed": {"title": "Bug"},
                    "parsing_error": None,
                }
                result = OpenAICompatibleClient(db).chat_json(
                    "system",
                    "user",
                    response_schema=schema,
                )
            self.assertEqual(result, {"title": "Bug"})
            chat_openai.return_value.with_structured_output.assert_called_once_with(
                schema,
                method="json_schema",
                include_raw=True,
            )

    def test_recovers_missing_parsed_object_from_raw_message(self):
        cases = (
            (
                AIMessage(content="", additional_kwargs={"parsed": {"source": "metadata"}}),
                {"source": "metadata"},
            ),
            (AIMessage(content='{"source":"string"}'), {"source": "string"}),
            (
                AIMessage(content=[{"type": "text", "text": '{"source":"block"}'}]),
                {"source": "block"},
            ),
        )
        for raw, expected in cases:
            with self.subTest(source=expected["source"]), tempfile.TemporaryDirectory() as temp_dir:
                _, client = self.configured_client(Path(temp_dir))
                with patch("telegram_project_manager.platform.llm.client.ChatOpenAI") as chat_openai:
                    structured = chat_openai.return_value.with_structured_output.return_value
                    structured.invoke.return_value = {
                        "raw": raw,
                        "parsed": None,
                        "parsing_error": None,
                    }
                    self.assertEqual(client.chat_json("system", "user"), expected)
                structured.invoke.assert_called_once()

    def test_retries_missing_parsed_object_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, client = self.configured_client(Path(temp_dir))
            with patch("telegram_project_manager.platform.llm.client.ChatOpenAI") as chat_openai:
                structured = chat_openai.return_value.with_structured_output.return_value
                structured.invoke.side_effect = [
                    {
                        "raw": AIMessage(content="null"),
                        "parsed": None,
                        "parsing_error": None,
                    },
                    {
                        "raw": AIMessage(content='{"title":"Recovered"}'),
                        "parsed": {"title": "Recovered"},
                        "parsing_error": None,
                    },
                ]
                self.assertEqual(client.chat_json("system", "user"), {"title": "Recovered"})
            self.assertEqual(structured.invoke.call_count, 2)

    def test_missing_parsed_object_fails_after_one_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, client = self.configured_client(Path(temp_dir))
            with patch("telegram_project_manager.platform.llm.client.ChatOpenAI") as chat_openai:
                structured = chat_openai.return_value.with_structured_output.return_value
                structured.invoke.return_value = {
                    "raw": AIMessage(content="null"),
                    "parsed": None,
                    "parsing_error": None,
                }
                with self.assertRaisesRegex(LlmError, "missing parsed object"):
                    client.chat_json("system", "user")
            self.assertEqual(structured.invoke.call_count, 2)

    def test_structured_parsing_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, client = self.configured_client(Path(temp_dir))
            with patch("telegram_project_manager.platform.llm.client.ChatOpenAI") as chat_openai:
                structured = chat_openai.return_value.with_structured_output.return_value
                structured.invoke.return_value = {
                    "raw": AIMessage(content="not-json"),
                    "parsed": None,
                    "parsing_error": ValueError("invalid JSON"),
                }
                with self.assertRaisesRegex(LlmError, "invalid structured output"):
                    client.chat_json("system", "user")
            structured.invoke.assert_called_once()

    def test_invalid_structured_response_is_not_retried(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, client = self.configured_client(Path(temp_dir))
            with patch("telegram_project_manager.platform.llm.client.ChatOpenAI") as chat_openai:
                structured = chat_openai.return_value.with_structured_output.return_value
                structured.invoke.return_value = AIMessage(content='{"title":"Bug"}')
                with self.assertRaisesRegex(LlmError, "structured response is invalid"):
                    client.chat_json("system", "user")
            structured.invoke.assert_called_once()

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

    def test_retry_persists_only_successful_response_in_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db, client = self.configured_client(root)
            prompts = []
            responses = iter(
                [
                    {
                        "raw": AIMessage(content="null"),
                        "parsed": None,
                        "parsing_error": None,
                    },
                    {
                        "raw": AIMessage(content='{"turn":1}'),
                        "parsed": {"turn": 1},
                        "parsing_error": None,
                    },
                    {
                        "raw": AIMessage(content='{"turn":2}'),
                        "parsed": {"turn": 2},
                        "parsing_error": None,
                    },
                ]
            )

            def respond(prompt):
                prompts.append(prompt.to_messages())
                return next(responses)

            with patch("telegram_project_manager.platform.llm.client.ChatOpenAI") as chat_openai:
                chat_openai.return_value.with_structured_output.return_value = RunnableLambda(respond)
                self.assertEqual(client.chat_json("system", "first", memory_key="chat:1"), {"turn": 1})
                self.assertEqual(
                    db.list_llm_messages("chat:1", 12),
                    [
                        {"role": "human", "content": "first"},
                        {"role": "ai", "content": '{"turn":1}'},
                    ],
                )
                self.assertEqual(client.chat_json("system", "second", memory_key="chat:1"), {"turn": 2})

            self.assertEqual(len(prompts), 3)
            self.assertEqual(
                [(message.type, message.content) for message in prompts[2]],
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
