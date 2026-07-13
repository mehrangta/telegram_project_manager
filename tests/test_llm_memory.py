import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage

from telegram_project_manager.platform.llm.memory import SQLiteChatMessageHistory, memory_session_id
from telegram_project_manager.platform.storage.db import Database


class LlmMemoryTests(unittest.TestCase):
    def test_persists_trims_and_clears_messages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            session_id = memory_session_id(123)
            history = SQLiteChatMessageHistory(db, session_id, max_messages=4)
            history.add_messages(
                [
                    HumanMessage(content="one"),
                    AIMessage(content='{"turn":1}'),
                    HumanMessage(content="two"),
                    AIMessage(content='{"turn":2}'),
                    HumanMessage(content="three"),
                    AIMessage(content='{"turn":3}'),
                ]
            )

            restored = SQLiteChatMessageHistory(db, session_id, max_messages=4)
            self.assertEqual(
                [message.content for message in restored.messages],
                ["two", '{"turn":2}', "three", '{"turn":3}'],
            )
            self.assertEqual(db.count_llm_messages(session_id), 4)

            restored.clear()
            self.assertEqual(restored.messages, [])

    def test_isolates_chat_sessions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            SQLiteChatMessageHistory(db, memory_session_id(1)).add_messages([HumanMessage(content="chat one")])
            SQLiteChatMessageHistory(db, memory_session_id(2)).add_messages([HumanMessage(content="chat two")])

            self.assertEqual(
                [message.content for message in SQLiteChatMessageHistory(db, memory_session_id(1)).messages],
                ["chat one"],
            )


if __name__ == "__main__":
    unittest.main()
