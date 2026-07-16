import asyncio
import unittest
from unittest import mock

from telegram_project_manager.platform.router import IncomingMessage, TelegramRouter
from telegram_project_manager.platform.telegram_bot import (
    TelegramBotApi,
    callback_action_from_update,
    incoming_message_from_update,
    incoming_message_from_updates,
    run_polling,
    TelegramBotApiError,
)


class TelegramBotTests(unittest.TestCase):
    def test_send_and_edit_include_formatting_keyboard_and_preview_options(self):
        class RecordingApi(TelegramBotApi):
            def __init__(self):
                super().__init__("token")
                self.calls = []

            def _call(self, method, payload=None, timeout=30):
                self.calls.append((method, payload))
                return {"message_id": 9} if method == "sendMessage" else True

        api = RecordingApi()
        markup = {"inline_keyboard": [[{"text": "Copy", "copy_text": {"text": "c-id"}}]]}
        api.send_message(
            1,
            "<b>Status</b>",
            2,
            parse_mode="HTML",
            reply_markup=markup,
            disable_link_preview=True,
            reply_to_message_id=7,
        )
        api.edit_message_text(
            1,
            9,
            "<b>Ready</b>",
            parse_mode="HTML",
            reply_markup={"inline_keyboard": []},
            disable_link_preview=True,
        )
        api.delete_message(1, 9)

        sent = api.calls[0][1]
        edited = api.calls[1][1]
        deleted = api.calls[2]
        self.assertEqual(sent["parse_mode"], "HTML")
        self.assertEqual(sent["reply_markup"], markup)
        self.assertEqual(sent["link_preview_options"], {"is_disabled": True})
        self.assertEqual(
            sent["reply_parameters"],
            {"message_id": 7, "allow_sending_without_reply": True},
        )
        self.assertEqual(edited["reply_markup"], {"inline_keyboard": []})
        self.assertEqual(deleted, ("deleteMessage", {"chat_id": 1, "message_id": 9}))

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

    def test_builds_callback_action_from_button_press(self):
        action = callback_action_from_update(
            {
                "callback_query": {
                    "id": "query-1",
                    "from": {"id": 30, "username": "admin"},
                    "data": "command:/code rebase c-abcdef12",
                    "message": {
                        "message_id": 20,
                        "message_thread_id": 7,
                        "chat": {"id": 40, "type": "supergroup"},
                    },
                }
            }
        )

        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.query_id, "query-1")
        self.assertEqual(action.message.chat_id, 40)
        self.assertEqual(action.message.user_id, 30)
        self.assertEqual(action.message.thread_id, 7)

    def test_group_reply_to_draft_is_routed_without_command_or_mention(self):
        class Handler:
            async def handle(self, message):
                return f"edited {message.reply_to_draft_id}"

        class Database:
            @staticmethod
            def get_user(user_id):
                return {"role": "admin"} if user_id == 2 else None

        router = TelegramRouter(Database(), [Handler()])
        response = asyncio.run(
            router.handle_message(
                IncomingMessage(
                    1, 2, "admin", "make it clearer",
                    reply_to_draft_id="i-abcdef12",
                )
            )
        )
        self.assertEqual(response, "edited i-abcdef12")

    def test_non_admin_message_is_silently_ignored_before_handlers(self):
        class Database:
            @staticmethod
            def get_user(user_id):
                return None

        class Handler:
            called = False

            async def handle(self, message):
                self.called = True
                return "should not be sent"

        handler = Handler()
        router = TelegramRouter(Database(), [handler])

        response = asyncio.run(
            router.handle_message(IncomingMessage(1, 999, "stranger", "/start", is_private=True))
        )

        self.assertIsNone(response)
        self.assertFalse(handler.called)


