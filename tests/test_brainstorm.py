import asyncio
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from unittest.mock import patch

SDK_AVAILABLE = True
try:
    from openai_codex import Sandbox
    from openai_codex.types import ReasoningEffort
except ModuleNotFoundError:
    if os.environ.get("TPM_TEST_CODEX_STUBS") != "1":
        SDK_AVAILABLE = False
        Sandbox = None
        ReasoningEffort = None
    else:
        class Sandbox(Enum):
            full_access = "full_access"

        class ReasoningEffort(Enum):
            high = "high"

        class ApprovalMode(Enum):
            never = "never"

        class AsyncCodex:
            pass

        class TextInput:
            def __init__(self, text):
                self.text = text

        class LocalImageInput:
            def __init__(self, path):
                self.path = path

        class CodexConfig:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        codex_module = types.ModuleType("openai_codex")
        codex_module.ApprovalMode = ApprovalMode
        codex_module.AsyncCodex = AsyncCodex
        codex_module.LocalImageInput = LocalImageInput
        codex_module.Sandbox = Sandbox
        codex_module.TextInput = TextInput
        client_module = types.ModuleType("openai_codex.client")
        client_module.CodexConfig = CodexConfig
        types_module = types.ModuleType("openai_codex.types")
        types_module.ReasoningEffort = ReasoningEffort
        sys.modules["openai_codex"] = codex_module
        sys.modules["openai_codex.client"] = client_module
        sys.modules["openai_codex.types"] = types_module

from telegram_project_manager.bots.ideas.schedule import (
    advance_run_at,
    format_interval,
    format_utc_time,
    next_run_at,
    parse_interval,
    parse_utc_time,
)
from telegram_project_manager.bots.ideas.schemas import (
    BRAINSTORM_RESPONSE_SCHEMA,
    BrainstormResponse,
)
from telegram_project_manager.platform.router import IncomingMessage
from telegram_project_manager.platform.storage.db import Database

if SDK_AVAILABLE:
    from telegram_project_manager.bots.codex_queue.commands import _render_queue
    from telegram_project_manager.bots.ideas.commands import BrainstormManager
    from telegram_project_manager.bots.ideas.service import BrainstormService
else:
    _render_queue = None
    BrainstormManager = None
    BrainstormService = None


def timestamp(year, month, day, hour, minute):
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp())


async def run_inline(function, /, *args, **kwargs):
    return function(*args, **kwargs)


async def wait_for_completion(service, brainstorm_id):
    for _ in range(200):
        if brainstorm_id not in service._tasks:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"brainstorm did not finish: {brainstorm_id}")


