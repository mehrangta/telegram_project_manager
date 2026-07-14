import unittest

from telegram_project_manager.platform.telegram_bot import (
    incoming_message_from_update,
    incoming_message_from_updates,
)


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

    def test_builds_photo_caption_message(self):
        incoming = incoming_message_from_update(
            {
                "update_id": 11,
                "message": {
                    "message_id": 21,
                    "media_group_id": "album-1",
                    "message_thread_id": 7,
                    "from": {"id": 30, "username": "admin"},
                    "chat": {"id": 40, "type": "supergroup"},
                    "caption": "/issue button broken",
                    "photo": [
                        {"file_id": "small", "file_unique_id": "u1", "file_size": 10},
                        {"file_id": "large", "file_unique_id": "u2", "file_size": 20},
                    ],
                },
            }
        )
        self.assertIsNotNone(incoming)
        assert incoming is not None
        self.assertEqual(incoming.text, "/issue button broken")
        self.assertEqual(incoming.attachments[0].file_id, "large")
        self.assertEqual(incoming.media_group_id, "album-1")
        self.assertEqual(incoming.thread_id, 7)

    def test_merges_album_in_message_order(self):
        def update(message_id, caption, file_id):
            return {
                "message": {
                    "message_id": message_id,
                    "media_group_id": "album",
                    "from": {"id": 1},
                    "chat": {"id": 2, "type": "private"},
                    "caption": caption,
                    "photo": [{"file_id": file_id, "file_unique_id": file_id, "file_size": 1}],
                }
            }

        incoming = incoming_message_from_updates(
            [update(2, "", "second"), update(1, "/issue bug", "first")]
        )
        self.assertIsNotNone(incoming)
        assert incoming is not None
        self.assertEqual(incoming.text, "/issue bug")
        self.assertEqual([item.file_id for item in incoming.attachments], ["first", "second"])


if __name__ == "__main__":
    unittest.main()
