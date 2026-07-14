import tempfile
import unittest
from pathlib import Path

from telegram_project_manager.bots.issue_manager.commands import IssueManager
from telegram_project_manager.bots.issue_manager.schemas import IssueDraft
from telegram_project_manager.bots.issue_manager.schemas import PossibleCause, RelevantFile
from telegram_project_manager.integrations.gh.repository_context import RepositoryContextError
from telegram_project_manager.platform.router import IncomingAttachment, IncomingMessage
from telegram_project_manager.platform.storage.db import Database


class FakePlanner:
    def create_draft(self, **kwargs):
        return (
            "i-12345678",
            IssueDraft(
                title="Fix broken button",
                summary="The save button does not work.",
                actual_behavior="Clicking Save has no effect.",
                expected_behavior="Clicking Save persists the form.",
                codebase_context="The form handler owns the save flow.",
                relevant_files=(RelevantFile("src/form.py", "Contains the save handler."),),
                possible_causes=(PossibleCause("The handler exits early.", ("src/form.py",)),),
                context_branch="main",
                context_commit_sha="abcdef1234567890",
            ),
        )


class FakeExecution:
    def execute(self, draft_id, user_id):
        raise AssertionError("not used")


class IssueManagerTests(unittest.TestCase):
    def test_admin_creates_issue_draft_for_chat_repo_with_image(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.upsert_user(10, "admin", "admin")
            db.allow_repo("owner/repo", 10)
            db.set_chat_repo(20, "owner/repo", 10)
            manager = IssueManager(db, FakePlanner(), FakeExecution())
            message = IncomingMessage(
                20,
                10,
                "admin",
                "/issue button broken",
                attachments=(IncomingAttachment("file", "unique", "image/png", 100),),
            )
            response = manager.create(message, "button broken")
            self.assertIn("Draft ID: i-12345678", response)
            self.assertIn("Actual behavior: Clicking Save has no effect.", response)
            self.assertIn("Expected behavior: Clicking Save persists the form.", response)
            self.assertIn("Codebase context: The form handler owns the save flow.", response)
            self.assertIn("Relevant files: 1", response)
            self.assertIn("Possible causes: 1", response)
            self.assertIn("Context commit: abcdef123456", response)
            self.assertIn("Images: 1", response)

    def test_non_admin_cannot_create_issue_draft(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            manager = IssueManager(db, FakePlanner(), FakeExecution())
            response = manager.create(IncomingMessage(1, 2, "", "", False), "bug")
            self.assertIn("Unauthorized", response)

    def test_issue_draft_round_trip_with_attachment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.create_issue_draft(
                {
                    "id": "i-abcdef12",
                    "telegram_chat_id": 1,
                    "telegram_user_id": 2,
                    "repo": "owner/repo",
                    "default_branch": "main",
                    "request_text": "bug",
                    "issue_json": {
                        "title": "Bug",
                        "summary": "Summary",
                        "actual_behavior": "Actual",
                        "expected_behavior": "Expected",
                    },
                    "status": "pending",
                    "created_at": 1,
                    "expires_at": 2,
                },
                [
                    {
                        "position": 0,
                        "telegram_file_id": "file",
                        "telegram_file_unique_id": "unique",
                        "mime_type": "image/png",
                        "file_size": 100,
                    }
                ],
            )
            draft = db.get_issue_draft("i-abcdef12")
            self.assertIsNotNone(draft)
            assert draft is not None
            self.assertEqual(draft["issue_json"]["expected_behavior"], "Expected")
            self.assertEqual(draft["attachments"][0]["telegram_file_id"], "file")

    def test_issue_body_has_fixed_sections_and_marker(self):
        issue = IssueDraft("Title", "Summary", "Actual", "Expected")
        body = issue.body(["![Issue image 1](image)"], "<!-- marker -->")
        self.assertIn("## Summary", body)
        self.assertIn("## Actual behavior", body)
        self.assertIn("## Expected behavior", body)
        self.assertIn("## Images", body)
        self.assertTrue(body.endswith("<!-- marker -->"))

    def test_issue_body_renders_pinned_context_and_hypotheses(self):
        issue = IssueDraft(
            "Title",
            "Summary",
            "Actual",
            "Expected",
            codebase_context="The handler controls saves.",
            relevant_files=(RelevantFile("src/form handler.py", "Contains the handler."),),
            possible_causes=(PossibleCause("It may exit early.", ("src/form handler.py",)),),
            context_branch="main",
            context_commit_sha="abcdef",
        )
        body = issue.body([], "<!-- marker -->", "owner/repo")
        self.assertIn("## Codebase context", body)
        self.assertIn("## Relevant files", body)
        self.assertIn("blob/abcdef/src/form%20handler.py", body)
        self.assertIn("## Possible causes", body)
        self.assertIn("**Hypothesis:**", body)

    def test_context_failure_blocks_draft(self):
        class FailingPlanner:
            def create_draft(self, **kwargs):
                raise RepositoryContextError("repository tree is truncated")

        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.upsert_user(10, "admin", "admin")
            db.allow_repo("owner/repo", 10)
            db.set_chat_repo(20, "owner/repo", 10)
            manager = IssueManager(db, FailingPlanner(), FakeExecution())
            response = manager.create(IncomingMessage(20, 10, "admin", "/issue bug"), "bug")
            self.assertIn("Issue draft not created", response)
            self.assertIn("truncated", response)


if __name__ == "__main__":
    unittest.main()
