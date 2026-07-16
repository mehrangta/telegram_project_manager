import asyncio
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from openai_codex import Sandbox
from openai_codex.types import ReasoningEffort

from telegram_project_manager.bots.ask_manager.commands import AskManager
from telegram_project_manager.bots.ask_manager.images import (
    MAX_IMAGE_BYTES,
    MAX_TOTAL_IMAGE_BYTES,
    stage_attachments,
    validate_attachments,
)
from telegram_project_manager.bots.ask_manager.service import AskService
from telegram_project_manager.bots.code_manager.workspace import GitWorkspaceService
from telegram_project_manager.platform.router import IncomingAttachment, IncomingMessage
from telegram_project_manager.platform.storage.db import Database
from telegram_project_manager.platform.telegram_bot import TelegramBotApiError


class FakeBot:
    def __init__(self):
        self.sent = []
        self.files = {}
        self.download_errors = {}
        self.downloaded = []

    def send_message(self, chat_id, text, thread_id=None, **options):
        self.sent.append((chat_id, text, thread_id, options))
        return {"message_id": 100 + len(self.sent)}

    def download_file(self, file_id, max_bytes):
        self.downloaded.append((file_id, max_bytes))
        if file_id in self.download_errors:
            raise self.download_errors[file_id]
        return self.files[file_id]


class FakeCodex:
    def __init__(self, result=None):
        self.calls = []
        self.image_inputs = []
        self.result = result or {
            "answer": "The entry point is src/app.py.",
            "sources": ["src/app.py", "missing.py"],
        }

    async def run_turn(self, **kwargs):
        self.calls.append(kwargs)
        self.image_inputs.append(
            [(Path(path).name, Path(path).read_bytes()) for path in kwargs["image_paths"]]
        )
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


async def wait_for_completion(service, ask_id):
    for _ in range(200):
        if ask_id not in service._tasks:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"ask job did not finish: {ask_id}")


async def run_inline(function, /, *args, **kwargs):
    return function(*args, **kwargs)


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
                attachments=(IncomingAttachment("image", "unique", "image/png", 100),),
                message_id=40,
                thread_id=30,
            )
        )

        self.assertIsNone(response)
        self.assertEqual(service.calls[0]["repo"], "owner/topic")
        self.assertEqual(service.calls[0]["branch"], "develop")
        self.assertEqual(service.calls[0]["source_path"], "/cache/topic.git")
        self.assertEqual(service.calls[0]["message_id"], 40)
        self.assertEqual(service.calls[0]["attachments"][0].file_id, "image")

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

    async def test_invalid_attachment_is_rejected_before_submit(self):
        class Service:
            def __init__(self):
                self.calls = []

            async def submit(self, **kwargs):
                self.calls.append(kwargs)

        service = Service()
        manager = AskManager(db=self.db, service=service)
        response = await manager.handle(
            IncomingMessage(
                10,
                20,
                "admin",
                "/ask inspect this",
                attachments=(IncomingAttachment("image", "unique", "image/webp", 100),),
                message_id=43,
                thread_id=30,
            )
        )

        self.assertEqual(response.reply_to_message_id, 43)
        self.assertIn("Unsupported image type", response.text)
        self.assertEqual(service.calls, [])


