import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openai_codex import Sandbox
from openai_codex.types import ReasoningEffort

from telegram_project_manager.bots.code_manager.codex_sdk import CodexSdkError
from telegram_project_manager.bots.do_manager.commands import DoManager
from telegram_project_manager.bots.do_manager.service import DoService
from telegram_project_manager.platform.router import IncomingMessage
from telegram_project_manager.platform.storage.db import Database
from telegram_project_manager.platform.telegram_bot import TelegramBotApiError


class FakeBot:
    def __init__(self, fail_at=None):
        self.sent = []
        self.fail_at = fail_at
        self.calls = 0

    def send_message(self, chat_id, text, thread_id=None, **options):
        self.calls += 1
        if self.fail_at == self.calls:
            raise TelegramBotApiError("send failed with sk-secret-token")
        self.sent.append((chat_id, text, thread_id, options))
        return {"message_id": 100 + self.calls}


class FakeCodex:
    def __init__(self, result="Changed <host> with sk-secret-token and Authorization: Bearer abc"):
        self.result = result
        self.calls = []
        self.interrupted = []

    async def run_text_turn(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return "thread-do", self.result

    async def interrupt(self, job_id):
        self.interrupted.append(job_id)


class RecordingService:
    def __init__(self):
        self.calls = []

    async def submit(self, **kwargs):
        self.calls.append(kwargs)
        return "d-12345678"


async def wait_for_messages(bot, count):
    for _ in range(200):
        if len(bot.sent) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {count} messages, got {len(bot.sent)}")


async def wait_for_idle(service):
    for _ in range(200):
        if not service._tasks:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("expected full-access service to become idle")


async def run_direct(function, *args, **kwargs):
    return function(*args, **kwargs)


class DoManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "bot.db")
        self.db.initialize()
        self.db.upsert_user(20, "admin", "admin")

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_private_bot_qualified_command_preserves_multiline_job(self):
        service = RecordingService()
        manager = DoManager(db=self.db, service=service)

        response = await manager.handle(
            IncomingMessage(
                10,
                20,
                "admin",
                "/do@ProjectBot first line\n  second line",
                is_private=True,
                message_id=40,
            )
        )

        self.assertIsNone(response)
        self.assertEqual(service.calls[0]["job"], "first line\n  second line")
        self.assertEqual(service.calls[0]["message_id"], 40)

    async def test_group_admin_is_rejected_before_submission(self):
        service = RecordingService()
        manager = DoManager(db=self.db, service=service)

        response = await manager.handle(
            IncomingMessage(10, 20, "admin", "/do change host", message_id=41)
        )

        self.assertIn("only in private admin chats", response.text)
        self.assertEqual(response.reply_to_message_id, 41)
        self.assertEqual(service.calls, [])

    async def test_non_admin_and_empty_job_are_rejected(self):
        service = RecordingService()
        manager = DoManager(db=self.db, service=service)

        unauthorized = await manager.handle(
            IncomingMessage(10, 99, "user", "/do work", is_private=True)
        )
        usage = await manager.handle(
            IncomingMessage(10, 20, "admin", "/do", is_private=True, message_id=42)
        )

        self.assertIn("Unauthorized", unauthorized)
        self.assertIn("Usage: /do", usage.text)
        self.assertEqual(usage.reply_to_message_id, 42)
        self.assertEqual(service.calls, [])


class DoServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.to_thread_patch = mock.patch(
            "telegram_project_manager.bots.do_manager.service.asyncio.to_thread",
            new=run_direct,
        )
        self.to_thread_patch.start()
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "bot.db")
        self.db.initialize()
        self.bot = FakeBot()
        self.codex = FakeCodex()
        self.working_directory = Path(self.temp.name) / "service-root"
        self.working_directory.mkdir()
        self.service = DoService(
            db=self.db,
            codex=self.codex,
            bot=self.bot,
            working_directory=self.working_directory,
        )

    async def asyncTearDown(self):
        await self.service.shutdown()
        self.temp.cleanup()
        self.to_thread_patch.stop()

    def audit_rows(self):
        with self.db.session() as conn:
            return conn.execute(
                "SELECT plan_id, action, status, details_json FROM audit_events ORDER BY id"
            ).fetchall()

    async def test_job_uses_full_access_and_replies_with_redacted_plain_result(self):
        await self.service.submit(
            chat_id=10,
            user_id=20,
            message_id=40,
            job="perform private task",
        )
        await wait_for_messages(self.bot, 2)
        await wait_for_idle(self.service)

        self.assertIn("job queued", self.bot.sent[0][1])
        self.assertIn("job completed", self.bot.sent[1][1])
        self.assertIn("&lt;host&gt;", self.bot.sent[1][1])
        self.assertNotIn("sk-secret-token", self.bot.sent[1][1])
        self.assertNotIn("Bearer abc", self.bot.sent[1][1])
        for sent in self.bot.sent:
            self.assertEqual(sent[0], 10)
            self.assertIsNone(sent[2])
            self.assertEqual(sent[3]["reply_to_message_id"], 40)
        call = self.codex.calls[0]
        self.assertEqual(call["cwd"], str(self.working_directory.resolve()))
        self.assertEqual(call["prompt"], "perform private task")
        self.assertEqual(call["sandbox"], Sandbox.full_access)
        self.assertEqual(call["effort"], ReasoningEffort.high)
        self.assertEqual(call["model_role"], "code")
        self.assertIsNone(call["thread_id"])
        self.assertEqual(call["timeout_seconds"], 2 * 60 * 60)
        rows = self.audit_rows()
        self.assertEqual([row["status"] for row in rows], ["queued", "ok"])
        audit_json = " ".join(row["details_json"] for row in rows)
        self.assertNotIn("perform private task", audit_json)
        self.assertNotIn("Changed", audit_json)

    async def test_full_queue_rejects_before_acknowledging(self):
        service = DoService(
            db=self.db,
            codex=self.codex,
            bot=self.bot,
            working_directory=self.working_directory,
            max_outstanding=0,
        )

        with self.assertRaisesRegex(ValueError, "queue is full"):
            await service.submit(chat_id=10, user_id=20, message_id=40, job="work")

        self.assertEqual(self.bot.sent, [])

    async def test_failure_is_redacted_and_does_not_retry(self):
        self.codex.result = CodexSdkError("provider rejected sk-secret-token")

        await self.service.submit(chat_id=10, user_id=20, message_id=40, job="work")
        await wait_for_messages(self.bot, 2)
        await wait_for_idle(self.service)

        self.assertEqual(len(self.codex.calls), 1)
        self.assertIn("job failed", self.bot.sent[1][1])
        self.assertNotIn("sk-secret-token", self.bot.sent[1][1])
        self.assertEqual([row["status"] for row in self.audit_rows()], ["queued", "failed"])

    async def test_result_delivery_failure_is_audited_without_rerun(self):
        self.bot = FakeBot(fail_at=2)
        self.service.bot = self.bot

        await self.service.submit(chat_id=10, user_id=20, message_id=40, job="work")
        for _ in range(200):
            if any(row["action"] == "do.reply" for row in self.audit_rows()):
                break
            await asyncio.sleep(0.01)
        await wait_for_idle(self.service)

        self.assertEqual(len(self.codex.calls), 1)
        rows = self.audit_rows()
        self.assertEqual([row["action"] for row in rows], ["do.execute", "do.execute", "do.reply"])
        self.assertEqual(rows[-1]["status"], "failed")
        self.assertNotIn("sk-secret-token", rows[-1]["details_json"])

    async def test_jobs_execute_serially(self):
        class BlockingCodex(FakeCodex):
            def __init__(self):
                super().__init__("done")
                self.active = 0
                self.maximum_active = 0
                self.release = asyncio.Event()

            async def run_text_turn(self, **kwargs):
                self.calls.append(kwargs)
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
                await self.release.wait()
                self.active -= 1
                return "thread-do", self.result

        codex = BlockingCodex()
        self.service.codex = codex

        await self.service.submit(chat_id=10, user_id=20, message_id=40, job="one")
        await self.service.submit(chat_id=10, user_id=20, message_id=41, job="two")
        for _ in range(200):
            if len(codex.calls) == 1:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(codex.calls), 1)

        codex.release.set()
        await wait_for_messages(self.bot, 4)
        await wait_for_idle(self.service)

        self.assertEqual(len(codex.calls), 2)
        self.assertEqual(codex.maximum_active, 1)

    async def test_shutdown_interrupts_outstanding_job(self):
        class HangingCodex(FakeCodex):
            def __init__(self):
                super().__init__("done")
                self.started = asyncio.Event()

            async def run_text_turn(self, **kwargs):
                self.calls.append(kwargs)
                self.started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        codex = HangingCodex()
        self.service.codex = codex
        do_id = await self.service.submit(chat_id=10, user_id=20, message_id=40, job="hang")
        await codex.started.wait()

        await self.service.shutdown()

        self.assertEqual(codex.interrupted, [do_id])
        self.assertEqual(self.service._tasks, {})
        self.assertEqual(self.audit_rows()[-1]["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
