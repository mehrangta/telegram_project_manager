import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from telegram_project_manager.bots.commit_manager.issue_list import IssueListService
from telegram_project_manager.integrations.gh.issues import IssueSummary
from telegram_project_manager.platform.responses import COPY_TEXT_LIMIT
from telegram_project_manager.platform.storage.db import Database
from telegram_project_manager.platform.telegram_bot import TelegramBotApiError


class FakeIssueReader:
    def __init__(self):
        self.issues = {}
        self.errors = {}
        self.calls = []

    def list_open_issues(self, repo):
        self.calls.append(repo)
        if repo in self.errors:
            raise self.errors[repo]
        return list(self.issues.get(repo, ()))


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


def issue(number, title="Issue", repo="owner/repo"):
    return IssueSummary(number, title, f"https://github.com/{repo}/issues/{number}")


class IssueListServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "bot.db")
        self.db.initialize()
        self.db.allow_repo("owner/repo", 1)
        self.reader = FakeIssueReader()
        self.bot = FakeBot()
        self.service = IssueListService(
            db=self.db,
            bot=self.bot,
            reader=self.reader,
            interval_seconds=3600,
        )

    async def asyncTearDown(self):
        await self.service.shutdown()
        self.temp.cleanup()

    def create_code_job(
        self,
        job_id,
        issue_number,
        status,
        *,
        repo="owner/repo",
        chat_id=20,
        thread_id=None,
        message_id=None,
        created_at=None,
        updated_at=None,
    ):
        self.db.create_code_job(
            {
                "id": job_id,
                "telegram_chat_id": chat_id,
                "telegram_user_id": 10,
                "telegram_thread_id": thread_id,
                "repo": repo,
                "issue_number": issue_number,
                "issue_title": "Issue",
                "issue_url": f"https://github.com/{repo}/issues/{issue_number}",
                "issue_context_json": {},
                "base_branch": "main",
                "target_branch": f"codex/issue-{issue_number}-{job_id}",
                "workspace_path": f"/tmp/{job_id}",
                "source_repo_path": "/tmp/repo",
                "status": status,
                "resume_phase": "plan",
                "skip_plan": False,
            }
        )
        if message_id is not None:
            self.db.update_code_job(job_id, {"telegram_message_id": message_id})
        if created_at is not None or updated_at is not None:
            with self.db.session() as conn:
                conn.execute(
                    """
                    UPDATE code_jobs
                    SET created_at = COALESCE(?, created_at),
                        updated_at = COALESCE(?, updated_at)
                    WHERE id = ?
                    """,
                    (created_at, updated_at, job_id),
                )

    async def test_publish_renders_safe_links_and_copyable_qualified_commands(self):
        self.reader.issues["owner/repo"] = [
            issue(9, "Fix <unsafe & title>"),
            issue(4, "Older issue"),
        ]

        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")

        sent = self.bot.sent[0]
        self.assertIn("Open issues for owner/repo:", sent[2])
        self.assertIn('<a href="https://github.com/owner/repo/issues/9">#9</a>', sent[2])
        self.assertIn("Fix &lt;unsafe &amp; title&gt;", sent[2])
        self.assertIn("<code>/code owner/repo#9</code>", sent[2])
        buttons = sent[4]["reply_markup"]["inline_keyboard"]
        self.assertEqual(buttons[0][0]["copy_text"]["text"], "/code owner/repo#9")
        self.assertEqual(buttons[1][0]["copy_text"]["text"], "/code owner/repo#4")
        self.assertTrue(sent[4]["disable_link_preview"])

    async def test_publish_shows_codex_status_only_for_issues_with_jobs(self):
        self.reader.issues["owner/repo"] = [issue(9), issue(4)]
        self.create_code_job("c-00000001", 9, "awaiting_approval")

        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")

        text = self.bot.sent[0][2]
        self.assertIn("#9</a> — Issue — Codex: awaiting approval", text)
        self.assertIn("#4</a> — Issue\n", text)
        self.assertEqual(text.count("Codex:"), 1)

    def test_render_escapes_and_normalizes_codex_status(self):
        outgoing = self.service.render(
            "owner/repo",
            [issue(7)],
            {
                7: {
                    "status": "waiting_<unsafe&>",
                    "telegram_chat_id": 20,
                    "telegram_thread_id": None,
                    "telegram_message_id": None,
                }
            },
        )

        self.assertIn("Codex: waiting &lt;unsafe&amp;&gt;", outgoing.text)

    def test_ready_status_links_to_supergroup_message(self):
        outgoing = self.service.render(
            "owner/repo",
            [issue(7)],
            {
                7: {
                    "status": "ready",
                    "telegram_chat_id": -1001234567890,
                    "telegram_thread_id": None,
                    "telegram_message_id": 77,
                }
            },
        )

        self.assertIn(
            '<a href="https://t.me/c/1234567890/77">Codex: ready</a>',
            outgoing.text,
        )

    def test_ready_status_links_to_forum_topic_message(self):
        outgoing = self.service.render(
            "owner/repo",
            [issue(7)],
            {
                7: {
                    "status": "ready",
                    "telegram_chat_id": -1001234567890,
                    "telegram_thread_id": 42,
                    "telegram_message_id": 77,
                }
            },
        )

        self.assertIn(
            '<a href="https://t.me/c/1234567890/42/77">Codex: ready</a>',
            outgoing.text,
        )

    def test_ready_status_falls_back_for_unsupported_message_targets(self):
        cases = (
            (20, None),
            (-20, 77),
            (-1001234567890, None),
            (-1001234567890, 0),
        )
        for chat_id, message_id in cases:
            with self.subTest(chat_id=chat_id, message_id=message_id):
                outgoing = self.service.render(
                    "owner/repo",
                    [issue(7)],
                    {
                        7: {
                            "status": "ready",
                            "telegram_chat_id": chat_id,
                            "telegram_thread_id": None,
                            "telegram_message_id": message_id,
                        }
                    },
                )

                self.assertIn(" — Codex: ready\n", outgoing.text)
                self.assertNotIn("t.me/c/", outgoing.text)

    def test_non_ready_status_does_not_link_to_message(self):
        outgoing = self.service.render(
            "owner/repo",
            [issue(7)],
            {
                7: {
                    "status": "coding",
                    "telegram_chat_id": -1001234567890,
                    "telegram_thread_id": 42,
                    "telegram_message_id": 77,
                }
            },
        )

        self.assertIn(" — Codex: coding\n", outgoing.text)
        self.assertNotIn("t.me/c/", outgoing.text)

    def test_latest_status_lookup_is_deterministic_and_repo_scoped(self):
        self.create_code_job(
            "c-00000001",
            1,
            "ready",
            chat_id=-1001111111111,
            thread_id=7,
            message_id=71,
            created_at=100,
            updated_at=300,
        )
        self.create_code_job(
            "c-00000002",
            1,
            "coding",
            chat_id=-1002222222222,
            thread_id=8,
            message_id=72,
            created_at=200,
            updated_at=200,
        )
        self.create_code_job("c-00000003", 2, "discarded")
        self.create_code_job("c-00000004", 1, "failed", repo="owner/other")

        statuses = self.db.get_latest_code_job_statuses("owner/repo", [1, 2, 3])

        self.assertEqual(
            statuses,
            {
                1: {
                    "status": "coding",
                    "telegram_chat_id": -1002222222222,
                    "telegram_thread_id": 8,
                    "telegram_message_id": 72,
                },
                2: {
                    "status": "discarded",
                    "telegram_chat_id": 20,
                    "telegram_thread_id": None,
                    "telegram_message_id": None,
                },
            },
        )
        self.assertEqual(self.db.get_latest_code_job_statuses("owner/repo", []), {})

    def test_full_issue_list_with_statuses_stays_within_telegram_limit(self):
        issues = [issue(number) for number in range(1, 21)]
        statuses = {
            number: {
                "status": "ready",
                "telegram_chat_id": -1001234567890,
                "telegram_thread_id": 42,
                "telegram_message_id": number,
            }
            for number in range(1, 21)
        }

        outgoing = self.service.render("owner/repo", issues, statuses)

        self.assertLessEqual(len(outgoing.text), 4096)

    def test_render_falls_back_when_qualified_command_exceeds_copy_limit(self):
        repo = "o/" + "r" * COPY_TEXT_LIMIT

        outgoing = self.service.render(repo, [issue(7, repo=repo)])

        button = outgoing.reply_markup()["inline_keyboard"][0][0]
        self.assertEqual(button["copy_text"]["text"], "/code #7")
        self.assertIn("<code>/code #7</code>", outgoing.text)

    async def test_new_publish_supersedes_old_message_for_refresh(self):
        self.reader.issues["owner/repo"] = [issue(1, "First")]
        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")
        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")
        self.reader.issues["owner/repo"] = [issue(2, "Changed")]

        await self.service.refresh()

        self.assertEqual([item[1] for item in self.bot.sent], [100, 101])
        self.assertEqual([item[1] for item in self.bot.edited], [101])
        target = self.db.get_issue_list_message(20, None)
        self.assertEqual(target["telegram_message_id"], 101)

    async def test_topic_targets_are_independent_and_refresh_is_grouped(self):
        self.db.allow_repo("owner/other", 1)
        self.reader.issues["owner/repo"] = [issue(1)]
        self.reader.issues["owner/other"] = [issue(2, repo="owner/other")]
        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")
        await self.service.publish(chat_id=20, thread_id=7, repo="owner/repo")
        await self.service.publish(chat_id=20, thread_id=8, repo="owner/other")
        self.reader.calls.clear()
        self.reader.issues["owner/repo"] = [issue(3)]
        self.reader.issues["owner/other"] = [issue(4, repo="owner/other")]

        await self.service.refresh()

        self.assertCountEqual(self.reader.calls, ["owner/repo", "owner/other"])
        self.assertEqual([item[1] for item in self.bot.edited], [100, 101, 102])
        self.assertEqual(self.db.get_issue_list_message(20, 7)["telegram_message_id"], 101)
        self.assertEqual(self.db.get_issue_list_message(20, 8)["telegram_message_id"], 102)

    async def test_empty_transition_removes_stale_keyboard(self):
        self.reader.issues["owner/repo"] = [issue(1)]
        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")
        self.reader.issues["owner/repo"] = []

        await self.service.refresh()

        edit = self.bot.edited[0]
        self.assertEqual(edit[2], "No open issues for owner/repo.")
        self.assertEqual(edit[3]["reply_markup"], {"inline_keyboard": []})

    async def test_unchanged_refresh_does_not_edit(self):
        self.reader.issues["owner/repo"] = [issue(1)]
        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")

        await self.service.refresh()

        self.assertEqual(self.bot.edited, [])

    async def test_status_only_transition_updates_tracked_message_once(self):
        self.reader.issues["owner/repo"] = [issue(1)]
        self.create_code_job(
            "c-00000001",
            1,
            "planning",
            chat_id=-1001234567890,
            thread_id=42,
            message_id=77,
        )
        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")
        self.db.update_code_job("c-00000001", {"status": "ready"})

        await self.service.refresh()
        await self.service.refresh()

        self.assertEqual(len(self.bot.edited), 1)
        self.assertIn(
            '<a href="https://t.me/c/1234567890/42/77">Codex: ready</a>',
            self.bot.edited[0][2],
        )

    async def test_stale_refresh_snapshot_cannot_edit_replaced_message(self):
        self.reader.issues["owner/repo"] = [issue(1)]
        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")
        stale = self.db.get_issue_list_message(20, None)
        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")
        outgoing = self.service.render("owner/repo", [issue(2)])

        await self.service._refresh_target(
            stale,
            outgoing,
            self.service.render_hash(outgoing),
        )

        self.assertEqual(self.bot.edited, [])
        self.assertEqual(self.db.get_issue_list_message(20, None)["telegram_message_id"], 101)

    async def test_message_not_modified_synchronizes_hash(self):
        self.reader.issues["owner/repo"] = [issue(1)]
        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")
        self.reader.issues["owner/repo"] = [issue(2)]
        expected = self.service.render_hash(
            self.service.render("owner/repo", self.reader.issues["owner/repo"])
        )
        self.bot.edit_error = TelegramBotApiError("Bad Request: message is not modified")

        await self.service.refresh()

        self.assertEqual(self.db.get_issue_list_message(20, None)["render_hash"], expected)

    async def test_permanent_message_error_removes_only_current_target(self):
        self.reader.issues["owner/repo"] = [issue(1)]
        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")
        self.reader.issues["owner/repo"] = [issue(2)]
        self.bot.edit_error = TelegramBotApiError("Bad Request: message to edit not found")

        await self.service.refresh()

        self.assertIsNone(self.db.get_issue_list_message(20, None))

    async def test_transient_refresh_failure_is_audited_and_retained(self):
        self.reader.issues["owner/repo"] = [issue(1)]
        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")
        self.reader.errors["owner/repo"] = RuntimeError(" temporary\n failure ")

        await self.service.refresh()

        self.assertIsNotNone(self.db.get_issue_list_message(20, None))
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT action, details_json FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row["action"], "issues.refresh")
        self.assertEqual(json.loads(row["details_json"])["error"], "temporary failure")

    async def test_disallowed_repository_tracker_is_pruned(self):
        self.reader.issues["owner/repo"] = [issue(1)]
        await self.service.publish(chat_id=20, thread_id=None, repo="owner/repo")
        self.db.disallow_repo("owner/repo")

        await self.service.refresh()

        self.assertIsNone(self.db.get_issue_list_message(20, None))
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
        self.db.upsert_issue_list_message(20, None, 100, "owner/repo", "old")
        self.db.upsert_issue_list_message(20, None, 101, "owner/repo", "new")

        updated = self.db.update_issue_list_render_hash(
            20, None, 100, "owner/repo", "stale"
        )
        deleted = self.db.delete_issue_list_message(20, None, 100, "owner/repo")

        self.assertFalse(updated)
        self.assertFalse(deleted)
        self.assertEqual(self.db.get_issue_list_message(20, None)["render_hash"], "new")


if __name__ == "__main__":
    unittest.main()
