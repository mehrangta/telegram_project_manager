import asyncio
import unittest

from telegram_project_manager.platform.router import IncomingMessage, TelegramRouter
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

    def test_extracts_draft_id_only_from_bot_preview_reply(self):
        def update(is_bot):
            return {
                "message": {
                    "message_id": 22,
                    "from": {"id": 30},
                    "chat": {"id": 40, "type": "supergroup"},
                    "text": "make the title shorter",
                    "reply_to_message": {
                        "from": {"id": 99, "is_bot": is_bot},
                        "text": "Issue draft created.\nDraft ID: i-abcdef12\nRevision: 1",
                    },
                }
            }

        bot_reply = incoming_message_from_update(update(True))
        user_reply = incoming_message_from_update(update(False))
        assert bot_reply is not None and user_reply is not None
        self.assertEqual(bot_reply.reply_to_draft_id, "i-abcdef12")
        self.assertIsNone(user_reply.reply_to_draft_id)

    def test_extracts_issue_and_code_job_from_bot_reply(self):
        issue = incoming_message_from_update({"message": {"message_id": 30, "from": {"id": 1}, "chat": {"id": 2, "type": "supergroup"}, "text": "/code", "reply_to_message": {"from": {"id": 99, "is_bot": True}, "text": "Issue created.\nRepo: owner/repo\nIssue: #123\nhttps://github.com/owner/repo/issues/123"}}})
        job = incoming_message_from_update({"message": {"message_id": 31, "from": {"id": 1}, "chat": {"id": 2, "type": "supergroup"}, "text": "approve", "reply_to_message": {"from": {"id": 99, "is_bot": True}, "text": "Codex code job\nCode Job ID: c-abcdef12\nStatus: awaiting approval"}}})
        assert issue is not None and job is not None
        self.assertEqual(issue.reply_to_issue_ref, "owner/repo#123")
        self.assertEqual(job.reply_to_code_job_id, "c-abcdef12")

    def test_deploy_reply_extracts_code_job_id(self):
        message = incoming_message_from_update(
            {
                "message": {
                    "message_id": 32,
                    "from": {"id": 1},
                    "chat": {"id": 2, "type": "supergroup"},
                    "text": "/deploy",
                    "reply_to_message": {
                        "from": {"id": 99, "is_bot": True},
                        "text": "Codex code job\nCode Job ID: c-abcdef12\nStatus: ready",
                    },
                }
            }
        )
        assert message is not None
        self.assertEqual(message.reply_to_code_job_id, "c-abcdef12")

    def test_group_reply_to_draft_is_routed_without_command_or_mention(self):
        class Handler:
            async def handle(self, message):
                return f"edited {message.reply_to_draft_id}"

        router = TelegramRouter(None, [Handler()])
        response = asyncio.run(
            router.handle_message(
                IncomingMessage(
                    1, 2, "admin", "make it clearer",
                    reply_to_draft_id="i-abcdef12",
                )
            )
        )
        self.assertEqual(response, "edited i-abcdef12")


if __name__ == "__main__":
    unittest.main()
