import tempfile
import unittest
from pathlib import Path

from telegram_project_manager.bots.issue_manager.commands import IssueManager
from telegram_project_manager.bots.issue_manager.schemas import IssueDraft
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


if __name__ == "__main__":
    unittest.main()
