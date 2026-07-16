import asyncio
import tempfile
import unittest
from pathlib import Path

from openai_codex import Sandbox

from telegram_project_manager.bots.do_manager.commands import DoManager
from telegram_project_manager.bots.do_manager.progress import DoProgressReporter
from telegram_project_manager.bots.do_manager.service import DO_TIMEOUT_SECONDS, DoService
from telegram_project_manager.platform.router import IncomingAttachment, IncomingMessage
from telegram_project_manager.platform.storage.db import Database


PNG = bytes.fromhex("89504e470d0a1a0a") + b"payload"


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []
        self.downloads = {"image": PNG}

    def send_message(self, chat_id, text, thread_id=None, **options):
        self.sent.append((chat_id, text, thread_id, options))
        return {"message_id": 100 + len(self.sent)}

    def edit_message_text(self, chat_id, message_id, text, **options):
        self.edited.append((chat_id, message_id, text, options))
        return True

    def download_file(self, file_id, maximum):
        del maximum
        return self.downloads[file_id]


class FakeCodex:
    def __init__(self, result="done with sk-secret-token"):
        self.result = result
        self.calls = []
        self.interrupted = []

    async def run_text_turn(self, **kwargs):
        self.calls.append(kwargs)
        await kwargs["on_progress"](
            {"kind": "command", "text": "pytest -q", "status": "completed"}
        )
        for path in kwargs.get("image_paths", ()):
            assert Path(path).is_file()
        return "thread-do", self.result

    async def interrupt(self, job_id):
        self.interrupted.append(job_id)


class FakeWorkspaces:
    def __init__(self, root):
        self.root = Path(root)
        self.prepared = []

    def validate_source(self, *, source_path, repo):
        if not source_path:
            raise ValueError("missing cache")
        return source_path

    def prepare(self, **kwargs):
        self.prepared.append(kwargs)
        path = self.root / kwargs["repo"].replace("/", "--")
        path.mkdir(parents=True, exist_ok=True)
        return path


class RecordingService:
    def __init__(self):
        self.calls = []
        self.status_calls = []

    async def submit(self, **kwargs):
        self.calls.append(kwargs)
        return "d-12345678"

    def status(self, **kwargs):
        self.status_calls.append(kwargs)
        return "Do Job ID: d-12345678\nStatus: running"


async def wait_for_status(db, job_id, status):
    for _ in range(300):
        job = db.get_do_job(job_id)
        if job and job["status"] == status:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach {status}")


class DoManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "bot.db")
        self.db.initialize()
        self.db.upsert_user(20, "admin", "admin")
        self.db.allow_repo("owner/repo", 20)
        self.db.set_scope_repo(10, 7, "owner/repo", 20, "main")
        self.db.set_scope_local_repo(10, 7, "/cache/repo.git", 20)
        self.service = RecordingService()
        self.manager = DoManager(db=self.db, service=self.service)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_topic_repo_job_preserves_multiline_text_and_images(self):
        response = await self.manager.handle(
            IncomingMessage(
                10, 20, "admin", "/do first line\n second line",
                thread_id=7, message_id=40,
                attachments=(IncomingAttachment("image", "unique", "image/png", 100),),
            )
        )
        self.assertIsNone(response)
        call = self.service.calls[0]
        self.assertEqual(call["mode"], "repo")
        self.assertEqual(call["repo"], "owner/repo")
        self.assertEqual(call["job"], "first line\n second line")
        self.assertEqual(call["thread_id"], 7)
        self.assertEqual(len(call["attachments"]), 1)

    async def test_host_mode_is_private_only(self):
        rejected = await self.manager.handle(
            IncomingMessage(10, 20, "admin", "/do --host restart", message_id=41)
        )
        self.assertIn("private admin", rejected.text)
        accepted = await self.manager.handle(
            IncomingMessage(10, 20, "admin", "/do --host restart", is_private=True)
        )
        self.assertIsNone(accepted)
        self.assertEqual(self.service.calls[-1]["mode"], "host")

    async def test_status_is_scoped(self):
        response = await self.manager.handle(
            IncomingMessage(10, 20, "admin", "/do status d-12345678", thread_id=7)
        )
        self.assertIn("d-12345678", response.text)
        self.assertEqual(
            self.service.status_calls[0],
            {"chat_id": 10, "thread_id": 7, "job_id": "d-12345678"},
        )

    async def test_missing_repo_and_non_admin_are_rejected(self):
        missing = await self.manager.handle(
            IncomingMessage(99, 20, "admin", "/do work", message_id=42)
        )
        unauthorized = await self.manager.handle(
            IncomingMessage(10, 99, "user", "/do work", thread_id=7)
        )
        self.assertIn("No active repo", missing.text)
        self.assertIn("Unauthorized", unauthorized)


class DoServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = Database(root / "bot.db")
        self.db.initialize()
        self.bot = FakeBot()
        self.codex = FakeCodex()
        self.workspaces = FakeWorkspaces(root / "workspaces")
        self.reporter = DoProgressReporter(self.db, self.bot, min_interval=0)
        self.host = root / "host"
        self.host.mkdir()
        self.service = DoService(
            db=self.db,
            codex=self.codex,
            bot=self.bot,
            reporter=self.reporter,
            workspaces=self.workspaces,
            host_working_directory=self.host,
            payload_root=root / "payloads",
        )

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def run_until_terminal(self, job_id, status="completed"):
        worker = asyncio.create_task(self.service.run_worker())
        try:
            return await wait_for_status(self.db, job_id, status)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_host_job_runs_in_worker_reports_and_redacts_result(self):
        job_id = await self.service.submit(
            chat_id=10, user_id=20, thread_id=None, message_id=40,
            mode="host", job="perform task",
        )
        job = await self.run_until_terminal(job_id)
        self.assertEqual(job["status"], "completed")
        call = self.codex.calls[0]
        self.assertEqual(call["cwd"], str(self.host.resolve()))
        self.assertEqual(call["sandbox"], Sandbox.full_access)
        self.assertEqual(call["timeout_seconds"], DO_TIMEOUT_SECONDS)
        self.assertEqual(DO_TIMEOUT_SECONDS, 10 * 60 * 60)
        self.assertTrue(self.bot.edited)
        self.assertNotIn("sk-secret-token", self.bot.sent[-1][1])
        self.assertFalse(Path(job["payload_path"]).exists())

    async def test_repo_job_forwards_image_and_uses_persistent_workspace(self):
        attachment = IncomingAttachment("image", "unique", "image/png", len(PNG))
        job_id = await self.service.submit(
            chat_id=10, user_id=20, thread_id=7, message_id=40,
            mode="repo", repo="owner/repo", branch="main", source_path="/cache/repo.git",
            job="inspect image", attachments=(attachment,),
        )
        job = await self.run_until_terminal(job_id)
        self.assertEqual(job["image_count"], 1)
        self.assertEqual(self.workspaces.prepared[0]["repo"], "owner/repo")
        self.assertEqual(len(self.codex.calls[0]["image_paths"]), 1)
        self.assertFalse(Path(job["payload_path"]).exists())

    async def test_status_rejects_cross_topic(self):
        job_id = await self.service.submit(
            chat_id=10, user_id=20, thread_id=7, message_id=40,
            mode="host", job="work",
        )
        with self.assertRaisesRegex(ValueError, "different chat or topic"):
            self.service.status(chat_id=10, thread_id=8, job_id=job_id)

    async def test_queue_limit_is_durable(self):
        limited = DoService(
            db=self.db, codex=self.codex, bot=self.bot, reporter=self.reporter,
            workspaces=self.workspaces, host_working_directory=self.host,
            payload_root=Path(self.temp.name) / "other-payloads", max_outstanding=0,
        )
        with self.assertRaisesRegex(ValueError, "queue is full"):
            await limited.submit(
                chat_id=10, user_id=20, thread_id=None, message_id=40,
                mode="host", job="work",
            )

    async def test_running_jobs_are_interrupted_on_worker_recovery(self):
        job_id = await self.service.submit(
            chat_id=10, user_id=20, thread_id=None, message_id=40,
            mode="host", job="work",
        )
        self.assertTrue(self.db.claim_do_job(job_id))
        interrupted = self.db.mark_running_do_jobs_interrupted()
        self.assertEqual(interrupted, [job_id])
        self.assertEqual(self.db.get_do_job(job_id)["status"], "interrupted")

    async def test_worker_runs_two_lanes_but_serializes_same_repo(self):
        class BlockingCodex(FakeCodex):
            def __init__(self):
                super().__init__("done")
                self.release = asyncio.Event()
                self.active = 0
                self.maximum = 0

            async def run_text_turn(self, **kwargs):
                self.calls.append(kwargs)
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                await self.release.wait()
                self.active -= 1
                return "thread", "done"

        codex = BlockingCodex()
        self.service.codex = codex
        first = await self.service.submit(
            chat_id=10, user_id=20, thread_id=7, message_id=1, mode="repo",
            repo="owner/repo", branch="main", source_path="/cache/repo.git", job="one",
        )
        second = await self.service.submit(
            chat_id=10, user_id=20, thread_id=7, message_id=2, mode="repo",
            repo="owner/repo", branch="main", source_path="/cache/repo.git", job="two",
        )
        host = await self.service.submit(
            chat_id=10, user_id=20, thread_id=None, message_id=3, mode="host", job="host",
        )
        worker = asyncio.create_task(self.service.run_worker())
        try:
            for _ in range(200):
                if len(codex.calls) == 2:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(codex.calls), 2)
            repo_statuses = {
                self.db.get_do_job(first)["status"],
                self.db.get_do_job(second)["status"],
            }
            self.assertEqual(repo_statuses, {"running", "queued"})
            self.assertEqual(self.db.get_do_job(host)["status"], "running")
            codex.release.set()
            await wait_for_status(self.db, second, "completed")
            self.assertEqual(codex.maximum, 2)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