class ScheduleTests(unittest.TestCase):
    def test_parses_and_formats_supported_intervals(self):
        self.assertEqual(parse_interval("daily"), 1)
        self.assertEqual(parse_interval("1d"), 1)
        self.assertEqual(parse_interval("2D"), 2)
        self.assertEqual(parse_interval("weekly"), 7)
        self.assertEqual(parse_interval("365d"), 365)
        self.assertEqual(format_interval(1), "daily")
        self.assertEqual(format_interval(7), "weekly")
        self.assertEqual(format_interval(2), "2d")

    def test_rejects_invalid_intervals(self):
        for value in ("", "0d", "366d", "every day", "1", "01d"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_interval(value)

    def test_parses_utc_time(self):
        self.assertEqual(parse_utc_time("00:00"), 0)
        self.assertEqual(parse_utc_time("23:59"), 1439)
        self.assertEqual(format_utc_time(545), "09:05 UTC")
        for value in ("9:00", "24:00", "09:60", "UTC", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_utc_time(value)

    def test_calculates_and_advances_utc_runs(self):
        morning = timestamp(2026, 7, 26, 8, 0)
        evening = timestamp(2026, 7, 26, 10, 0)
        self.assertEqual(
            next_run_at(morning, 2, parse_utc_time("09:00")),
            timestamp(2026, 7, 26, 9, 0),
        )
        first = next_run_at(evening, 2, parse_utc_time("09:00"))
        self.assertEqual(first, timestamp(2026, 7, 27, 9, 0))
        self.assertEqual(
            advance_run_at(first, timestamp(2026, 8, 2, 12, 0), 2),
            timestamp(2026, 8, 4, 9, 0),
        )


class BrainstormSchemaTests(unittest.TestCase):
    def test_requires_exactly_three_unique_ideas(self):
        raw = {
            "ideas": [
                {
                    "title": f"Idea {index}",
                    "opportunity": "Opportunity",
                    "proposal": "Proposal",
                    "value": "Value",
                    "sources": ["src/app.py", "src/app.py"],
                }
                for index in range(3)
            ]
        }
        response = BrainstormResponse.from_json(raw)
        self.assertEqual(len(response.ideas), 3)
        self.assertEqual(response.ideas[0].sources, ("src/app.py",))
        with self.assertRaisesRegex(ValueError, "exactly 3"):
            BrainstormResponse.from_json({"ideas": raw["ideas"][:2]})
        raw["ideas"][1]["title"] = "IDEA 0"
        with self.assertRaisesRegex(ValueError, "duplicate"):
            BrainstormResponse.from_json(raw)

    def test_schema_uses_idea_oriented_fields(self):
        self.assertEqual(BRAINSTORM_RESPONSE_SCHEMA["required"], ["ideas"])
        idea_schema = BRAINSTORM_RESPONSE_SCHEMA["properties"]["ideas"]["items"]
        self.assertEqual(
            idea_schema["required"],
            ["title", "opportunity", "proposal", "value", "sources"],
        )
        self.assertNotIn("problem", idea_schema["properties"])


class BrainstormDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "bot.db")
        self.db.initialize()
        self.db.allow_repo("owner/repo", 10)

    def test_schedule_disable_and_cascade(self):
        self.db.configure_brainstorm_schedule("owner/repo", 2, 540, 1000, 10)
        config = self.db.get_brainstorm_config("owner/repo")
        self.assertFalse(config["enabled"])
        self.assertEqual(config["interval_days"], 2)
        self.assertIsNone(config["next_run_at"])
        self.db.enable_brainstorm(
            "owner/repo",
            chat_id=20,
            thread_id=30,
            default_branch="main",
            local_repo_path="/cache/repo.git",
            next_run_at=1000,
            user_id=10,
        )
        self.db.disable_brainstorm("owner/repo", 10)
        config = self.db.get_brainstorm_config("owner/repo")
        self.assertFalse(config["enabled"])
        self.assertEqual(config["interval_days"], 2)
        self.assertEqual(config["telegram_thread_id"], 30)
        self.assertIsNone(config["next_run_at"])
        self.db.disallow_repo("owner/repo")
        self.assertIsNone(self.db.get_brainstorm_config("owner/repo"))

    def test_lists_brainstorm_configs_for_exact_scope(self):
        for repo in ("owner/topic", "owner/second", "owner/other"):
            self.db.allow_repo(repo, 10)
        self.db.enable_brainstorm(
            "owner/repo",
            chat_id=20,
            thread_id=None,
            default_branch="main",
            local_repo_path="/cache/chat.git",
            next_run_at=None,
            user_id=10,
        )
        self.db.enable_brainstorm(
            "owner/topic",
            chat_id=20,
            thread_id=30,
            default_branch="develop",
            local_repo_path="/cache/topic.git",
            next_run_at=None,
            user_id=10,
        )
        self.db.enable_brainstorm(
            "owner/second",
            chat_id=20,
            thread_id=30,
            default_branch="stable",
            local_repo_path="/cache/second.git",
            next_run_at=None,
            user_id=10,
        )
        self.db.disable_brainstorm("owner/second", 10)
        self.db.enable_brainstorm(
            "owner/other",
            chat_id=21,
            thread_id=30,
            default_branch="main",
            local_repo_path="/cache/other.git",
            next_run_at=None,
            user_id=10,
        )

        self.assertEqual(
            [
                config["repo"]
                for config in self.db.list_brainstorm_configs_for_scope(20, None)
            ],
            ["owner/repo"],
        )
        self.assertEqual(
            [
                config["repo"]
                for config in self.db.list_brainstorm_configs_for_scope(20, 30)
            ],
            ["owner/second", "owner/topic"],
        )
        self.assertEqual(self.db.list_brainstorm_configs_for_scope(20, 31), [])
        self.assertEqual(
            [
                config["repo"]
                for config in self.db.list_brainstorm_configs_for_scope(21, 30)
            ],
            ["owner/other"],
        )

    def test_claims_atomically_and_finishes_matching_run_only(self):
        self.db.enable_brainstorm(
            "owner/repo",
            chat_id=20,
            thread_id=None,
            default_branch="main",
            local_repo_path="/cache/repo.git",
            next_run_at=None,
            user_id=10,
        )
        claimed = self.db.claim_brainstorm_run(
            "owner/repo", "b-first", "manual", 100, 0
        )
        self.assertEqual(claimed["active_run_id"], "b-first")
        self.assertIsNone(
            self.db.claim_brainstorm_run("owner/repo", "b-second", "manual", 101, 0)
        )
        self.assertFalse(
            self.db.finish_brainstorm_run("owner/repo", "b-wrong", "ok", "", 102)
        )
        self.assertTrue(
            self.db.finish_brainstorm_run("owner/repo", "b-first", "ok", "", 103)
        )

    def test_legacy_database_upgrade_adds_brainstorm_configuration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE allowed_repos (
                    repo TEXT PRIMARY KEY,
                    deploy_workflow TEXT,
                    added_by_user_id INTEGER,
                    created_at INTEGER NOT NULL
                );
                INSERT INTO allowed_repos VALUES ('owner/legacy', NULL, 10, 1);
                """
            )
            connection.commit()
            connection.close()
            upgraded = Database(path)
            upgraded.initialize()
            with upgraded.session() as session:
                table = session.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'brainstorm_configs'"
                ).fetchone()
                indexes = {
                    row["name"]
                    for row in session.execute("PRAGMA index_list(brainstorm_configs)")
                }
            self.assertIsNotNone(table)
            self.assertIn("idx_brainstorm_configs_destination", indexes)
            self.assertIsNone(upgraded.get_brainstorm_config("owner/legacy"))


class FakeCommandService:
    def __init__(self):
        self.calls = []

    def validate_source(self, *, source_path, repo):
        if not source_path:
            raise ValueError("missing cache")
        return source_path

    async def submit(self, **kwargs):
        self.calls.append(kwargs)
        return "b-12345678"


@unittest.skipUnless(SDK_AVAILABLE, "openai_codex dependency is unavailable")
class BrainstormManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.db = Database(Path(self.temp.name) / "bot.db")
        self.db.initialize()
        self.db.upsert_user(10, "admin", "admin")
        self.db.allow_repo("owner/repo", 10)
        self.db.set_scope_repo(20, 30, "owner/repo", 10, "develop")
        self.db.set_scope_local_repo(20, 30, "/cache/repo.git", 10)
        self.service = FakeCommandService()
        self.manager = BrainstormManager(db=self.db, service=self.service, clock=lambda: 100)

    async def _cleanup(self):
        self.temp.cleanup()

    async def test_configures_enables_and_runs_for_active_topic(self):
        scheduled = await self.manager.handle(
            IncomingMessage(
                20, 10, "admin", "/repo brainstorm schedule owner/repo 2d 09:00", thread_id=30
            )
        )
        enabled = await self.manager.handle(
            IncomingMessage(
                20, 10, "admin", "/repo brainstorm enable owner/repo", thread_id=30
            )
        )
        response = await self.manager.handle(
            IncomingMessage(
                20, 10, "admin", "/brainstorm@ProjectBot", message_id=40, thread_id=30
            )
        )
        self.assertIn("2d at 09:00 UTC", scheduled)
        self.assertIn("topic 30", enabled)
        self.assertIsNone(response)
        self.assertEqual(self.service.calls[0]["branch"], "develop")
        self.assertEqual(self.service.calls[0]["source_path"], "/cache/repo.git")

    async def test_disabled_repo_rejects_manual_run_and_preserves_schedule(self):
        await self.manager.handle(
            IncomingMessage(
                20, 10, "admin", "/repo brainstorm schedule owner/repo daily 09:00", thread_id=30
            )
        )
        disabled = await self.manager.handle(
            IncomingMessage(
                20, 10, "admin", "/repo brainstorm disable owner/repo", thread_id=30
            )
        )
        response = await self.manager.handle(
            IncomingMessage(20, 10, "admin", "/brainstorm", message_id=41, thread_id=30)
        )
        self.assertIn("preserved", disabled)
        self.assertIn("disabled", response.text)
        self.assertEqual(self.db.get_brainstorm_config("owner/repo")["interval_days"], 1)

    async def test_auto_detects_repository_for_chat_and_topic(self):
        self.db.enable_brainstorm(
            "owner/repo",
            chat_id=21,
            thread_id=None,
            default_branch="release",
            local_repo_path="/cache/chat.git",
            next_run_at=None,
            user_id=10,
        )
        self.db.allow_repo("owner/topic", 10)
        self.db.enable_brainstorm(
            "owner/topic",
            chat_id=21,
            thread_id=31,
            default_branch="develop",
            local_repo_path="/cache/topic.git",
            next_run_at=None,
            user_id=10,
        )

        chat_response = await self.manager.handle(
            IncomingMessage(21, 10, "admin", "/brainstorm", message_id=42)
        )
        topic_response = await self.manager.handle(
            IncomingMessage(
                21, 10, "admin", "/brainstorm", message_id=43, thread_id=31
            )
        )

        self.assertIsNone(chat_response)
        self.assertIsNone(topic_response)
        self.assertEqual(
            (self.service.calls[0]["repo"], self.service.calls[0]["branch"]),
            ("owner/repo", "release"),
        )
        self.assertEqual(self.service.calls[0]["source_path"], "/cache/chat.git")
        self.assertEqual(
            (self.service.calls[1]["repo"], self.service.calls[1]["branch"]),
            ("owner/topic", "develop"),
        )
        self.assertEqual(self.service.calls[1]["source_path"], "/cache/topic.git")

    async def test_detected_repository_overrides_unrelated_active_repo(self):
        self.db.allow_repo("owner/other", 10)
        self.db.set_scope_repo(21, 31, "owner/other", 10, "wrong")
        self.db.set_scope_local_repo(21, 31, "/cache/wrong.git", 10)
        self.db.enable_brainstorm(
            "owner/repo",
            chat_id=21,
            thread_id=31,
            default_branch="detected",
            local_repo_path="/cache/detected.git",
            next_run_at=None,
            user_id=10,
        )

        response = await self.manager.handle(
            IncomingMessage(
                21, 10, "admin", "/brainstorm", message_id=44, thread_id=31
            )
        )

        self.assertIsNone(response)
        self.assertEqual(self.service.calls[0]["repo"], "owner/repo")
        self.assertEqual(self.service.calls[0]["branch"], "detected")
        self.assertEqual(self.service.calls[0]["source_path"], "/cache/detected.git")

    async def test_topic_does_not_inherit_chat_brainstorm_destination(self):
        self.db.enable_brainstorm(
            "owner/repo",
            chat_id=21,
            thread_id=None,
            default_branch="main",
            local_repo_path="/cache/chat.git",
            next_run_at=None,
            user_id=10,
        )

        response = await self.manager.handle(
            IncomingMessage(
                21, 10, "admin", "/brainstorm", message_id=45, thread_id=31
            )
        )

        self.assertIn("No active repo for this topic", response.text)
        self.assertEqual(response.reply_to_message_id, 45)
        self.assertEqual(self.service.calls, [])

    async def test_active_repo_disambiguates_multiple_destination_repositories(self):
        self.db.allow_repo("owner/other", 10)
        for repo, path in (
            ("owner/repo", "/cache/repo.git"),
            ("owner/other", "/cache/other.git"),
        ):
            self.db.enable_brainstorm(
                repo,
                chat_id=21,
                thread_id=None,
                default_branch="main",
                local_repo_path=path,
                next_run_at=None,
                user_id=10,
            )

        ambiguous = await self.manager.handle(
            IncomingMessage(21, 10, "admin", "/brainstorm", message_id=46)
        )
        self.db.set_scope_repo(21, None, "owner/other", 10, "ignored")
        selected = await self.manager.handle(
            IncomingMessage(21, 10, "admin", "/brainstorm", message_id=47)
        )

        self.assertIn("Multiple brainstorm repos", ambiguous.text)
        self.assertIn("owner/other, owner/repo", ambiguous.text)
        self.assertEqual(ambiguous.reply_to_message_id, 46)
        self.assertIsNone(selected)
        self.assertEqual(len(self.service.calls), 1)
        self.assertEqual(self.service.calls[0]["repo"], "owner/other")
        self.assertEqual(self.service.calls[0]["source_path"], "/cache/other.git")

    async def test_auto_detected_disabled_repository_reports_disabled(self):
        self.db.enable_brainstorm(
            "owner/repo",
            chat_id=21,
            thread_id=31,
            default_branch="main",
            local_repo_path="/cache/repo.git",
            next_run_at=None,
            user_id=10,
        )
        self.db.disable_brainstorm("owner/repo", 10)

        response = await self.manager.handle(
            IncomingMessage(
                21, 10, "admin", "/brainstorm", message_id=47, thread_id=31
            )
        )

        self.assertIn("disabled for owner/repo", response.text)
        self.assertEqual(response.reply_to_message_id, 47)
        self.assertEqual(self.service.calls, [])

    async def test_falls_back_to_active_repo_without_destination_match(self):
        self.db.enable_brainstorm(
            "owner/repo",
            chat_id=99,
            thread_id=None,
            default_branch="scheduled",
            local_repo_path="/cache/scheduled.git",
            next_run_at=None,
            user_id=10,
        )

        response = await self.manager.handle(
            IncomingMessage(
                20, 10, "admin", "/brainstorm", message_id=48, thread_id=30
            )
        )

        self.assertIsNone(response)
        self.assertEqual(self.service.calls[0]["repo"], "owner/repo")
        self.assertEqual(self.service.calls[0]["branch"], "develop")
        self.assertEqual(self.service.calls[0]["source_path"], "/cache/repo.git")

    async def test_enable_requires_matching_active_repo(self):
        response = await self.manager.handle(
            IncomingMessage(20, 10, "admin", "/repo brainstorm enable owner/repo")
        )
        self.assertIn("active repo", response)


class FakeBot:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, thread_id=None, **options):
        self.sent.append((chat_id, text, thread_id, options))
        return {"message_id": len(self.sent)}


class FakeCodex:
    def __init__(self):
        self.calls = []
        self.interrupted = []

    async def run_turn(self, **kwargs):
        self.calls.append(kwargs)
        return "thread", {
            "ideas": [
                {
                    "title": f"Idea {index}",
                    "opportunity": "A repository-specific opportunity.",
                    "proposal": "Add a focused new capability.",
                    "value": "Expand what users can accomplish.",
                    "sources": ["src/app.py", "missing.py"],
                }
                for index in range(1, 4)
            ]
        }

    async def interrupt(self, job_id):
        self.interrupted.append(job_id)


class FakeWorkspaces:
    def __init__(self):
        self.cleaned = []

    def validate_source(self, *, source_path, repo):
        if not source_path:
            raise ValueError("missing cache")
        return source_path

    def prepare_read_only(self, *, path, **kwargs):
        (path / "src").mkdir(parents=True)
        (path / "src" / "app.py").write_text("print('ok')", encoding="utf-8")
        return "abcdef1234567890"

    def cleanup_read_only(self, *, source_path, path):
        self.cleaned.append((source_path, path))


@unittest.skipUnless(SDK_AVAILABLE, "openai_codex dependency is unavailable")
class BrainstormServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.to_thread = patch(
            "telegram_project_manager.bots.ideas.service.asyncio.to_thread", new=run_inline
        )
        self.to_thread.start()
        self.addCleanup(self.to_thread.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.db = Database(Path(self.temp.name) / "bot.db")
        self.db.initialize()
        self.db.allow_repo("owner/repo", 10)
        self.db.enable_brainstorm(
            "owner/repo",
            chat_id=20,
            thread_id=30,
            default_branch="main",
            local_repo_path="/cache/repo.git",
            next_run_at=None,
            user_id=10,
        )
        self.bot = FakeBot()
        self.codex = FakeCodex()
        self.workspaces = FakeWorkspaces()
        self.service = BrainstormService(
            db=self.db,
            codex=self.codex,
            workspaces=self.workspaces,
            bot=self.bot,
            clock=lambda: 100,
        )

    async def _cleanup(self):
        await self.service.shutdown()
        self.temp.cleanup()

    async def test_manual_run_sends_three_grounded_ideas(self):
        brainstorm_id = await self.service.submit(
            chat_id=20,
            user_id=10,
            thread_id=30,
            message_id=40,
            repo="owner/repo",
            branch="main",
            source_path="/cache/repo.git",
        )
        await wait_for_completion(self.service, brainstorm_id)
        self.assertIn("queued", self.bot.sent[0][1])
        result = self.bot.sent[1][1]
        self.assertIn("Repository brainstorm", result)
        self.assertIn("1. Idea 1", result)
        self.assertIn("3. Idea 3", result)
        self.assertIn("<b>Opportunity:</b> A repository-specific opportunity.", result)
        self.assertIn("<b>Proposal:</b> Add a focused new capability.", result)
        self.assertIn("<b>Value:</b> Expand what users can accomplish.", result)
        self.assertNotIn("Problem:", result)
        self.assertNotIn("Change:", result)
        self.assertIn("src/app.py", result)
        self.assertNotIn("missing.py", result)
        call = self.codex.calls[0]
        self.assertEqual(call["effort"], ReasoningEffort.high)
        self.assertEqual(call["model_role"], "plan")
        self.assertEqual(call["sandbox"], Sandbox.full_access)
        prompt = call["prompt"]
        self.assertIn("new capabilities", prompt)
        self.assertIn("Do not propose bug fixes", prompt)
        self.assertIn("maintenance", prompt)
        self.assertIn("only when it directly enables", prompt)
        self.assertIn(
            "new repository capabilities", call["developer_instructions"]
        )
        self.assertEqual(call["output_schema"], BRAINSTORM_RESPONSE_SCHEMA)
        self.assertEqual(self.db.get_brainstorm_config("owner/repo")["last_status"], "ok")

    async def test_scheduled_run_advances_and_uses_configured_destination(self):
        self.db.configure_brainstorm_schedule("owner/repo", 2, 0, 50, 10)
        self.db.enable_brainstorm(
            "owner/repo",
            chat_id=22,
            thread_id=33,
            default_branch="stable",
            local_repo_path="/cache/repo.git",
            next_run_at=50,
            user_id=10,
        )
        await self.service.run_due_once()
        brainstorm_id = next(iter(self.service._tasks))
        await wait_for_completion(self.service, brainstorm_id)
        self.assertEqual(self.bot.sent[0][0], 22)
        self.assertEqual(self.bot.sent[0][2], 33)
        self.assertIn("scheduled", self.bot.sent[0][1])
        self.assertEqual(self.db.get_brainstorm_config("owner/repo")["next_run_at"], 172850)

    async def test_recover_starts_once_and_shutdown_stops_scheduler(self):
        await self.service.recover()
        scheduler = self.service._scheduler_task
        await self.service.recover()
        self.assertIs(self.service._scheduler_task, scheduler)
        await self.service.shutdown()
        self.assertIsNone(self.service._scheduler_task)
        self.assertTrue(scheduler.done())

    def test_queue_renderer_includes_brainstorms(self):
        empty = {"running": (), "queued": ()}
        rendered = _render_queue(
            empty,
            empty,
            empty,
            {
                "running": (
                    {
                        "id": "b-12345678",
                        "repo": "owner/repo",
                        "branch": "main",
                        "trigger": "scheduled",
                        "status": "running",
                    },
                ),
                "queued": (),
            },
        )
        self.assertIn("Repository brainstorms", rendered)
        self.assertIn("scheduled: running", rendered)


if __name__ == "__main__":
    unittest.main()
