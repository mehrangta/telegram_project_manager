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
    MAX_OPPORTUNITY_LENGTH,
    MAX_PROPOSAL_LENGTH,
    MAX_SOURCE_LENGTH,
    MAX_SOURCES,
    MAX_TITLE_LENGTH,
    MAX_VALUE_LENGTH,
    BrainstormIdea,
    BrainstormResponse,
    BrainstormValidationError,
)
from telegram_project_manager.platform.responses import TELEGRAM_TEXT_LIMIT, outgoing_message
from telegram_project_manager.platform.router import IncomingMessage
from telegram_project_manager.platform.storage.db import Database
from telegram_project_manager.platform.telegram_bot import TelegramBotApiError

if SDK_AVAILABLE:
    from telegram_project_manager.bots.codex_queue.commands import _render_queue
    from telegram_project_manager.bots.ideas.commands import BrainstormManager
    from telegram_project_manager.bots.ideas.service import BrainstormService, _render_result
else:
    _render_queue = None
    BrainstormManager = None
    BrainstormService = None
    _render_result = None


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

async def wait_for_notifications(service):
    for _ in range(200):
        if not service._notification_tasks:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("brainstorm notification did not finish")


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
                    "opportunity": "Opportunity.",
                    "proposal": "Proposal.",
                    "value": "Value.",
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

    def test_schema_matches_single_message_content_limits(self):
        idea_schema = BRAINSTORM_RESPONSE_SCHEMA["properties"]["ideas"]["items"]
        properties = idea_schema["properties"]

        self.assertEqual(properties["title"]["maxLength"], MAX_TITLE_LENGTH)
        self.assertEqual(
            properties["opportunity"]["maxLength"], MAX_OPPORTUNITY_LENGTH
        )
        self.assertEqual(properties["proposal"]["maxLength"], MAX_PROPOSAL_LENGTH)
        self.assertEqual(properties["value"]["maxLength"], MAX_VALUE_LENGTH)
        self.assertEqual(properties["sources"]["maxItems"], MAX_SOURCES)
        self.assertEqual(
            properties["sources"]["items"]["maxLength"], MAX_SOURCE_LENGTH
        )

    def test_preserves_complete_normalized_fields_at_limits(self):
        raw = {
            "ideas": [
                {
                    "title": f"Idea {index}".ljust(MAX_TITLE_LENGTH, "T"),
                    "opportunity": "O" * (MAX_OPPORTUNITY_LENGTH - 1) + ".",
                    "proposal": "P" * (MAX_PROPOSAL_LENGTH - 1) + ".",
                    "value": "V" * (MAX_VALUE_LENGTH - 1) + ".",
                    "sources": ["s" * MAX_SOURCE_LENGTH],
                }
                for index in range(3)
            ]
        }
        raw["ideas"][0]["opportunity"] = "  Complete\n\topportunity.  "

        response = BrainstormResponse.from_json(raw)

        self.assertEqual(response.ideas[0].opportunity, "Complete opportunity.")
        self.assertEqual(len(response.ideas[1].proposal), MAX_PROPOSAL_LENGTH)
        self.assertEqual(len(response.ideas[2].value), MAX_VALUE_LENGTH)
        self.assertEqual(response.ideas[0].sources, ("s" * MAX_SOURCE_LENGTH,))

    def test_accepts_terminal_punctuation_with_closing_delimiters(self):
        raw = {
            "ideas": [
                {
                    "title": f"Idea {index}",
                    "opportunity": "A complete opportunity!”",
                    "proposal": "A complete proposal?)",
                    "value": "A complete value。",
                    "sources": ["src/path-without-punctuation"],
                }
                for index in range(3)
            ]
        }

        response = BrainstormResponse.from_json(raw)

        self.assertEqual(response.ideas[0].title, "Idea 0")
        self.assertEqual(
            response.ideas[0].sources, ("src/path-without-punctuation",)
        )

    def test_rejects_oversized_or_incomplete_fields(self):
        limits = {
            "title": MAX_TITLE_LENGTH,
            "opportunity": MAX_OPPORTUNITY_LENGTH,
            "proposal": MAX_PROPOSAL_LENGTH,
            "value": MAX_VALUE_LENGTH,
        }
        for field, limit in limits.items():
            raw = {
                "ideas": [
                    {
                        "title": f"Idea {index}",
                        "opportunity": "Complete opportunity.",
                        "proposal": "Complete proposal.",
                        "value": "Complete value.",
                        "sources": [],
                    }
                    for index in range(3)
                ]
            }
            raw["ideas"][0][field] = "x" * (limit + 1)
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "exceeds"
            ):
                BrainstormResponse.from_json(raw)

        for field in ("opportunity", "proposal", "value"):
            for incomplete in ("Incomplete fragment", "Incomplete...", "Incomplete…”"):
                raw = {
                    "ideas": [
                        {
                            "title": f"Idea {index}",
                            "opportunity": "Complete opportunity.",
                            "proposal": "Complete proposal.",
                            "value": "Complete value.",
                            "sources": [],
                        }
                        for index in range(3)
                    ]
                }
                raw["ideas"][0][field] = incomplete
                with self.subTest(
                    field=field, incomplete=incomplete
                ), self.assertRaisesRegex(
                    BrainstormValidationError, f"idea 1 has an incomplete {field}"
                ):
                    BrainstormResponse.from_json(raw)

    def test_rejects_sources_outside_schema_limits(self):
        raw = {
            "ideas": [
                {
                    "title": f"Idea {index}",
                    "opportunity": "Complete opportunity.",
                    "proposal": "Complete proposal.",
                    "value": "Complete value.",
                    "sources": [],
                }
                for index in range(3)
            ]
        }
        raw["ideas"][0]["sources"] = ["a"] * (MAX_SOURCES + 1)
        with self.assertRaisesRegex(ValueError, "more than"):
            BrainstormResponse.from_json(raw)

        raw["ideas"][0]["sources"] = ["s" * (MAX_SOURCE_LENGTH + 1)]
        with self.assertRaisesRegex(ValueError, "source exceeds"):
            BrainstormResponse.from_json(raw)


