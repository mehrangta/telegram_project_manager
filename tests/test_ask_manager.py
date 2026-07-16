import asyncio
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path

from openai_codex import Sandbox
from openai_codex.types import ReasoningEffort

from telegram_project_manager.bots.ask_manager.commands import AskManager
from telegram_project_manager.bots.ask_manager.service import AskService
from telegram_project_manager.bots.code_manager.workspace import GitWorkspaceService
from telegram_project_manager.platform.router import IncomingMessage
from telegram_project_manager.platform.storage.db import Database


class FakeBot:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, thread_id=None, **options):
        self.sent.append((chat_id, text, thread_id, options))
        return {"message_id": 100 + len(self.sent)}


class FakeCodex:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {
            "answer": "The entry point is src/app.py.",
            "sources": ["src/app.py", "missing.py"],
        }

    async def run_turn(self, **kwargs):
        self.calls.append(kwargs)
        await kwargs["on_thread"]("thread-ask")
        await kwargs["on_progress"]({"kind": "phase"})
        return "thread-ask", self.result


class FakeWorkspaces:
    def __init__(self):
        self.validated = []
        self.prepared = []
        self.cleaned = []

    def validate_source(self, *, source_path, repo):
        self.validated.append((source_path, repo))
        if not source_path:
            raise ValueError("missing local repository")
        return source_path

    def prepare_read_only(self, *, path, **kwargs):
        self.prepared.append((path, kwargs))
        (path / "src").mkdir(parents=True)
        (path / "src" / "app.py").write_text("print('ok')", encoding="utf-8")
        return "abcdef1234567890"

    def cleanup_read_only(self, *, source_path, path):
        self.cleaned.append((source_path, path))


async def wait_for_messages(bot, count):
    for _ in range(200):
        if len(bot.sent) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {count} messages, got {len(bot.sent)}")


class AskManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "bot.db")
        self.db.initialize()
        self.db.upsert_user(20, "admin", "admin")
        self.db.allow_repo("owner/topic", 20)
        self.db.set_scope_repo(10, 30, "owner/topic", 20, "develop")
        self.db.set_scope_local_repo(10, 30, "/cache/topic.git", 20)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_ask_uses_current_topic_settings_and_bot_qualified_command(self):
        class Service:
            def __init__(self):
                self.calls = []

            async def submit(self, **kwargs):
                self.calls.append(kwargs)
                return "a-12345678"

        service = Service()
        manager = AskManager(db=self.db, service=service)
        response = await manager.handle(
            IncomingMessage(
                10, 20, "admin", "/ask@ProjectBot where is startup?",
                message_id=40, thread_id=30,
            )
        )

        self.assertIsNone(response)
        self.assertEqual(service.calls[0]["repo"], "owner/topic")
        self.assertEqual(service.calls[0]["branch"], "develop")
        self.assertEqual(service.calls[0]["source_path"], "/cache/topic.git")
        self.assertEqual(service.calls[0]["message_id"], 40)

    async def test_usage_and_missing_topic_repo_reply_to_command(self):
        manager = AskManager(db=self.db, service=object())
        usage = await manager.handle(
            IncomingMessage(10, 20, "admin", "/ask", message_id=41, thread_id=30)
        )
        missing = await manager.handle(
            IncomingMessage(10, 20, "admin", "/ask question", message_id=42, thread_id=31)
        )

        self.assertEqual(usage.reply_to_message_id, 41)
        self.assertIn("Usage", usage.text)
        self.assertEqual(missing.reply_to_message_id, 42)
        self.assertIn("No active repo", missing.text)


class AskServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "bot.db")
        self.db.initialize()
        self.bot = FakeBot()
        self.codex = FakeCodex()
        self.workspaces = FakeWorkspaces()
        self.service = AskService(
            db=self.db,
            codex=self.codex,
            workspaces=self.workspaces,
            bot=self.bot,
        )

    async def asyncTearDown(self):
        await self.service.shutdown()
        self.temp.cleanup()

    async def test_submit_acknowledges_then_sends_read_only_grounded_answer(self):
        await self.service.submit(
            chat_id=10,
            user_id=20,
            thread_id=30,
            message_id=40,
            repo="owner/repo",
            branch="main",
            source_path="/cache/repo.git",
            question="Where is startup?",
        )
        await wait_for_messages(self.bot, 2)

        self.assertIn("question queued", self.bot.sent[0][1])
        self.assertIn("Repository answer", self.bot.sent[1][1])
        self.assertIn("src/app.py", self.bot.sent[1][1])
        self.assertNotIn("missing.py", self.bot.sent[1][1])
        for sent in self.bot.sent:
            self.assertEqual(sent[2], 30)
            self.assertEqual(sent[3]["reply_to_message_id"], 40)
        call = self.codex.calls[0]
        self.assertEqual(call["sandbox"], Sandbox.read_only)
        self.assertEqual(call["effort"], ReasoningEffort.high)
        self.assertEqual(call["model_role"], "plan")
        self.assertIsNone(call["thread_id"])
        for _ in range(200):
            if self.workspaces.cleaned:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(self.workspaces.cleaned), 1)

    async def test_full_queue_rejects_before_acknowledging(self):
        service = AskService(
            db=self.db,
            codex=self.codex,
            workspaces=self.workspaces,
            bot=self.bot,
            max_outstanding=0,
        )
        with self.assertRaisesRegex(ValueError, "queue is full"):
            await service.submit(
                chat_id=10, user_id=20, thread_id=None, message_id=40,
                repo="owner/repo", branch="main", source_path="/cache/repo.git",
                question="Question",
            )
        self.assertEqual(self.bot.sent, [])


class GitReadOnlyWorkspaceTests(unittest.TestCase):
    def test_prepare_and_cleanup_use_detached_worktree(self):
        class Repositories:
            def validate(self, source_path, repo):
                return Path(source_path)

            def fetch(self, source, branch):
                return source, "base-sha"

            def lock_for(self, source):
                return nullcontext()

        class Commands:
            def __init__(self):
                self.calls = []

            def run(self, args, **kwargs):
                self.calls.append((args, kwargs))
                return ""

        commands = Commands()
        service = GitWorkspaceService(commands=commands, repositories=Repositories())
        with tempfile.TemporaryDirectory() as temp_dir:
            source = str(Path(temp_dir) / "repo.git")
            path = Path(temp_dir) / "ask" / "repo"
            commit = service.prepare_read_only(
                source_path=source,
                repo="owner/repo",
                base_branch="main",
                path=path,
            )
            service.cleanup_read_only(source_path=source, path=path)

        self.assertEqual(commit, "base-sha")
        add = next(args for args, _ in commands.calls if "add" in args)
        self.assertIn("--detach", add)
        self.assertIn("refs/remotes/origin/main", add)
        self.assertTrue(any("remove" in args for args, _ in commands.calls))
