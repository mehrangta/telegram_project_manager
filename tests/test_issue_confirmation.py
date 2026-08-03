import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from telegram_project_manager.bots.issue_manager.progress import IssueConfirmationService
from telegram_project_manager.integrations.gh.issues import IssueResult
from telegram_project_manager.platform.storage.db import Database
from telegram_project_manager.platform.telegram_bot import TelegramBotApiError


class FakeIssueReader:
    def __init__(self):
        self.state = "open"
        self.error = None
        self.calls = []

    def get_issue_state(self, repo, number):
        self.calls.append((repo, number))
        if self.error:
            raise self.error
        return self.state


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []
        self.deleted = []
        self.edit_error = None

    def send_message(self, chat_id, text, thread_id=None, **options):
        message_id = 100 + len(self.sent)
        self.sent.append((chat_id, message_id, text, thread_id, options))
        return {"message_id": message_id}

    def edit_message_text(self, chat_id, message_id, text, **options):
        if self.edit_error:
            raise self.edit_error
        self.edited.append((chat_id, message_id, text, options))

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class IssueConfirmationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "bot.db")
        self.db.initialize()
        self.reader = FakeIssueReader()
        self.bot = FakeBot()
        self.service = IssueConfirmationService(
            db=self.db,
            bot=self.bot,
            reader=self.reader,
            interval_seconds=3600,
        )
        self.create_issue()

    async def asyncTearDown(self):
        await self.service.shutdown()
        self.temp.cleanup()

    def create_issue(self, draft_id="i-abcdef12"):
        now = int(time.time())
        self.db.create_issue_draft(
            {
                "id": draft_id,
                "telegram_chat_id": 20,
                "telegram_thread_id": 7,
                "telegram_reply_to_message_id": 41,
                "telegram_user_id": 10,
                "repo": "owner/repo",
                "default_branch": "main",
                "request_text": "button broken",
                "issue_json": {
                    "title": "Broken button",
                    "summary": "Summary",
                    "actual_behavior": "Nothing happens",
                    "expected_behavior": "It works",
                },
                "status": "pending",
                "created_at": now,
                "expires_at": now + 300,
            },
            [],
        )
        self.db.update_issue_draft_status(
            draft_id,
            "created",
            12,
            "https://github.com/owner/repo/issues/12",
        )

    @staticmethod
    def result():
        return IssueResult(
            repo="owner/repo",
            number=12,
            url="https://github.com/owner/repo/issues/12",
            title="Broken button",
        )

    async def publish(self):
        await self.service.publish(
            draft_id="i-abcdef12",
            result=self.result(),
            reply_to_message_id=41,
        )

    async def test_publish_sends_actions_and_persists_message(self):
        await self.publish()

        sent = self.bot.sent[0]
        self.assertEqual((sent[0], sent[3]), (20, 7))
        self.assertEqual(sent[4]["reply_to_message_id"], 41)
        buttons = sent[4]["reply_markup"]["inline_keyboard"]
        self.assertEqual([button["text"] for button in buttons[0]], [
            "📝 Plan", "💻 Code", "📝💻 Plan & Code", "↗ Issue"
        ])
        self.assertEqual(buttons[1][0]["text"], "✖️ Close")
        record = self.db.get_issue_draft("i-abcdef12")
        self.assertEqual(record["telegram_confirmation_message_id"], 100)

    async def test_repeated_publish_reuses_tracked_message(self):
        await self.publish()
        await self.publish()

        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual([(item[0], item[1]) for item in self.bot.edited], [(20, 100)])

    async def test_open_refresh_does_not_edit(self):
        await self.publish()

        await self.service.refresh()

        self.assertEqual(self.reader.calls, [("owner/repo", 12)])
        self.assertEqual(self.bot.edited, [])
        self.assertEqual(self.db.get_issue_draft("i-abcdef12")["status"], "created")

    async def test_external_close_removes_actions_and_marks_closed(self):
        await self.publish()
        self.reader.state = "closed"

        await self.service.refresh()

        edited = self.bot.edited[0]
        self.assertIn("Issue closed", edited[2])
        buttons = edited[3]["reply_markup"]["inline_keyboard"]
        self.assertEqual(buttons, [[{
            "text": "↗ Issue",
            "url": "https://github.com/owner/repo/issues/12",
        }]])
        record = self.db.get_issue_draft("i-abcdef12")
        self.assertEqual(record["status"], "closed")
        self.assertIsNone(record["telegram_confirmation_message_id"])

    async def test_local_close_retires_tracked_confirmation_immediately(self):
        await self.publish()
        self.db.update_issue_draft_status("i-abcdef12", "closed")

        await self.service.retire("i-abcdef12")

        self.assertIn("Issue closed", self.bot.edited[0][2])
        buttons = self.bot.edited[0][3]["reply_markup"]["inline_keyboard"]
        self.assertEqual(len(buttons), 1)
        record = self.db.get_issue_draft("i-abcdef12")
        self.assertEqual(record["status"], "closed")
        self.assertIsNone(record["telegram_confirmation_message_id"])

    async def test_transient_github_failure_is_audited_and_retried(self):
        await self.publish()
        self.reader.error = RuntimeError("temporary failure")

        await self.service.refresh()

        record = self.db.get_issue_draft("i-abcdef12")
        self.assertEqual(record["status"], "created")
        self.assertEqual(record["telegram_confirmation_message_id"], 100)
        with self.db.session() as conn:
            audit = conn.execute(
                "SELECT action, details_json FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(audit["action"], "issue.confirmation.refresh")
        self.assertEqual(json.loads(audit["details_json"])["error"], "temporary failure")

    async def test_transient_telegram_failure_keeps_tracker_for_retry(self):
        await self.publish()
        self.reader.state = "closed"
        self.bot.edit_error = TelegramBotApiError("read timed out")

        await self.service.refresh()

        record = self.db.get_issue_draft("i-abcdef12")
        self.assertEqual(record["status"], "created")
        self.assertEqual(record["telegram_confirmation_message_id"], 100)
        self.bot.edit_error = None
        await self.service.refresh()
        self.assertEqual(self.db.get_issue_draft("i-abcdef12")["status"], "closed")

    async def test_permanent_telegram_failure_closes_and_clears_tracker(self):
        await self.publish()
        self.reader.state = "closed"
        self.bot.edit_error = TelegramBotApiError("message to edit not found")

        await self.service.refresh()

        record = self.db.get_issue_draft("i-abcdef12")
        self.assertEqual(record["status"], "closed")
        self.assertIsNone(record["telegram_confirmation_message_id"])

    async def test_stale_snapshot_cannot_refresh_replaced_message(self):
        await self.publish()
        target = self.db.list_issue_confirmation_messages()[0]
        self.db.set_issue_confirmation_message("i-abcdef12", 101)

        await self.service._refresh_target(target)

        self.assertEqual(self.reader.calls, [])
        self.assertEqual(self.bot.edited, [])

    async def test_recover_starts_once_and_shutdown_stops_scheduler(self):
        await self.service.recover()
        task = self.service._task
        await self.service.recover()

        self.assertIs(self.service._task, task)
        await self.service.shutdown()
        self.assertIsNone(self.service._task)
        self.assertTrue(task.done())


class IssueConfirmationMigrationTests(unittest.TestCase):
    def test_initialize_adds_confirmation_column_to_existing_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bot.db"
            with sqlite3.connect(path) as conn:
                conn.execute(
                    """
                    CREATE TABLE issue_drafts (
                        id TEXT PRIMARY KEY,
                        telegram_chat_id INTEGER NOT NULL,
                        telegram_thread_id INTEGER,
                        telegram_reply_to_message_id INTEGER,
                        telegram_user_id INTEGER NOT NULL,
                        repo TEXT NOT NULL,
                        default_branch TEXT NOT NULL,
                        local_repo_path TEXT NOT NULL DEFAULT '',
                        request_text TEXT NOT NULL,
                        issue_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        github_issue_number INTEGER,
                        github_issue_url TEXT,
                        created_at INTEGER NOT NULL,
                        expires_at INTEGER NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO issue_drafts VALUES (
                        'i-abcdef12', 20, NULL, NULL, 10, 'owner/repo', 'main', '',
                        'bug', '{}', 'created', 12,
                        'https://github.com/owner/repo/issues/12', 1, 2
                    )
                    """
                )

            db = Database(path)
            db.initialize()

            record = db.get_issue_draft("i-abcdef12")
            self.assertIsNotNone(record)
            self.assertIsNone(record["telegram_confirmation_message_id"])


if __name__ == "__main__":
    unittest.main()