@unittest.skipUnless(SDK_AVAILABLE, "openai_codex dependency is unavailable")
class BrainstormRenderingTests(unittest.TestCase):
    def test_near_limit_result_remains_complete(self):
        ideas = tuple(
            BrainstormIdea(
                title=f"Idea {index} ".ljust(MAX_TITLE_LENGTH, "T"),
                opportunity="O" * (MAX_OPPORTUNITY_LENGTH - 1) + ".",
                proposal="P" * (MAX_PROPOSAL_LENGTH - 1) + ".",
                value="V" * (MAX_VALUE_LENGTH - 1) + ".",
                sources=(
                    "a" * MAX_SOURCE_LENGTH,
                    "b" * MAX_SOURCE_LENGTH,
                    "c" * MAX_SOURCE_LENGTH,
                ),
            )
            for index in range(1, 4)
        )

        result = _render_result("owner/repo", "main", "a" * 40, ideas, "manual")
        outgoing = outgoing_message(result)

        self.assertLessEqual(len(result), TELEGRAM_TEXT_LIMIT)
        self.assertIn("3. Idea 3", result)
        self.assertIn("V" * (MAX_VALUE_LENGTH - 1) + ".", result)
        self.assertIn("c" * MAX_SOURCE_LENGTH, result)
        self.assertNotIn("…", result)
        self.assertNotIn("... truncated ...", outgoing.text)

    def test_rejects_result_over_telegram_limit(self):
        idea = BrainstormIdea(
            title="Complete idea",
            opportunity="Complete opportunity.",
            proposal="Complete proposal.",
            value="Complete value.",
            sources=(),
        )
        with self.assertRaisesRegex(ValueError, "exceeds Telegram"):
            _render_result(
                f"owner/{'r' * TELEGRAM_TEXT_LIMIT}",
                "main",
                "a" * 40,
                (idea, idea, idea),
                "manual",
            )


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

    async def test_uses_active_repository_for_chat_and_topic(self):
        self.db.set_scope_repo(21, None, "owner/repo", 10, "release")
        self.db.set_scope_local_repo(21, None, "/cache/chat.git", 10)
        self.db.enable_brainstorm(
            "owner/repo",
            chat_id=99,
            thread_id=None,
            default_branch="scheduled-chat",
            local_repo_path="/cache/scheduled-chat.git",
            next_run_at=None,
            user_id=10,
        )
        self.db.allow_repo("owner/topic", 10)
        self.db.set_scope_repo(21, 31, "owner/topic", 10, "develop")
        self.db.set_scope_local_repo(21, 31, "/cache/topic.git", 10)
        self.db.enable_brainstorm(
            "owner/topic",
            chat_id=99,
            thread_id=None,
            default_branch="scheduled-topic",
            local_repo_path="/cache/scheduled-topic.git",
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

    async def test_active_repository_overrides_destination_repository(self):
        self.db.allow_repo("owner/other", 10)
        self.db.set_scope_repo(21, 31, "owner/other", 10, "current")
        self.db.set_scope_local_repo(21, 31, "/cache/current.git", 10)
        self.db.enable_brainstorm(
            "owner/repo",
            chat_id=21,
            thread_id=31,
            default_branch="destination",
            local_repo_path="/cache/destination.git",
            next_run_at=None,
            user_id=10,
        )
        self.db.enable_brainstorm(
            "owner/other",
            chat_id=99,
            thread_id=None,
            default_branch="scheduled",
            local_repo_path="/cache/scheduled.git",
            next_run_at=None,
            user_id=10,
        )

        response = await self.manager.handle(
            IncomingMessage(
                21, 10, "admin", "/brainstorm", message_id=44, thread_id=31
            )
        )

        self.assertIsNone(response)
        self.assertEqual(self.service.calls[0]["repo"], "owner/other")
        self.assertEqual(self.service.calls[0]["branch"], "current")
        self.assertEqual(self.service.calls[0]["source_path"], "/cache/current.git")

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

    async def test_destination_repository_does_not_replace_missing_active_repo(self):
        self.db.enable_brainstorm(
            "owner/repo",
            chat_id=21,
            thread_id=31,
            default_branch="main",
            local_repo_path="/cache/repo.git",
            next_run_at=None,
            user_id=10,
        )

        response = await self.manager.handle(
            IncomingMessage(
                21, 10, "admin", "/brainstorm", message_id=46, thread_id=31
            )
        )

        self.assertIn("No active repo for this topic", response.text)
        self.assertEqual(response.reply_to_message_id, 46)
        self.assertEqual(self.service.calls, [])

    async def test_disabled_active_repository_does_not_fall_through(self):
        self.db.allow_repo("owner/other", 10)
        self.db.set_scope_repo(21, 31, "owner/other", 10, "develop")
        self.db.set_scope_local_repo(21, 31, "/cache/other.git", 10)
        self.db.enable_brainstorm(
            "owner/repo",
            chat_id=21,
            thread_id=31,
            default_branch="main",
            local_repo_path="/cache/repo.git",
            next_run_at=None,
            user_id=10,
        )
        self.db.enable_brainstorm(
            "owner/other",
            chat_id=99,
            thread_id=None,
            default_branch="main",
            local_repo_path="/cache/other.git",
            next_run_at=None,
            user_id=10,
        )
        self.db.disable_brainstorm("owner/other", 10)

        response = await self.manager.handle(
            IncomingMessage(
                21, 10, "admin", "/brainstorm", message_id=47, thread_id=31
            )
        )

        self.assertIn("disabled for owner/other", response.text)
        self.assertEqual(response.reply_to_message_id, 47)
        self.assertEqual(self.service.calls, [])

    async def test_uses_current_scope_metadata_instead_of_scheduled_snapshot(self):
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
        self.edited = []
        self.deleted = []
        self.send_errors = {}
        self.edit_error = None
        self.delete_error = None

    def send_message(self, chat_id, text, thread_id=None, **options):
        self.sent.append((chat_id, text, thread_id, options))
        error = self.send_errors.get(len(self.sent))
        if error:
            raise error
        return {"message_id": len(self.sent)}

    def edit_message_text(self, chat_id, message_id, text, **options):
        self.edited.append((chat_id, message_id, text, options))
        if self.edit_error:
            raise self.edit_error

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        if self.delete_error:
            raise self.delete_error


class FakeCodex:
    def __init__(self):
        self.calls = []
        self.interrupted = []
        self.error = None
        self.block = False
        self.started = asyncio.Event()
        self.responses = []

    @staticmethod
    def complete_response():
        return {
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

    async def run_turn(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        if self.block:
            self.started.set()
            await asyncio.Future()
        response = self.responses.pop(0) if self.responses else self.complete_response()
        return kwargs.get("thread_id") or "thread", response

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
            completion_notice_seconds=0,
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
            reply_to_message_id=40,
            repo="owner/repo",
            branch="main",
            source_path="/cache/repo.git",
        )
        await wait_for_completion(self.service, brainstorm_id)
        await wait_for_notifications(self.service)
        self.assertEqual(len(self.bot.sent), 2)
        self.assertIn("queued", self.bot.sent[0][1])
        self.assertEqual(self.bot.sent[0][3]["reply_to_message_id"], 40)
        self.assertEqual(
            self.bot.sent[1][0:3],
            (20, "✅ <b>Repository brainstorm complete.</b>", 30),
        )
        self.assertIsNone(self.bot.sent[1][3]["reply_to_message_id"])
        self.assertEqual(self.bot.deleted, [(20, 2)])
        self.assertEqual(len(self.bot.edited), 1)
        self.assertEqual(self.bot.edited[0][0:2], (20, 1))
        result = self.bot.edited[0][2]
        self.assertIn("Repository brainstorm", result)
        self.assertIn("1. Idea 1", result)
        self.assertIn("3. Idea 3", result)
        self.assertIn("<b>Opportunity:</b> A repository-specific opportunity.", result)
        self.assertIn("<b>Proposal:</b> Add a focused new capability.", result)
        self.assertIn("<b>Value:</b> Expand what users can accomplish.", result)
        self.assertNotIn("…", result)
        self.assertNotIn("... truncated ...", result)
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
        self.assertIn("without a ranking number", prompt)
        self.assertIn("complete standalone sentences", prompt)
        self.assertIn("Never end a field with an ellipsis", prompt)
        self.assertIn(
            "new repository capabilities", call["developer_instructions"]
        )
        self.assertEqual(call["output_schema"], BRAINSTORM_RESPONSE_SCHEMA)
        self.assertEqual(len(self.codex.calls), 1)
        self.assertEqual(self.db.get_brainstorm_config("owner/repo")["last_status"], "ok")

    async def test_incomplete_result_is_repaired_on_same_thread(self):
        incomplete = self.codex.complete_response()
        incomplete["ideas"][0]["proposal"] = "Add a focused new capability"
        self.codex.responses = [incomplete, self.codex.complete_response()]

        brainstorm_id = await self.service.submit(
            chat_id=20,
            user_id=10,
            thread_id=30,
            reply_to_message_id=40,
            repo="owner/repo",
            branch="main",
            source_path="/cache/repo.git",
        )
        await wait_for_completion(self.service, brainstorm_id)

        self.assertEqual(len(self.codex.calls), 2)
        self.assertIsNone(self.codex.calls[0]["thread_id"])
        self.assertEqual(self.codex.calls[1]["thread_id"], "thread")
        self.assertIn("failed validation", self.codex.calls[1]["prompt"])
        self.assertIn(
            "idea 1 has an incomplete proposal", self.codex.calls[1]["prompt"]
        )
        self.assertIn("Regenerate the entire response", self.codex.calls[1]["prompt"])
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(len(self.bot.edited), 1)
        self.assertIn("3. Idea 3", self.bot.edited[0][2])
        self.assertEqual(self.db.get_brainstorm_config("owner/repo")["last_status"], "ok")

    async def test_two_incomplete_results_fail_after_single_repair(self):
        first = self.codex.complete_response()
        first["ideas"][0]["value"] = "Incomplete value"
        second = self.codex.complete_response()
        second["ideas"][1]["opportunity"] = "Still incomplete"
        self.codex.responses = [first, second]

        brainstorm_id = await self.service.submit(
            chat_id=20,
            user_id=10,
            thread_id=30,
            reply_to_message_id=40,
            repo="owner/repo",
            branch="main",
            source_path="/cache/repo.git",
        )
        await wait_for_completion(self.service, brainstorm_id)

        self.assertEqual(len(self.codex.calls), 2)
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(len(self.bot.edited), 1)
        self.assertIn("Repository brainstorm failed", self.bot.edited[0][2])
        self.assertIn("after 2 attempts", self.bot.edited[0][2])
        self.assertIn(
            "idea 2 has an incomplete opportunity", self.bot.edited[0][2]
        )
        self.assertNotIn("Still incomplete", self.bot.edited[0][2])
        self.assertEqual(
            self.db.get_brainstorm_config("owner/repo")["last_status"], "failed"
        )

    async def test_scheduled_run_advances_and_uses_configured_destination(self):
        incomplete = self.codex.complete_response()
        incomplete["ideas"][0]["opportunity"] = "Incomplete opportunity"
        self.codex.responses = [incomplete, self.codex.complete_response()]
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
        self.assertEqual(self.bot.edited, [])
        self.assertEqual(len(self.codex.calls), 2)
        self.assertEqual(self.db.get_brainstorm_config("owner/repo")["next_run_at"], 172850)

    async def test_completion_notification_send_failure_keeps_successful_result(self):
        self.bot.send_errors[2] = TelegramBotApiError("notification unavailable")
        brainstorm_id = await self.service.submit(
            chat_id=20,
            user_id=10,
            thread_id=30,
            reply_to_message_id=40,
            repo="owner/repo",
            branch="main",
            source_path="/cache/repo.git",
        )

        await wait_for_completion(self.service, brainstorm_id)

        self.assertEqual(len(self.bot.sent), 2)
        self.assertEqual(len(self.bot.edited), 1)
        self.assertEqual(self.bot.deleted, [])
        self.assertEqual(self.db.get_brainstorm_config("owner/repo")["last_status"], "ok")

    async def test_completion_notification_delete_failure_keeps_successful_result(self):
        self.bot.delete_error = TelegramBotApiError("message cannot be deleted")
        brainstorm_id = await self.service.submit(
            chat_id=20,
            user_id=10,
            thread_id=30,
            reply_to_message_id=40,
            repo="owner/repo",
            branch="main",
            source_path="/cache/repo.git",
        )

        await wait_for_completion(self.service, brainstorm_id)
        await wait_for_notifications(self.service)

        self.assertEqual(self.bot.deleted, [(20, 2)])
        self.assertEqual(self.db.get_brainstorm_config("owner/repo")["last_status"], "ok")

    async def test_shutdown_deletes_pending_completion_notification(self):
        self.service.completion_notice_seconds = 60
        brainstorm_id = await self.service.submit(
            chat_id=20,
            user_id=10,
            thread_id=30,
            reply_to_message_id=40,
            repo="owner/repo",
            branch="main",
            source_path="/cache/repo.git",
        )
        await wait_for_completion(self.service, brainstorm_id)
        self.assertTrue(self.service._notification_tasks)

        await self.service.shutdown()

        self.assertEqual(self.bot.deleted, [(20, 2)])
        self.assertEqual(self.service._notification_tasks, set())

    async def test_manual_failure_updates_queued_message(self):
        self.codex.error = ValueError("invalid brainstorm response")
        brainstorm_id = await self.service.submit(
            chat_id=20,
            user_id=10,
            thread_id=30,
            reply_to_message_id=40,
            repo="owner/repo",
            branch="main",
            source_path="/cache/repo.git",
        )

        await wait_for_completion(self.service, brainstorm_id)

        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(len(self.bot.edited), 1)
        self.assertEqual(self.bot.edited[0][0:2], (20, 1))
        self.assertIn("Repository brainstorm failed", self.bot.edited[0][2])
        self.assertIn("invalid brainstorm response", self.bot.edited[0][2])
        self.assertEqual(self.db.get_brainstorm_config("owner/repo")["last_status"], "failed")

    async def test_over_budget_result_updates_manual_message_as_failed(self):
        repo = f"owner/{'r' * TELEGRAM_TEXT_LIMIT}"
        self.db.allow_repo(repo, 10)
        self.db.enable_brainstorm(
            repo,
            chat_id=20,
            thread_id=30,
            default_branch="main",
            local_repo_path="/cache/repo.git",
            next_run_at=None,
            user_id=10,
        )
        brainstorm_id = await self.service.submit(
            chat_id=20,
            user_id=10,
            thread_id=30,
            reply_to_message_id=40,
            repo=repo,
            branch="main",
            source_path="/cache/repo.git",
        )

        await wait_for_completion(self.service, brainstorm_id)

        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(len(self.bot.edited), 1)
        self.assertIn("Repository brainstorm failed", self.bot.edited[0][2])
        self.assertIn("exceeds Telegram", self.bot.edited[0][2])
        self.assertNotIn("... truncated ...", self.bot.edited[0][2])
        self.assertEqual(len(self.codex.calls), 1)
        self.assertEqual(self.db.get_brainstorm_config(repo)["last_status"], "failed")

    async def test_manual_edit_failure_does_not_send_fallback_message(self):
        self.bot.edit_error = TelegramBotApiError("message can't be edited")
        brainstorm_id = await self.service.submit(
            chat_id=20,
            user_id=10,
            thread_id=30,
            reply_to_message_id=40,
            repo="owner/repo",
            branch="main",
            source_path="/cache/repo.git",
        )

        await wait_for_completion(self.service, brainstorm_id)

        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(len(self.bot.edited), 1)
        self.assertEqual(self.db.get_brainstorm_config("owner/repo")["last_status"], "failed")

    async def test_shutdown_updates_manual_message_as_cancelled(self):
        self.codex.block = True
        brainstorm_id = await self.service.submit(
            chat_id=20,
            user_id=10,
            thread_id=30,
            reply_to_message_id=40,
            repo="owner/repo",
            branch="main",
            source_path="/cache/repo.git",
        )
        await asyncio.wait_for(self.codex.started.wait(), timeout=1)

        await self.service.shutdown()

        self.assertNotIn(brainstorm_id, self.service._tasks)
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(len(self.bot.edited), 1)
        self.assertIn("Repository brainstorm cancelled", self.bot.edited[0][2])
        self.assertEqual(
            self.db.get_brainstorm_config("owner/repo")["last_status"], "cancelled"
        )

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
