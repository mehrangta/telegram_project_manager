import unittest

from telegram_project_manager.platform.telegram_bot import incoming_message_from_update


class TelegramBotTests(unittest.TestCase):
    def test_builds_incoming_message_from_bot_api_update(self):
        incoming = incoming_message_from_update(
            {
                "update_id": 10,
                "message": {
                    "message_id": 20,
                    "from": {"id": 30, "username": "admin"},
                    "chat": {"id": 40, "type": "private"},
                    "text": " /status ",
                },
            }
        )

        self.assertIsNotNone(incoming)
        assert incoming is not None
        self.assertEqual(incoming.chat_id, 40)
        self.assertEqual(incoming.user_id, 30)
        self.assertEqual(incoming.username, "admin")
        self.assertEqual(incoming.text, "/status")
        self.assertTrue(incoming.is_private)

    def test_ignores_non_text_update(self):
        self.assertIsNone(incoming_message_from_update({"update_id": 10, "message": {"photo": []}}))


if __name__ == "__main__":
    unittest.main()
