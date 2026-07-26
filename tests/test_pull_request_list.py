import json
import tempfile
import unittest
from pathlib import Path

from telegram_project_manager.bots.pull_request_manager.pull_request_list import (
    PullRequestListService,
)
from telegram_project_manager.integrations.gh.pull_requests import PullRequestSummary
from telegram_project_manager.platform.storage.db import Database
from telegram_project_manager.platform.telegram_bot import TelegramBotApiError


class FakePullRequestReader:
    def __init__(self):
        self.pull_requests = {}
        self.errors = {}
        self.calls = []

    def list_open_pull_requests(self, repo):
        self.calls.append(repo)
        if repo in self.errors:
            raise self.errors[repo]
        return list(self.pull_requests.get(repo, ()))


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


def pull_request(number, title="Pull request", repo="owner/repo"):
    return PullRequestSummary(
        number,
        title,
        f"https://github.com/{repo}/pull/{number}",
    )


class PullRequestListServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "bot.db")
        self.db.initialize()
        self.db.allow_repo("owner/repo", 1)
        self.reader = FakePullRequestReader()
        self.bot = FakeBot()
        self.service = PullRequestListService(
            db=self.db,
            bot=self.bot,
            reader=self.reader,
            interval_seconds=3600,
        )

    async def asyncTearDown(self):
        await self.service.shutdown()
        self.temp.cleanup()

    async def test_publish_renders_safe_links_without_code_actions(self):
        self.reader.pull_requests["owner/repo"] = [
            pull_request(9, "Fix <unsafe & title>"),
            pull_request(4, "Older pull request"),
        ]

        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")

        sent = self.bot.sent[0]
        self.assertIn("Open pull requests for owner/repo:", sent[2])
        self.assertIn('<a href="https://github.com/owner/repo/pull/9">#9</a>', sent[2])
        self.assertIn("Fix &lt;unsafe &amp; title&gt;", sent[2])
        self.assertNotIn("/code", sent[2])
        self.assertNotIn("/merge", sent[2])
        self.assertIsNone(sent[4]["reply_markup"])
        self.assertTrue(sent[4]["disable_link_preview"])

    async def test_new_publish_supersedes_old_pr_message_but_not_issue_message(self):
        self.db.upsert_issue_list_message(20, None, 50, "owner/repo", "issues")
        self.reader.pull_requests["owner/repo"] = [pull_request(1, "First")]
        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")
        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")

        self.assertEqual(
            self.db.get_pull_request_list_message(20, None)["telegram_message_id"],
            101,
        )
        self.assertEqual(self.db.get_issue_list_message(20, None)["telegram_message_id"], 50)

    async def test_topic_targets_are_independent_and_refresh_is_grouped(self):
        self.db.allow_repo("owner/other", 1)
        self.reader.pull_requests["owner/repo"] = [pull_request(1)]
        self.reader.pull_requests["owner/other"] = [
            pull_request(2, repo="owner/other")
        ]
        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")
        await self.service.publish(chat_id=20, thread_id=7, repo="owner/repo")
        await self.service.publish(chat_id=20, thread_id=8, repo="owner/other")
        self.reader.calls.clear()
        self.reader.pull_requests["owner/repo"] = [pull_request(3)]
        self.reader.pull_requests["owner/other"] = [
            pull_request(4, repo="owner/other")
        ]

        await self.service.refresh()

        self.assertCountEqual(self.reader.calls, ["owner/repo", "owner/other"])
        self.assertEqual([item[1] for item in self.bot.edited], [100, 101, 102])

    async def test_empty_transition_renders_empty_state(self):
        self.reader.pull_requests["owner/repo"] = [pull_request(1)]
        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")
        self.reader.pull_requests["owner/repo"] = []

        await self.service.refresh()

        edit = self.bot.edited[0]
        self.assertEqual(edit[2], "No open pull requests for owner/repo.")
        self.assertEqual(edit[3]["reply_markup"], {"inline_keyboard": []})

    async def test_unchanged_refresh_does_not_edit(self):
        self.reader.pull_requests["owner/repo"] = [pull_request(1)]
        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")

        await self.service.refresh()

        self.assertEqual(self.bot.edited, [])

    async def test_stale_refresh_snapshot_cannot_edit_replaced_message(self):
        self.reader.pull_requests["owner/repo"] = [pull_request(1)]
        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")
        stale = self.db.get_pull_request_list_message(20, None)
        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")
        outgoing = self.service.render("owner/repo", [pull_request(2)])

        await self.service._refresh_target(
            stale,
            outgoing,
            self.service.render_hash(outgoing),
        )

        self.assertEqual(self.bot.edited, [])

    async def test_message_not_modified_synchronizes_hash(self):
        self.reader.pull_requests["owner/repo"] = [pull_request(1)]
        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")
        self.reader.pull_requests["owner/repo"] = [pull_request(2)]
        expected = self.service.render_hash(
            self.service.render("owner/repo", self.reader.pull_requests["owner/repo"])
        )
        self.bot.edit_error = TelegramBotApiError("Bad Request: message is not modified")

        await self.service.refresh()

        self.assertEqual(
            self.db.get_pull_request_list_message(20, None)["render_hash"], expected
        )

    async def test_permanent_message_error_removes_target(self):
        self.reader.pull_requests["owner/repo"] = [pull_request(1)]
        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")
        self.reader.pull_requests["owner/repo"] = [pull_request(2)]
        self.bot.edit_error = TelegramBotApiError(
            "Bad Request: message to edit not found"
        )

        await self.service.refresh()

        self.assertIsNone(self.db.get_pull_request_list_message(20, None))

    async def test_transient_refresh_failure_is_audited_and_retained(self):
        self.reader.pull_requests["owner/repo"] = [pull_request(1)]
        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")
        self.reader.errors["owner/repo"] = RuntimeError(" temporary\n failure ")

        await self.service.refresh()

        self.assertIsNotNone(self.db.get_pull_request_list_message(20, None))
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT action, details_json FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row["action"], "prs.refresh")
        self.assertEqual(json.loads(row["details_json"])["error"], "temporary failure")

    async def test_disallowed_repository_tracker_is_pruned(self):
        self.reader.pull_requests["owner/repo"] = [pull_request(1)]
        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")
        self.db.disallow_repo("owner/repo")

        await self.service.refresh()

        self.assertIsNone(self.db.get_pull_request_list_message(20, None))
        self.assertEqual(self.reader.calls, ["owner/repo"])

    async def test_recover_starts_once_and_shutdown_stops_scheduler(self):
        await self.service.recover()
        task = self.service._task
        await self.service.recover()

        self.assertIs(self.service._task, task)
        await self.service.shutdown()
        self.assertIsNone(self.service._task)
        self.assertTrue(task.done())

    def test_database_conditional_updates_do_not_touch_replaced_target(self):
        self.db.upsert_pull_request_list_message(20, None, 100, "owner/repo", "old")
        self.db.upsert_pull_request_list_message(20, None, 101, "owner/repo", "new")

        updated = self.db.update_pull_request_list_render_hash(
            20, None, 100, "owner/repo", "stale"
        )
        deleted = self.db.delete_pull_request_list_message(
            20, None, 100, "owner/repo"
        )

        self.assertFalse(updated)
        self.assertFalse(deleted)
        self.assertEqual(
            self.db.get_pull_request_list_message(20, None)["render_hash"], "new"
        )


if __name__ == "__main__":
    unittest.main()
