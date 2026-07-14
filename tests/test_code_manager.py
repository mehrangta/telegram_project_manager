import asyncio
import tempfile
import unittest
from pathlib import Path

from openai_codex import Sandbox
from openai_codex.types import ReasoningEffort

from telegram_project_manager.bots.code_manager.progress import CodeProgressReporter
from telegram_project_manager.bots.code_manager.codex_sdk import _codex_config, _safe_progress
from telegram_project_manager.bots.code_manager.prompts import coding_prompt
from telegram_project_manager.bots.code_manager.schemas import CodeJobValidationError, CodeResult
from telegram_project_manager.bots.code_manager.service import CodeJobService
from telegram_project_manager.bots.code_manager.workspace import (
    GitWorkspaceService,
    IssueContext,
    WorkspaceError,
)
from telegram_project_manager.platform.storage.db import Database


PLAN = {
    "summary": "Implement the requested behavior safely.",
    "steps": [{"title": "Update handler", "details": "Change the issue handler and preserve existing behavior.", "files": ["src/handler.py"]}],
    "tests": ["pytest"],
    "risks": [],
    "questions": [],
}

RESULT = {
    "summary": "Updated the handler and its tests.",
    "commit_message": "fix: implement issue request",
    "tests": [{"command": "pytest", "status": "passed", "summary": "All tests passed."}],
}


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []

    def send_message(self, chat_id, text, thread_id=None):
        self.sent.append((chat_id, text, thread_id))
        return {"message_id": 77}

    def edit_message_text(self, chat_id, message_id, text):
        self.edited.append((chat_id, message_id, text))
        return {"message_id": message_id}


class FakeCodex:
    def __init__(self):
        self.calls = []
        self.interrupted = []

    async def run_turn(self, **kwargs):
        self.calls.append(kwargs)
        await kwargs["on_thread"]("thread-1")
        await kwargs["on_progress"]({"kind": "phase", "text": "Inspecting repository"})
        raw = PLAN if kwargs["sandbox"] == Sandbox.read_only else RESULT
        return "thread-1", raw

    async def interrupt(self, job_id):
        self.interrupted.append(job_id)

    async def close(self):
        return None


class FakeWorkspaces:
    def __init__(self):
        self.plan_commits = []
        self.code_commits = []
        self.cleaned = []
        self.removed_plans = []

    def prepare(self, *, path, **kwargs):
        (path / ".git").mkdir(parents=True)
        return "base-sha"

    def checkout_existing(self, *, path, **kwargs):
        (path / ".git").mkdir(parents=True)
        return "branch-sha"

    def refresh_base(self, path, base_branch):
        return "fresh-base-sha"

    def push_rebased_branch(self, path):
        return None

    def is_dirty(self, path):
        return False

    def commit_plan(self, **kwargs):
        self.plan_commits.append(kwargs)
        return "plan-sha"

    def validate_code_changes(self, **kwargs):
        return ["src/handler.py", "tests/test_handler.py"]

    def remove_plan(self, **kwargs):
        self.removed_plans.append(kwargs)

    def commit_code(self, **kwargs):
        self.code_commits.append(kwargs)
        return "code-sha"

    def cleanup(self, path):
        self.cleaned.append(path)


class FakeGitHub:
    def __init__(self):
        self.created = []
        self.updated = []
        self.ready = []
        self.discarded = []

    def create_draft_pr(self, **kwargs):
        self.created.append(kwargs)
        return {"number": 42, "html_url": "https://github.com/owner/repo/pull/42"}

    def update_pr(self, **kwargs):
        self.updated.append(kwargs)

    def mark_ready(self, url):
        self.ready.append(url)

    def discard(self, **kwargs):
        self.discarded.append(kwargs)


async def wait_for_status(db, job_id, expected):
    for _ in range(200):
        job = db.get_code_job(job_id)
        if job and job["status"] == expected:
            return job
        await asyncio.sleep(0.01)
    job = db.get_code_job(job_id)
    actual = job["status"] if job else None
    raise AssertionError(f"expected {expected}, got {actual}")


class CodeJobServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "bot.db")
        self.db.initialize()
        self.bot = FakeBot()
        self.codex = FakeCodex()
        self.workspaces = FakeWorkspaces()
        self.github = FakeGitHub()
        self.service = CodeJobService(
            db=self.db,
            codex=self.codex,
            workspaces=self.workspaces,
            github=self.github,
            reporter=CodeProgressReporter(self.db, self.bot, min_interval=0),
        )
        self.issue = IssueContext(
            repo="owner/repo", number=12, title="Broken handler",
            body="The handler does not save.", url="https://github.com/owner/repo/issues/12",
            comments=("Please preserve compatibility.",),
        )

    async def asyncTearDown(self):
        await self.service.shutdown()
        self.temp.cleanup()

    async def test_plan_approval_runs_code_and_marks_draft_pr_ready(self):
        job_id = await self.service.create_job(chat_id=10, user_id=20, thread_id=30, issue=self.issue, base_branch="main", skip_plan=False)
        planned = await wait_for_status(self.db, job_id, "awaiting_approval")
        self.assertEqual(planned["plan_json"]["summary"], PLAN["summary"])
        self.assertEqual(planned["pull_request_number"], 42)
        self.assertIn("Code Job ID:", self.bot.sent[0][1])
        self.assertEqual(self.codex.calls[0]["sandbox"], Sandbox.read_only)
        self.assertEqual(self.codex.calls[0]["effort"], ReasoningEffort.high)

        await self.service.approve(job_id)
        ready = await wait_for_status(self.db, job_id, "ready")
        self.assertEqual(ready["result_json"]["commit_sha"], "code-sha")
        self.assertEqual(self.codex.calls[1]["sandbox"], Sandbox.workspace_write)
        self.assertEqual(self.codex.calls[1]["effort"], ReasoningEffort.medium)
        self.assertEqual(len(self.github.created), 1)
        self.assertEqual(len(self.github.updated), 1)
        self.assertEqual(self.github.ready, ["https://github.com/owner/repo/pull/42"])
        self.assertEqual(
            self.workspaces.removed_plans[0]["plan_path"],
            f".codex/plans/{job_id}.md",
        )
        for _ in range(100):
            if self.workspaces.cleaned:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.workspaces.cleaned)
        self.assertTrue(any("Implementation ready for review" in item[2] for item in self.bot.edited))

    async def test_skip_plan_codes_immediately_and_creates_pr(self):
        job_id = await self.service.create_job(chat_id=10, user_id=20, thread_id=None, issue=self.issue, base_branch="main", skip_plan=True)
        ready = await wait_for_status(self.db, job_id, "ready")
        self.assertIsNone(ready["plan_json"])
        self.assertEqual(len(self.codex.calls), 1)
        self.assertEqual(self.codex.calls[0]["sandbox"], Sandbox.workspace_write)
        self.assertTrue(self.workspaces.code_commits[0]["first_push"])


class CodeSafetyTests(unittest.TestCase):
    def test_coding_prompt_excludes_plan_from_model_validation(self):
        prompt = coding_prompt(
            {"title": "Issue", "body": "Body", "comments": []},
            PLAN,
            ".codex/plans/c-test.md",
        )
        self.assertIn("Do not inspect, validate, modify, or remove", prompt)
        self.assertIn("not a validation result", prompt)

    def test_trusted_host_removes_only_workspace_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir)
            plan = path / ".codex" / "plans" / "c-test.md"
            plan.parent.mkdir(parents=True)
            plan.write_text("temporary", encoding="utf-8")
            GitWorkspaceService.remove_plan(
                path=path,
                plan_path=".codex/plans/c-test.md",
            )
            self.assertFalse(plan.exists())

    def test_trusted_host_rejects_plan_path_outside_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "repo"
            path.mkdir()
            with self.assertRaisesRegex(WorkspaceError, "escapes the workspace"):
                GitWorkspaceService.remove_plan(
                    path=path,
                    plan_path="../outside.md",
                )

    def test_progress_redacts_api_keys(self):
        event = _safe_progress(
            "error",
            {"error": {"message": "401 invalid key sk-example-secret-value"}},
        )
        assert event is not None
        self.assertNotIn("sk-example-secret-value", event["text"])
        self.assertIn("[REDACTED_API_KEY]", event["text"])

    def test_progress_redacts_sdk_masked_api_keys(self):
        event = _safe_progress(
            "error",
            {"error": {"message": "invalid sk-prefix****************suffix"}},
        )
        assert event is not None
        self.assertEqual(event["text"], "invalid [REDACTED_API_KEY]")

    def test_codex_config_sets_environment_and_runtime_base_url(self):
        config = _codex_config("secret", "http://codex.example.test")
        self.assertEqual(config.env["OPENAI_BASE_URL"], "http://codex.example.test")
        self.assertEqual(
            config.config_overrides,
            ('openai_base_url="http://codex.example.test"',),
        )

    def test_result_requires_a_successful_validation(self):
        with self.assertRaises(CodeJobValidationError):
            CodeResult.from_json({"summary": "Changed code.", "commit_message": "fix: change code", "tests": [{"command": "pytest", "status": "not_run", "summary": "Unavailable"}]})

    def test_sensitive_paths_are_blocked(self):
        class Runner:
            def run(self, args, *, cwd=None, timeout=300):
                if args[:3] == ["git", "status", "--porcelain"]:
                    return " M src/app.py\n?? .env.production\n"
                return ""

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir)
            (path / "src").mkdir()
            (path / "src" / "app.py").write_text("changed", encoding="utf-8")
            (path / ".env.production").write_text("SECRET=value", encoding="utf-8")
            with self.assertRaisesRegex(WorkspaceError, "sensitive path blocked"):
                GitWorkspaceService(Runner()).validate_code_changes(path=path, plan_path=".codex/plans/c-test.md")


if __name__ == "__main__":
    unittest.main()
