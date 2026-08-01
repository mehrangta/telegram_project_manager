import asyncio
import tempfile
import unittest
from pathlib import Path

from telegram_project_manager.bots.do_manager.worker import FullAccessWorker
from telegram_project_manager.bots.goal_manager.commands import GoalManager
from telegram_project_manager.bots.goal_manager.progress import GoalProgressReporter
from telegram_project_manager.bots.goal_manager.service import (
    GOAL_RUN_INTERVAL_SECONDS,
    GoalService,
)
from telegram_project_manager.platform.router import IncomingAttachment, IncomingMessage
from telegram_project_manager.platform.storage.db import Database


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []

    def send_message(self, chat_id, text, thread_id=None, **options):
        self.sent.append((chat_id, text, thread_id, options))
        return {"message_id": 100 + len(self.sent)}

    def edit_message_text(self, chat_id, message_id, text, **options):
        self.edited.append((chat_id, message_id, text, options))
        return True


class FakeCodex:
    def __init__(self, result=None):
        self.result = result or {
            "status": "continue",
            "summary": "Implemented one safe increment.",
            "next_step": "Continue with the remaining work.",
        }
        self.calls = []
        self.interrupted = []

    async def run_turn(self, **kwargs):
        self.calls.append(kwargs)
        await kwargs["on_thread"]("thread-goal")
        await kwargs["on_progress"](
            {"kind": "command", "text": "uv run python -m unittest", "status": "completed"}
        )
        return "thread-goal", self.result

    async def interrupt(self, goal_id):
        self.interrupted.append(goal_id)


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


class GoalManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = Database(root / "bot.db")
        self.db.initialize()
        self.db.upsert_user(20, "admin", "admin")
        self.db.allow_repo("owner/repo", 20)
        self.db.set_scope_repo(10, 7, "owner/repo", 20, "main")
        self.db.set_scope_local_repo(10, 7, "/cache/repo.git", 20)
        self.bot = FakeBot()
        self.codex = FakeCodex()
        self.reporter = GoalProgressReporter(
            self.db, self.bot, min_interval=0, checkin_interval=0
        )
        self.service = GoalService(
            db=self.db,
            codex=self.codex,
            reporter=self.reporter,
            workspaces=FakeWorkspaces(root / "workspaces"),
            clock=lambda: 1000,
        )
        self.manager = GoalManager(db=self.db, service=self.service)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def set_goal(self, text="Keep the repository healthy"):
        response = await self.manager.handle(
            IncomingMessage(10, 20, "admin", f"/goal set {text}", thread_id=7)
        )
        self.assertIsNone(response)
        goal = self.db.get_scope_goal(10, 7)
        self.assertIsNotNone(goal)
        return goal

    async def test_set_view_qualified_edit_pause_resume_and_clear(self):
        goal = await self.set_goal()
        self.assertEqual(goal["repo"], "owner/repo")
        self.assertEqual(goal["next_run_at"], 1000)

        viewed = await self.manager.handle(
            IncomingMessage(10, 20, "admin", "/goal@ProjectBot", thread_id=7)
        )
        self.assertIn("Keep the repository healthy", viewed.text)

        edited = await self.manager.handle(
            IncomingMessage(10, 20, "admin", "/goal edit Keep tests green", thread_id=7)
        )
        self.assertIn("Goal updated", edited.text)
        goal = self.db.get_scope_goal(10, 7)
        self.assertEqual(goal["objective_revision"], 2)

        paused = await self.manager.handle(
            IncomingMessage(10, 20, "admin", "/goal pause", thread_id=7)
        )
        self.assertIn("paused", paused.text.lower())
        self.assertEqual(self.db.get_scope_goal(10, 7)["status"], "paused")

        resumed = await self.manager.handle(
            IncomingMessage(10, 20, "admin", "/goal resume", thread_id=7)
        )
        self.assertIn("resumed", resumed.text.lower())
        self.assertEqual(self.db.get_scope_goal(10, 7)["status"], "active")

        cleared = await self.manager.handle(
            IncomingMessage(10, 20, "admin", "/goal clear", thread_id=7)
        )
        self.assertIn("cleared", cleared.text.lower())
        self.assertIsNone(self.db.get_scope_goal(10, 7))

    async def test_scope_permissions_duplicates_usage_and_attachments(self):
        unauthorized = await self.manager.handle(
            IncomingMessage(10, 99, "user", "/goal set work", thread_id=7)
        )
        self.assertIn("Unauthorized", unauthorized)

        missing = await self.manager.handle(
            IncomingMessage(99, 20, "admin", "/goal set work")
        )
        self.assertIn("No active repo", missing.text)

        await self.set_goal()
        duplicate = await self.manager.handle(
            IncomingMessage(10, 20, "admin", "/goal set replacement", thread_id=7)
        )
        self.assertIn("already has a goal", duplicate.text)
        other_scope = await self.manager.handle(
            IncomingMessage(10, 20, "admin", "/goal view", thread_id=8)
        )
        self.assertIn("No goal", other_scope.text)
        attachment = await self.manager.handle(
            IncomingMessage(
                10,
                20,
                "admin",
                "/goal view",
                thread_id=7,
                attachments=(IncomingAttachment("image", "unique", "image/png", 10),),
            )
        )
        self.assertIn("do not accept attachments", attachment.text)

    async def test_running_controls_are_persisted_for_worker(self):
        goal = await self.set_goal()
        goal_id = str(goal["id"])
        self.assertTrue(self.db.claim_goal(goal_id, 1000))

        await self.service.edit(chat_id=10, thread_id=7, objective="New objective")
        self.assertEqual(self.db.get_goal(goal_id)["control_action"], "restart")
        await self.service.pause(chat_id=10, thread_id=7)
        self.assertEqual(self.db.get_goal(goal_id)["control_action"], "pause")
        cleared = await self.service.clear(chat_id=10, thread_id=7)
        self.assertIn("stopping", cleared)
        goal = self.db.get_goal(goal_id)
        self.assertEqual(goal["status"], "clearing")
        self.assertEqual(goal["control_action"], "clear")


class GoalExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = Database(root / "bot.db")
        self.db.initialize()
        self.db.allow_repo("owner/repo", 20)
        self.bot = FakeBot()
        self.codex = FakeCodex()
        self.service = GoalService(
            db=self.db,
            codex=self.codex,
            reporter=GoalProgressReporter(self.db, self.bot, min_interval=0, checkin_interval=0),
            workspaces=FakeWorkspaces(root / "workspaces"),
            clock=lambda: 1000,
        )
        await self.service.submit(
            chat_id=10,
            user_id=20,
            thread_id=7,
            repo="owner/repo",
            branch="main",
            source_path="/cache/repo.git",
            objective="Improve reliability",
        )
        self.goal = self.db.get_scope_goal(10, 7)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_continue_persists_thread_and_schedules_next_turn(self):
        goal_id = str(self.goal["id"])
        self.assertTrue(self.service.claim(goal_id))
        await self.service.execute(goal_id)

        goal = self.db.get_goal(goal_id)
        self.assertEqual(goal["status"], "active")
        self.assertEqual(goal["next_run_at"], 1000 + GOAL_RUN_INTERVAL_SECONDS)
        self.assertEqual(goal["codex_thread_id"], "thread-goal")
        self.assertEqual(goal["run_count"], 1)
        self.assertEqual(self.codex.calls[0]["thread_id"], None)
        self.assertGreaterEqual(len(self.bot.sent), 3)

    async def test_complete_and_blocked_results_are_terminal_until_user_action(self):
        goal_id = str(self.goal["id"])
        self.codex.result = {
            "status": "complete",
            "summary": "The objective is complete.",
            "next_step": "",
        }
        self.service.claim(goal_id)
        await self.service.execute(goal_id)
        self.assertEqual(self.db.get_goal(goal_id)["status"], "completed")

        self.db.delete_goal(goal_id)
        await self.service.submit(
            chat_id=10,
            user_id=20,
            thread_id=7,
            repo="owner/repo",
            branch="main",
            source_path="/cache/repo.git",
            objective="Wait for credentials",
        )
        blocked_id = str(self.db.get_scope_goal(10, 7)["id"])
        self.codex.result = {
            "status": "blocked",
            "summary": "External access is required.",
            "next_step": "Provide the missing access.",
        }
        self.service.claim(blocked_id)
        await self.service.execute(blocked_id)
        blocked = self.db.get_goal(blocked_id)
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("missing access", blocked["error"])

    async def test_recovery_blocks_running_goal_and_removes_clearing_goal(self):
        goal_id = str(self.goal["id"])
        self.service.claim(goal_id)
        await self.service.recover()
        self.assertEqual(self.db.get_goal(goal_id)["status"], "blocked")

        self.db.delete_goal(goal_id)
        await self.service.submit(
            chat_id=10,
            user_id=20,
            thread_id=7,
            repo="owner/repo",
            branch="main",
            source_path="/cache/repo.git",
            objective="Clear me",
        )
        clearing_id = str(self.db.get_scope_goal(10, 7)["id"])
        self.service.claim(clearing_id)
        self.db.request_goal_clear(clearing_id)
        await self.service.recover()
        self.assertIsNone(self.db.get_goal(clearing_id))


class RecordingWorkService:
    def __init__(self, items, *, maximum=2):
        self.items = items
        self.max_concurrent = maximum
        self.started = []
        self.recovered = False
        self.event = asyncio.Event()

    async def recover(self):
        self.recovered = True

    def queued_jobs(self):
        return self.items

    def due_goals(self):
        return self.items

    @staticmethod
    def lane(item):
        return item["lane"]

    def claim(self, work_id):
        return True

    async def execute(self, work_id):
        self.started.append(work_id)
        await self.event.wait()

    async def interrupt(self, work_id):
        del work_id

    def mark_stopped(self, work_id):
        del work_id

    def control_action(self, work_id):
        del work_id
        return ""


class ControlledGoalService(RecordingWorkService):
    def __init__(self, items):
        super().__init__(items)
        self.control = False
        self.interrupted = []

    def control_action(self, work_id):
        del work_id
        return "pause" if self.control else ""

    async def interrupt(self, work_id):
        self.interrupted.append(work_id)


class FullAccessWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_do_jobs_have_priority_and_repository_lanes_are_shared(self):
        do = RecordingWorkService([{"id": "d-1", "lane": "owner/repo"}], maximum=2)
        goals = RecordingWorkService(
            [
                {"id": "g-1", "lane": "owner/repo"},
                {"id": "g-2", "lane": "owner/other"},
            ]
        )
        worker = FullAccessWorker(do_service=do, goal_service=goals, poll_interval=0.01)
        task = asyncio.create_task(worker.run())
        try:
            for _ in range(100):
                if do.started and goals.started:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(do.started, ["d-1"])
            self.assertEqual(goals.started, ["g-2"])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_running_goal_control_interrupts_worker_turn(self):
        do = RecordingWorkService([], maximum=1)
        goals = ControlledGoalService([{"id": "g-1", "lane": "owner/repo"}])
        worker = FullAccessWorker(do_service=do, goal_service=goals, poll_interval=0.01)
        task = asyncio.create_task(worker.run())
        try:
            for _ in range(100):
                if goals.started:
                    break
                await asyncio.sleep(0.01)
            goals.control = True
            for _ in range(100):
                if goals.interrupted:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(goals.interrupted, ["g-1"])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
