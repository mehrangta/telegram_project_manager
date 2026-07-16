import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_project_manager.bots.commit_manager.commands import CommitManager, split_command
from telegram_project_manager.integrations.gh.issues import IssueSummary
from telegram_project_manager.integrations.gh.runner import GhError, GhResult
from telegram_project_manager.platform.permissions import PermissionService
from telegram_project_manager.platform.responses import OutgoingMessage
from telegram_project_manager.platform.router import IncomingMessage
from telegram_project_manager.platform.storage.db import Database


class CommandTests(unittest.TestCase):
    def test_help_lists_repository_and_host_do_commands(self):
        manager = object.__new__(CommitManager)

        help_text = manager.help()

        self.assertIn("/do <job>", help_text)
        self.assertIn("active repository", help_text)
        self.assertIn("/do --host <job>", help_text)
        self.assertIn("/do status", help_text)

    def test_database_upgrade_preserves_chat_settings_and_adds_topic_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bot.db"
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE chat_settings (
                    telegram_chat_id INTEGER PRIMARY KEY,
                    active_repo TEXT,
                    default_branch TEXT NOT NULL DEFAULT 'main',
                    local_repo_path TEXT,
                    updated_by_user_id INTEGER,
                    updated_at INTEGER NOT NULL
                );
                INSERT INTO chat_settings VALUES (20, 'owner/repo', 'main', '/cache/repo', 10, 1);
                CREATE TABLE plans (
                    id TEXT PRIMARY KEY, telegram_chat_id INTEGER NOT NULL,
                    telegram_user_id INTEGER NOT NULL, repo TEXT NOT NULL,
                    base_branch TEXT NOT NULL, target_branch TEXT NOT NULL,
                    request_text TEXT NOT NULL, plan_json TEXT NOT NULL,
                    status TEXT NOT NULL, created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE issue_drafts (
                    id TEXT PRIMARY KEY, telegram_chat_id INTEGER NOT NULL,
                    telegram_user_id INTEGER NOT NULL, repo TEXT NOT NULL,
                    default_branch TEXT NOT NULL, request_text TEXT NOT NULL,
                    issue_json TEXT NOT NULL, status TEXT NOT NULL,
                    github_issue_number INTEGER, github_issue_url TEXT,
                    created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
                );
                CREATE TABLE allowed_repos (
                    repo TEXT PRIMARY KEY,
                    deploy_workflow TEXT,
                    added_by_user_id INTEGER,
                    created_at INTEGER NOT NULL
                );
                INSERT INTO allowed_repos VALUES ('owner/repo', 'deploy.yml', 10, 1);
                """
            )
            conn.commit()
            conn.close()

            db = Database(path)
            db.initialize()

            self.assertEqual(db.get_chat_settings(20)["active_repo"], "owner/repo")
            with db.session() as upgraded:
                plan_columns = {
                    row["name"] for row in upgraded.execute("PRAGMA table_info(plans)")
                }
                draft_columns = {
                    row["name"] for row in upgraded.execute("PRAGMA table_info(issue_drafts)")
                }
                topic_columns = {
                    row["name"] for row in upgraded.execute("PRAGMA table_info(topic_settings)")
                }
                allowed_repo_columns = {
                    row["name"] for row in upgraded.execute("PRAGMA table_info(allowed_repos)")
                }
                code_job_columns = {
                    row["name"] for row in upgraded.execute("PRAGMA table_info(code_jobs)")
                }
                feedback_table = upgraded.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'code_plan_feedback'"
                ).fetchone()
            self.assertIn("telegram_thread_id", plan_columns)
            self.assertIn("telegram_thread_id", draft_columns)
            self.assertIn("local_repo_path", draft_columns)
            self.assertIn("telegram_thread_id", topic_columns)
            self.assertIn("deploy_enabled", allowed_repo_columns)
            self.assertFalse(db.is_repo_deploy_enabled("owner/repo"))
            self.assertIn("telegram_plan_message_id", code_job_columns)
            self.assertIn("github_plan_question_comment_id", code_job_columns)
            self.assertIn("github_plan_question_revision", code_job_columns)
            self.assertIn("github_plan_comment_cursor", code_job_columns)
            self.assertIn("deployment_mode", code_job_columns)
            self.assertIn("deployment_conflict_attempts", code_job_columns)
            self.assertIsNotNone(feedback_table)

    def test_database_upgrade_marks_existing_delivery_operations_as_deploy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.create_code_job(
                {
                    "id": "c-abcdef12", "telegram_chat_id": 10,
                    "telegram_user_id": 20, "telegram_thread_id": None,
                    "repo": "owner/repo", "issue_number": 1, "issue_title": "Issue",
                    "issue_url": "url", "issue_context_json": {}, "base_branch": "main",
                    "target_branch": "branch", "workspace_path": "/tmp/job",
                    "source_repo_path": "/tmp/repo", "status": "ready",
                    "resume_phase": "checks", "skip_plan": True,
                }
            )
            with db.session() as conn:
                conn.execute(
                    """
                    UPDATE code_jobs SET deployment_status = 'deploying', deployment_mode = NULL
                    WHERE id = 'c-abcdef12'
                    """
                )

            db.initialize()

            self.assertEqual(db.get_code_job("c-abcdef12")["deployment_mode"], "deploy")

    def test_split_command(self):
        self.assertEqual(split_command("/commit add readme"), ("/commit", "add readme"))

    def test_split_command_with_bot_username(self):
        self.assertEqual(split_command("/commit@MyBot add readme"), ("/commit", "add readme"))

    def test_openai_api_key_requires_private_admin_chat_and_is_redacted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.upsert_user(10, "admin", "admin")
            manager = object.__new__(CommitManager)
            manager.db = db
            manager.permissions = PermissionService(db)
            group_message = IncomingMessage(1, 10, "admin", "", is_private=False)
            private_message = IncomingMessage(10, 10, "admin", "", is_private=True)

            self.assertIn("private chat", manager.config(group_message, "set openai_api_key secret-value"))
            self.assertFalse(db.has_secret("openai_api_key"))
            self.assertEqual(
                manager.config(private_message, "set openai_api_key secret-value"),
                "Config set: openai_api_key",
            )
            shown = manager.config(private_message, "show")
            self.assertIn("openai_api_key=<set>", shown)
            self.assertNotIn("secret-value", shown)

    def test_admin_sets_and_clears_chat_local_repository(self):
        class Repositories:
            def __init__(self):
                self.calls = []

            def validate(self, path, repo):
                self.calls.append((path, repo))
                return Path(path)

        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.upsert_user(10, "admin", "admin")
            db.allow_repo("owner/repo", 10)
            db.set_chat_repo(20, "owner/repo", 10)
            manager = object.__new__(CommitManager)
            manager.db = db
            manager.permissions = PermissionService(db)
            manager.repositories = Repositories()
            message = IncomingMessage(20, 10, "admin", "")
            cache = str(Path(temp_dir) / "cache.git")

            self.assertIn("cache set", manager.repo(message, f"local set {cache}"))
            self.assertEqual(db.get_chat_settings(20)["local_repo_path"], cache)
            manager.repositories.calls.clear()
            self.assertIn("(configured; run /repo check to validate)", manager.repo(message, "show"))
            self.assertEqual(manager.repositories.calls, [])
            self.assertIn("(ok)", manager.repo(message, "check"))
            self.assertEqual(manager.repositories.calls, [(cache, "owner/repo")])
            self.assertIn("cache cleared", manager.repo(message, "local clear"))
            self.assertIsNone(db.get_chat_settings(20)["local_repo_path"])

    def test_repo_check_reports_invalid_cache(self):
        class Repositories:
            @staticmethod
            def validate(path, repo):
                from telegram_project_manager.integrations.git.local_repository import (
                    LocalRepositoryError,
                )

                raise LocalRepositoryError("origin does not match")

        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.upsert_user(10, "admin", "admin")
            db.allow_repo("owner/repo", 10)
            db.set_chat_repo(20, "owner/repo", 10)
            db.set_chat_local_repo(20, "/cache/repo.git", 10)
            manager = object.__new__(CommitManager)
            manager.db = db
            manager.permissions = PermissionService(db)
            manager.repositories = Repositories()

            response = manager.repo(IncomingMessage(20, 10, "admin", ""), "check")

            self.assertIn("(invalid: origin does not match)", response)

    def test_admin_configures_per_repo_deployment_workflow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.upsert_user(10, "admin", "admin")
            db.allow_repo("owner/repo", 10)
            db.set_chat_repo(20, "owner/repo", 10)
            manager = object.__new__(CommitManager)
            manager.db = db
            manager.permissions = PermissionService(db)
            manager.repositories = object()
            message = IncomingMessage(20, 10, "admin", "")

            self.assertFalse(db.is_repo_deploy_enabled("owner/repo"))
            self.assertIn(
                "workflow set",
                manager.repo(message, "deploy set owner/repo deploy.yml"),
            )
            self.assertEqual(db.get_repo_deploy_workflow("owner/repo"), "deploy.yml")
            self.assertFalse(db.is_repo_deploy_enabled("owner/repo"))
            self.assertIn(
                "Deployment enabled",
                manager.repo(message, "deploy enable owner/repo"),
            )
            self.assertTrue(db.is_repo_deploy_enabled("owner/repo"))
            self.assertIn("Deploy enabled: yes", manager.repo(message, "show"))
            self.assertIn(
                "Deployment disabled",
                manager.repo(message, "deploy disable owner/repo"),
            )
            self.assertFalse(db.is_repo_deploy_enabled("owner/repo"))
            self.assertIn("Deploy workflow: deploy.yml", manager.repo(message, "show"))
            self.assertIn(
                "workflow cleared",
                manager.repo(message, "deploy clear owner/repo"),
            )
            self.assertEqual(db.get_repo_deploy_workflow("owner/repo"), "")

    def test_topics_require_explicit_independent_repository_settings(self):
        class Repositories:
            @staticmethod
            def validate(path, repo):
                return Path(path)

        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.upsert_user(10, "admin", "admin")
            db.allow_repo("owner/group", 10)
            db.allow_repo("owner/topic", 10)
            db.set_chat_repo(20, "owner/group", 10, "stable")
            manager = object.__new__(CommitManager)
            manager.db = db
            manager.permissions = PermissionService(db)
            manager.repositories = Repositories()
            topic = IncomingMessage(20, 10, "admin", "", thread_id=101)

            self.assertIsNone(db.get_scope_settings(20, 101)["active_repo"])
            self.assertIn("not set", manager.repo(topic, "show"))
            self.assertIn("owner/topic", manager.repo(topic, "set owner/topic"))
            self.assertEqual(db.get_scope_settings(20, 101)["active_repo"], "owner/topic")
            self.assertEqual(db.get_chat_settings(20)["active_repo"], "owner/group")
            self.assertIsNone(db.get_scope_settings(20, 102)["active_repo"])

            manager.branch(topic, "develop")
            cache = str(Path(temp_dir) / "topic.git")
            manager.repo(topic, f"local set {cache}")
            settings = db.get_scope_settings(20, 101)
            self.assertEqual(settings["default_branch"], "develop")
            self.assertEqual(settings["local_repo_path"], cache)

    def test_plan_cannot_be_cancelled_from_another_topic(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.upsert_user(10, "admin", "admin")
            db.create_plan(
                {
                    "id": "p-1",
                    "telegram_chat_id": 20,
                    "telegram_thread_id": 101,
                    "telegram_user_id": 10,
                    "repo": "owner/repo",
                    "base_branch": "main",
                    "target_branch": "bot/10/p-1",
                    "request_text": "change",
                    "plan_json": {},
                    "status": "pending",
                    "created_at": 1,
                    "expires_at": 9999999999,
                }
            )
            manager = object.__new__(CommitManager)
            manager.db = db
            manager.permissions = PermissionService(db)

            response = manager.cancel(
                IncomingMessage(20, 10, "admin", "", thread_id=102), "p-1"
            )

            self.assertIn("different chat or topic", response)
            self.assertEqual(db.get_plan("p-1")["status"], "pending")


class FakeIssueReader:
    def __init__(self, issues=(), error=None):
        self.issues = list(issues)
        self.error = error
        self.calls = []

    def list_open_issues(self, repo):
        self.calls.append(repo)
        if self.error:
            raise self.error
        return list(self.issues)


class IssueListCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "bot.db")
        self.db.initialize()
        self.db.upsert_user(10, "admin", "admin")
        self.db.allow_repo("owner/repo", 10)
        self.db.set_chat_repo(20, "owner/repo", 10, "main")

    def manager(self, reader):
        manager = object.__new__(CommitManager)
        manager.db = self.db
        manager.permissions = PermissionService(self.db)
        manager.issue_reader = reader
        return manager

    async def handle(self, manager, message):
        async def run_sync(function, *args):
            return function(*args)

        with patch(
            "telegram_project_manager.bots.commit_manager.commands.asyncio.to_thread",
            new=run_sync,
        ):
            return await manager.handle(message)

    async def test_issues_command_renders_safe_clickable_results(self):
        reader = FakeIssueReader(
            [
                IssueSummary(9, "Fix <unsafe & title>", "https://github.com/owner/repo/issues/9"),
                IssueSummary(4, "Older issue", "https://github.com/owner/repo/issues/4"),
            ]
        )

        response = await self.handle(
            self.manager(reader), IncomingMessage(20, 10, "admin", "/issues")
        )

        self.assertIsInstance(response, OutgoingMessage)
        self.assertEqual(reader.calls, ["owner/repo"])
        self.assertIn("Open issues for owner/repo:", response.text)
        self.assertIn('<a href="https://github.com/owner/repo/issues/9">#9</a>', response.text)
        self.assertIn("Fix &lt;unsafe &amp; title&gt;", response.text)
        self.assertLess(response.text.index("#9"), response.text.index("#4"))
        self.assertEqual(response.keyboard, ())
        self.assertTrue(response.disable_link_preview)

    async def test_issues_command_uses_independent_topic_repository(self):
        self.db.allow_repo("owner/topic", 10)
        self.db.set_scope_repo(20, 101, "owner/topic", 10, "main")
        reader = FakeIssueReader()

        response = await self.handle(
            self.manager(reader),
            IncomingMessage(20, 10, "admin", "/issues", thread_id=101),
        )

        self.assertEqual(response, "No open issues for owner/topic.")
        self.assertEqual(reader.calls, ["owner/topic"])

    async def test_issues_command_validates_usage_and_repository(self):
        reader = FakeIssueReader()
        manager = self.manager(reader)

        usage = await self.handle(manager, IncomingMessage(20, 10, "admin", "/issues closed"))
        missing = await self.handle(manager, IncomingMessage(21, 10, "admin", "/issues"))
        self.db.set_chat_repo(22, "owner/not-allowed", 10, "main")
        disallowed = await self.handle(manager, IncomingMessage(22, 10, "admin", "/issues"))

        self.assertEqual(usage, "Usage: /issues")
        self.assertIn("No active repo for this chat", missing)
        self.assertIn("not in allowed repo list", disallowed)
        self.assertEqual(reader.calls, [])

    async def test_issues_command_reports_and_audits_reader_failure(self):
        error = GhError(GhResult(["gh", "issue", "list"], 1, "", "auth failed", 1))

        response = await self.handle(
            self.manager(FakeIssueReader(error=error)),
            IncomingMessage(20, 10, "admin", "/issues"),
        )

        self.assertEqual(response, "Issues not loaded.\nReason: auth failed")
        with self.db.session() as conn:
            event = conn.execute(
                "SELECT action, status, details_json FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(event["action"], "issues.list")
        self.assertEqual(event["status"], "failed")
        self.assertEqual(
            json.loads(event["details_json"]),
            {"repo": "owner/repo", "error": "auth failed"},
        )

    async def test_help_advertises_issues_without_claiming_issue_command(self):
        manager = self.manager(FakeIssueReader())

        self.assertIn("/issues", manager.help())
        self.assertIsNone(
            await manager.handle(IncomingMessage(20, 10, "admin", "/issue create one"))
        )


if __name__ == "__main__":
    unittest.main()