class TelegramCallbackPollingTests(unittest.IsolatedAsyncioTestCase):
    class Bot:
        def __init__(self, updates):
            self.updates = updates
            self.answers = []
            self.sent = []
            self.markup_edits = []
            self.polls = 0

        def delete_webhook(self):
            return None

        def get_me(self):
            return {"username": "project_bot"}

        def get_updates(self, offset=None):
            self.polls += 1
            if self.polls == 1:
                return self.updates
            raise asyncio.CancelledError()

        def answer_callback_query(self, query_id, text=""):
            self.answers.append((query_id, text))

        def send_message(self, chat_id, text, thread_id=None, **kwargs):
            self.sent.append((chat_id, text, thread_id, kwargs))
            return {"message_id": 100 + len(self.sent)}

        def edit_message_reply_markup(self, chat_id, message_id, reply_markup):
            self.markup_edits.append((chat_id, message_id, reply_markup))

    class Router:
        def __init__(self, admin_ids=None):
            self.commands = []
            self.bot_username = ""
            self.admin_ids = {30} if admin_ids is None else set(admin_ids)

        def set_bot_username(self, username):
            self.bot_username = username

        def is_admin(self, user_id):
            return user_id in self.admin_ids

        async def handle_message(self, message):
            self.commands.append(message.text)
            return "Action queued"

    @staticmethod
    def callback(update_id, query_id, data, message_id=20, user_id=30):
        return {
            "update_id": update_id,
            "callback_query": {
                "id": query_id,
                "from": {"id": user_id, "username": "admin"},
                "data": data,
                "message": {
                    "message_id": message_id,
                    "chat": {"id": 40, "type": "supergroup"},
                },
            },
        }

    @staticmethod
    def message(update_id, text, user_id=30):
        return {
            "update_id": update_id,
            "message": {
                "message_id": 100 + update_id,
                "from": {"id": user_id, "username": "admin"},
                "chat": {"id": 40, "type": "supergroup"},
                "text": text,
            },
        }

    async def test_action_button_dispatches_existing_command(self):
        bot = self.Bot([
            self.callback(1, "query-1", "command:/code rebase c-abcdef12")
        ])
        router = self.Router()

        with self.assertRaises(asyncio.CancelledError):
            await run_polling(bot, router)

        self.assertEqual(router.commands, ["/code rebase c-abcdef12"])
        self.assertEqual(bot.answers, [("query-1", "Action requested")])
        self.assertEqual(bot.sent[0][1], "ℹ️ <b>Action queued</b>")

    async def test_deploy_button_prompts_then_confirm_dispatches(self):
        bot = self.Bot([
            self.callback(1, "query-1", "confirm_deploy:c-abcdef12"),
            self.callback(2, "query-2", "command:/deploy c-abcdef12", message_id=21),
        ])
        router = self.Router()

        with self.assertRaises(asyncio.CancelledError):
            await run_polling(bot, router)

        self.assertEqual(router.commands, ["/deploy c-abcdef12"])
        confirmation_markup = bot.sent[0][3]["reply_markup"]
        self.assertEqual(
            confirmation_markup["inline_keyboard"][0][0]["callback_data"],
            "command:/deploy c-abcdef12",
        )
        self.assertEqual(
            bot.markup_edits,
            [(40, 21, {"inline_keyboard": []})],
        )

    async def test_merge_button_prompts_then_confirm_dispatches(self):
        bot = self.Bot([
            self.callback(1, "query-1", "confirm_merge:c-abcdef12"),
            self.callback(2, "query-2", "command:/merge c-abcdef12", message_id=21),
        ])
        router = self.Router()

        with self.assertRaises(asyncio.CancelledError):
            await run_polling(bot, router)

        self.assertEqual(router.commands, ["/merge c-abcdef12"])
        self.assertIn("It will not deploy", bot.sent[0][1])
        confirmation_markup = bot.sent[0][3]["reply_markup"]
        self.assertEqual(
            confirmation_markup["inline_keyboard"][0][0]["callback_data"],
            "command:/merge c-abcdef12",
        )
        self.assertEqual(bot.markup_edits, [(40, 21, {"inline_keyboard": []})])

    async def test_non_admin_callback_is_silently_ignored(self):
        bot = self.Bot([
            self.callback(1, "query-1", "command:/status", user_id=99),
            self.callback(2, "query-2", "cancel_deploy", user_id=99),
        ])
        router = self.Router()

        with self.assertRaises(asyncio.CancelledError):
            await run_polling(bot, router)

        self.assertEqual(router.commands, [])
        self.assertEqual(bot.answers, [])
        self.assertEqual(bot.sent, [])
        self.assertEqual(bot.markup_edits, [])

    async def test_expired_callback_ack_does_not_block_command_or_next_update(self):
        class ExpiredCallbackBot(self.Bot):
            def answer_callback_query(self, query_id, text=""):
                raise TelegramBotApiError("query is too old")

        bot = ExpiredCallbackBot([
            self.callback(1, "query-1", "command:/status"),
            self.message(2, "/repo show"),
        ])
        router = self.Router()

        with self.assertLogs(level="WARNING") as logs:
            with self.assertRaises(asyncio.CancelledError):
                await run_polling(bot, router)

        self.assertEqual(router.commands, ["/status", "/repo show"])
        self.assertEqual(len(bot.sent), 2)
        self.assertTrue(any("callback acknowledgement failed" in line for line in logs.output))

    async def test_polling_failure_retries_after_one_second(self):
        class FailingPollingBot(self.Bot):
            def get_updates(self, offset=None):
                self.polls += 1
                if self.polls == 1:
                    raise TelegramBotApiError("read timed out")
                raise asyncio.CancelledError()

        bot = FailingPollingBot([])
        router = self.Router()

        with mock.patch(
            "telegram_project_manager.platform.telegram_bot.asyncio.sleep",
            new=mock.AsyncMock(),
        ) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                await run_polling(bot, router)

        sleep.assert_awaited_once_with(1)

    async def test_slow_command_logs_update_id_and_command_only(self):
        bot = self.Bot([self.message(7, "/repo show private details")])
        router = self.Router()

        with mock.patch(
            "telegram_project_manager.platform.telegram_bot.monotonic",
            side_effect=[10.0, 12.1],
        ):
            with self.assertLogs(level="WARNING") as logs:
                with self.assertRaises(asyncio.CancelledError):
                    await run_polling(bot, router)

        warning = "\n".join(logs.output)
        self.assertIn("update_id=7 command=/repo elapsed=2.10s", warning)
        self.assertNotIn("private details", warning)


if __name__ == "__main__":
    unittest.main()