class AskImageTests(unittest.TestCase):
    def test_metadata_validation_enforces_supported_types_and_limits(self):
        with self.assertRaisesRegex(ValueError, "Unsupported image type"):
            validate_attachments((IncomingAttachment("1", "1", "image/webp", 1),))
        with self.assertRaisesRegex(ValueError, "Maximum: 10"):
            validate_attachments(
                tuple(IncomingAttachment(str(index), str(index), "image/png", 1) for index in range(11))
            )
        with self.assertRaisesRegex(ValueError, "10 MB or smaller"):
            validate_attachments(
                (IncomingAttachment("1", "1", "image/png", MAX_IMAGE_BYTES + 1),)
            )
        with self.assertRaisesRegex(ValueError, "20 MB or smaller"):
            validate_attachments(
                (
                    IncomingAttachment("1", "1", "image/png", MAX_IMAGE_BYTES),
                    IncomingAttachment("2", "2", "image/png", MAX_IMAGE_BYTES),
                    IncomingAttachment("3", "3", "image/png", 1),
                )
            )

    def test_stages_verified_images_in_attachment_order(self):
        bot = FakeBot()
        png = bytes.fromhex("89504e470d0a1a0a") + b"png"
        jpeg = bytes.fromhex("ffd8ff") + b"jpeg"
        bot.files = {"png": png, "jpeg": jpeg}
        attachments = (
            IncomingAttachment("png", "1", "image/png", len(png)),
            IncomingAttachment("jpeg", "2", "image/jpeg", len(jpeg)),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = stage_attachments(
                bot=bot,
                attachments=attachments,
                destination=Path(temp_dir) / "images",
            )
            self.assertEqual([Path(path).name for path in paths], ["1.png", "2.jpg"])
            self.assertEqual([Path(path).read_bytes() for path in paths], [png, jpeg])
        self.assertEqual(bot.downloaded, [("png", MAX_IMAGE_BYTES), ("jpeg", MAX_IMAGE_BYTES)])

    def test_staging_rechecks_actual_total_size(self):
        bot = FakeBot()
        bot.files = {
            "png": bytes.fromhex("89504e470d0a1a0a") + b"p" * (MAX_IMAGE_BYTES - 8),
            "jpeg": bytes.fromhex("ffd8ff") + b"j" * (MAX_IMAGE_BYTES - 3),
            "gif": bytes.fromhex("474946383961") + b"gif",
        }
        attachments = (
            IncomingAttachment("png", "1", "image/png", 0),
            IncomingAttachment("jpeg", "2", "image/jpeg", 0),
            IncomingAttachment("gif", "3", "image/gif", 0),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "20 MB or smaller"):
                stage_attachments(
                    bot=bot,
                    attachments=attachments,
                    destination=Path(temp_dir) / "images",
                )

    def test_staging_rejects_invalid_content_and_sanitizes_download_errors(self):
        bot = FakeBot()
        bot.files["invalid"] = b"not a png"
        bot.download_errors["failed"] = TelegramBotApiError("provider detail")
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "not valid image/png"):
                stage_attachments(
                    bot=bot,
                    attachments=(IncomingAttachment("invalid", "1", "image/png", 9),),
                    destination=Path(temp_dir) / "invalid-images",
                )
            with self.assertRaisesRegex(ValueError, "Telegram image download failed") as error:
                stage_attachments(
                    bot=bot,
                    attachments=(IncomingAttachment("failed", "2", "image/png", 9),),
                    destination=Path(temp_dir) / "failed-images",
                )
        self.assertNotIn("provider detail", str(error.exception))

    def test_staging_does_not_follow_repository_symlinks_or_overwrite_files(self):
        bot = FakeBot()
        png = bytes.fromhex("89504e470d0a1a0a") + b"png"
        bot.files["png"] = png
        attachment = (IncomingAttachment("png", "1", "image/png", len(png)),)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            existing = root / "existing"
            existing.mkdir()
            with self.assertRaisesRegex(ValueError, "staging path is unavailable"):
                stage_attachments(bot=bot, attachments=attachment, destination=existing)

            outside = root / "outside"
            outside.mkdir()
            repository = root / "repo"
            repository.mkdir()
            (repository / ".codex").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "staging path is unavailable"):
                stage_attachments(
                    bot=bot,
                    attachments=attachment,
                    destination=repository / ".codex" / "ask-images",
                )
            self.assertEqual(list(outside.iterdir()), [])


class AskServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.to_thread = patch(
            "telegram_project_manager.bots.ask_manager.service.asyncio.to_thread",
            new=run_inline,
        )
        self.to_thread.start()
        self.addCleanup(self.to_thread.stop)
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
        self.assertEqual(call["image_paths"], ())
        self.assertEqual(call["sandbox"], Sandbox.read_only)
        self.assertEqual(call["effort"], ReasoningEffort.high)
        self.assertEqual(call["model_role"], "plan")
        self.assertIsNone(call["thread_id"])
        for _ in range(200):
            if self.workspaces.cleaned:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(self.workspaces.cleaned), 1)

    async def test_images_are_attached_to_codex_filtered_from_sources_and_cleaned(self):
        png = bytes.fromhex("89504e470d0a1a0a") + b"png"
        gif = bytes.fromhex("474946383961") + b"gif"
        self.bot.files = {"png": png, "gif": gif}
        self.codex.result = {
            "answer": "The screenshot shows the startup failure.",
            "sources": ["src/app.py", ".codex/ask-images/1.png"],
        }
        ask_id = await self.service.submit(
            chat_id=10,
            user_id=20,
            thread_id=30,
            message_id=40,
            repo="owner/repo",
            branch="main",
            source_path="/cache/repo.git",
            question="What explains this failure?",
            attachments=(
                IncomingAttachment("png", "1", "image/png", len(png)),
                IncomingAttachment("gif", "2", "image/gif", len(gif)),
            ),
        )
        await wait_for_messages(self.bot, 2)
        await wait_for_completion(self.service, ask_id)

        self.assertIn("<b>Images:</b> 2", self.bot.sent[0][1])
        self.assertEqual(self.bot.downloaded, [("png", MAX_IMAGE_BYTES), ("gif", MAX_IMAGE_BYTES)])
        self.assertEqual(self.codex.image_inputs[0], [("1.png", png), ("2.gif", gif)])
        self.assertIn("src/app.py", self.bot.sent[1][1])
        self.assertNotIn(".codex/ask-images", self.bot.sent[1][1])
        self.assertFalse((self.service._root / ask_id).exists())

    async def test_image_download_failure_replies_and_cleans_workspace(self):
        self.bot.download_errors["failed"] = TelegramBotApiError("temporary provider error")
        ask_id = await self.service.submit(
            chat_id=10,
            user_id=20,
            thread_id=30,
            message_id=40,
            repo="owner/repo",
            branch="main",
            source_path="/cache/repo.git",
            question="What is in this image?",
            attachments=(IncomingAttachment("failed", "1", "image/png", 100),),
        )
        await wait_for_messages(self.bot, 2)
        await wait_for_completion(self.service, ask_id)

        self.assertIn("Repository question failed", self.bot.sent[1][1])
        self.assertIn("Telegram image download failed", self.bot.sent[1][1])
        self.assertNotIn("temporary provider error", self.bot.sent[1][1])
        self.assertEqual(self.codex.calls, [])
        self.assertEqual(len(self.workspaces.cleaned), 1)
        self.assertFalse((self.service._root / ask_id).exists())
        self.assertEqual(
            self.service.queue_snapshot(chat_id=10, thread_id=30),
            {"running": (), "queued": ()},
        )

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

    async def test_queue_snapshot_tracks_waiting_and_running_questions_by_topic(self):
        release = asyncio.Event()

        class BlockingCodex(FakeCodex):
            async def run_turn(self, **kwargs):
                self.calls.append(kwargs)
                await release.wait()
                return "thread-ask", self.result

        self.codex = BlockingCodex()
        self.service.codex = self.codex
        first_id = await self.service.submit(
            chat_id=10,
            user_id=20,
            thread_id=30,
            message_id=40,
            repo="owner/repo",
            branch="main",
            source_path="/cache/repo.git",
            question="First question",
        )
        for _ in range(200):
            snapshot = self.service.queue_snapshot(chat_id=10, thread_id=30)
            if snapshot["running"]:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("first repository question did not start")

        long_question = "  Explain   this <unsafe> behavior " + ("x" * 180)
        self.bot.files["image"] = bytes.fromhex("89504e470d0a1a0a") + b"png"
        second_id = await self.service.submit(
            chat_id=10,
            user_id=20,
            thread_id=30,
            message_id=41,
            repo="owner/repo",
            branch="develop",
            source_path="/cache/repo.git",
            question=long_question,
            attachments=(IncomingAttachment("image", "1", "image/png", 10),),
        )

        snapshot = self.service.queue_snapshot(chat_id=10, thread_id=30)
        self.assertEqual([item["id"] for item in snapshot["running"]], [first_id])
        self.assertEqual([item["id"] for item in snapshot["queued"]], [second_id])
        self.assertEqual(snapshot["queued"][0]["image_count"], 1)
        self.assertNotIn("  ", snapshot["queued"][0]["question"])
        self.assertTrue(snapshot["queued"][0]["question"].endswith("…"))
        self.assertLessEqual(len(snapshot["queued"][0]["question"]), 120)
        self.assertEqual(
            self.service.queue_snapshot(chat_id=10, thread_id=31),
            {"running": (), "queued": ()},
        )

        release.set()
        await wait_for_completion(self.service, first_id)
        await wait_for_completion(self.service, second_id)
        self.assertEqual(
            self.service.queue_snapshot(chat_id=10, thread_id=30),
            {"running": (), "queued": ()},
        )

    async def test_shutdown_clears_running_queue_metadata(self):
        release = asyncio.Event()

        class BlockingCodex(FakeCodex):
            async def run_turn(self, **kwargs):
                await release.wait()
                return "thread-ask", self.result

        self.service.codex = BlockingCodex()
        await self.service.submit(
            chat_id=10,
            user_id=20,
            thread_id=30,
            message_id=40,
            repo="owner/repo",
            branch="main",
            source_path="/cache/repo.git",
            question="Question interrupted by shutdown",
        )
        for _ in range(200):
            if self.service.queue_snapshot(chat_id=10, thread_id=30)["running"]:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("repository question did not start")

        await self.service.shutdown()

        self.assertEqual(
            self.service.queue_snapshot(chat_id=10, thread_id=30),
            {"running": (), "queued": ()},
        )


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
