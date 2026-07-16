import tempfile
import unittest
from pathlib import Path

from telegram_project_manager.bots.code_manager.service import (
    CODEX_QUEUED_STATUSES,
    CODEX_RUNNING_STATUSES,
    CodeJobService,
)
from telegram_project_manager.bots.codex_queue.commands import CodexQueueManager
from telegram_project_manager.platform.responses import OutgoingMessage
from telegram_project_manager.platform.router import IncomingMessage
from telegram_project_manager.platform.storage.db import Database


class FakeQueueService:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []

    def queue_snapshot(self, **kwargs):
        self.calls.append(kwargs)
        return self.snapshot


class CodexQueueManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "bot.db")
        self.db.initialize()
        self.db.upsert_user(20, "admin", "admin")

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_renders_mixed_scoped_queue_and_escapes_dynamic_text(self):
        code = FakeQueueService(
            {
                "running": (
                    {
                        "id": "c-11111111",
                        "repo": "owner/code",
                        "issue_number": 8,
                        "status": "planning",
                    },
                ),
                "queued": (),
            }
        )
        asks = FakeQueueService(
            {
                "running": (),
                "queued": (
                    {
                        "id": "a-22222222",
                        "repo": "owner/ask",
                        "branch": "main",
                        "question": "Explain <unsafe & value>",
                        "image_count": 2,
                    },
                ),
            }
        )
        manager = CodexQueueManager(db=self.db, code_service=code, ask_service=asks)

        response = await manager.handle(
            IncomingMessage(10, 20, "admin", "/queue@ProjectBot", thread_id=30)
        )

        self.assertIsInstance(response, OutgoingMessage)
        assert isinstance(response, OutgoingMessage)
        self.assertIn("Code jobs", response.text)
        self.assertIn("Running (1)", response.text)
        self.assertIn("c-11111111 owner/code#8", response.text)
        self.assertIn("Repository questions", response.text)
        self.assertIn("Queued (1)", response.text)
        self.assertIn("a-22222222 owner/ask@main · 2 images", response.text)
        self.assertIn("&lt;unsafe &amp; value&gt;", response.text)
        self.assertEqual(code.calls, [{"chat_id": 10, "thread_id": 30}])
        self.assertEqual(asks.calls, [{"chat_id": 10, "thread_id": 30}])

    async def test_empty_scope_and_unexpected_arguments(self):
        empty = FakeQueueService({"running": (), "queued": ()})
        manager = CodexQueueManager(db=self.db, code_service=empty, ask_service=empty)

        topic = await manager.handle(
            IncomingMessage(10, 20, "admin", "/queue", thread_id=30)
        )
        chat = await manager.handle(IncomingMessage(10, 20, "admin", "/queue"))
        usage = await manager.handle(
            IncomingMessage(10, 20, "admin", "/queue extra", thread_id=30)
        )

        self.assertEqual(topic, "No Codex work is running or queued for this topic.")
        self.assertEqual(chat, "No Codex work is running or queued for this chat.")
        self.assertEqual(usage, "Usage: /queue")


class CodeQueueSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "bot.db")
        self.db.initialize()
        self.service = CodeJobService(
            db=self.db,
            codex=object(),
            workspaces=object(),
            github=object(),
            reporter=object(),
        )

    def tearDown(self):
        self.temp.cleanup()

    def create_job(
        self,
        job_id: str,
        status: str,
        *,
        chat_id: int = 10,
        thread_id: int | None = 30,
        updated_at: int,
    ) -> None:
        self.db.create_code_job(
            {
                "id": job_id,
                "telegram_chat_id": chat_id,
                "telegram_user_id": 20,
                "telegram_thread_id": thread_id,
                "repo": "owner/repo",
                "issue_number": int(job_id[-2:], 16),
                "issue_title": "Queue issue",
                "issue_url": "https://github.com/owner/repo/issues/1",
                "issue_context_json": {},
                "base_branch": "main",
                "target_branch": f"branch-{job_id}",
                "workspace_path": f"/tmp/{job_id}",
                "source_repo_path": "/tmp/repo",
                "status": status,
                "resume_phase": "plan",
                "skip_plan": False,
            }
        )
        with self.db.session() as conn:
            conn.execute(
                "UPDATE code_jobs SET created_at = ?, updated_at = ? WHERE id = ?",
                (updated_at, updated_at, job_id),
            )

    def test_snapshot_filters_scope_statuses_and_orders_oldest_first(self):
        self.create_job("c-00000001", "planning", updated_at=4)
        self.create_job("c-00000002", "queued_code", updated_at=3)
        self.create_job("c-00000003", "queued_plan", updated_at=2)
        self.create_job("c-00000004", "awaiting_approval", updated_at=1)
        self.create_job("c-00000005", "queued_plan", thread_id=31, updated_at=1)
        self.create_job("c-00000006", "coding", chat_id=11, updated_at=1)

        snapshot = self.service.queue_snapshot(chat_id=10, thread_id=30)

        self.assertEqual([item["id"] for item in snapshot["running"]], ["c-00000001"])
        self.assertEqual(
            [item["id"] for item in snapshot["queued"]],
            ["c-00000003", "c-00000002"],
        )

    def test_all_queue_statuses_match_admission_count(self):
        for index, status in enumerate(sorted(CODEX_QUEUED_STATUSES), 1):
            self.create_job(f"c-{index:08x}", status, updated_at=index)
        for index, status in enumerate(sorted(CODEX_RUNNING_STATUSES), 20):
            self.create_job(f"c-{index:08x}", status, updated_at=index)

        snapshot = self.service.queue_snapshot(chat_id=10, thread_id=30)

        self.assertEqual(
            {item["status"] for item in snapshot["queued"]},
            set(CODEX_QUEUED_STATUSES),
        )
        self.assertEqual(
            {item["status"] for item in snapshot["running"]},
            set(CODEX_RUNNING_STATUSES),
        )
        self.assertEqual(self.db.count_queued_code_jobs(), len(CODEX_QUEUED_STATUSES))


if __name__ == "__main__":
    unittest.main()
