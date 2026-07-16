import tempfile
import time
import unittest
from pathlib import Path

from telegram_project_manager.bots.issue_manager.commands import IssueManager
from telegram_project_manager.bots.issue_manager.schemas import IssueDraft
from telegram_project_manager.bots.issue_manager.schemas import PossibleCause, RelevantFile
from telegram_project_manager.integrations.gh.issues import IssueResult
from telegram_project_manager.integrations.gh.repository_context import RepositoryContextError
from telegram_project_manager.platform.responses import OutgoingMessage
from telegram_project_manager.platform.router import IncomingAttachment, IncomingMessage
from telegram_project_manager.platform.storage.db import Database


class FakePlanner:
    def __init__(self):
        self.create_calls = []
        self.revise_calls = []

    def create_draft(self, **kwargs):
        self.create_calls.append(kwargs)
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

    def revise_draft(self, **kwargs):
        self.revise_calls.append(kwargs)
        return IssueDraft(
            title="Short title",
            summary="Revised summary.",
            actual_behavior="Clicking Save has no effect.",
            expected_behavior="The form is saved.",
            codebase_context="Fresh repository context.",
            relevant_files=(RelevantFile("src/form.py", "Contains the save handler."),),
            possible_causes=(),
            context_branch="main",
            context_commit_sha="fedcba9876543210",
        )


class FakeExecution:
    def execute(self, draft_id, user_id):
        raise AssertionError("not used")


class IssueManagerTests(unittest.TestCase):
    @staticmethod
    def create_pending_draft(
        db,
        *,
        draft_id="i-abcdef12",
        user_id=10,
        chat_id=20,
        thread_id=None,
        local_repo_path="",
    ):
        now = int(time.time())
        db.create_issue_draft(
            {
                "id": draft_id,
                "telegram_chat_id": chat_id,
                "telegram_thread_id": thread_id,
                "telegram_user_id": user_id,
                "repo": "owner/repo",
                "default_branch": "main",
                "local_repo_path": local_repo_path,
                "request_text": "button broken",
                "issue_json": {
                    "title": "Original title",
                    "summary": "Original summary",
                    "actual_behavior": "Nothing happens",
                    "expected_behavior": "The form is saved",
                    "codebase_context": "Original context",
                    "relevant_files": [
                        {"path": "src/form.py", "reason": "Contains the handler"}
                    ],
                    "possible_causes": [],
                    "context_branch": "main",
                    "context_commit_sha": "abcdef",
                },
                "status": "pending",
                "created_at": now,
                "expires_at": now + 300,
            },
            [
                {
                    "position": 0,
                    "telegram_file_id": "old-file",
                    "telegram_file_unique_id": "old-unique",
                    "mime_type": "image/png",
                    "file_size": 100,
                }
            ],
        )

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

    def test_topic_issue_uses_only_its_repository_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.upsert_user(10, "admin", "admin")
            db.allow_repo("owner/group", 10)
            db.allow_repo("owner/topic", 10)
            db.set_chat_repo(20, "owner/group", 10)
            db.set_scope_repo(20, 101, "owner/topic", 10, "develop")
            db.set_scope_local_repo(20, 101, "/cache/topic.git", 10)
            planner = FakePlanner()
            manager = IssueManager(db, planner, FakeExecution())

            missing = manager.create(
                IncomingMessage(20, 10, "admin", "", thread_id=102), "bug"
            )
            created = manager.create(
                IncomingMessage(20, 10, "admin", "", thread_id=101), "bug"
            )

            self.assertIn("No active repo for this topic", missing)
            self.assertIn("Draft ID", created)
            self.assertEqual(planner.create_calls[0]["thread_id"], 101)
            self.assertEqual(planner.create_calls[0]["repo"], "owner/topic")
            self.assertEqual(planner.create_calls[0]["default_branch"], "develop")
            self.assertEqual(planner.create_calls[0]["local_repo_path"], "/cache/topic.git")

    def test_issue_draft_revision_is_bound_to_topic_and_snapshotted_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.upsert_user(10, "admin", "admin")
            self.create_pending_draft(
                db, thread_id=101, local_repo_path="/cache/original.git"
            )
            planner = FakePlanner()
            manager = IssueManager(db, planner, FakeExecution())

            rejected = manager.revise(
                IncomingMessage(20, 10, "admin", "change", thread_id=102),
                "i-abcdef12",
                "change",
            )
            accepted = manager.revise(
                IncomingMessage(20, 10, "admin", "change", thread_id=101),
                "i-abcdef12",
                "change",
            )

            self.assertIn("different topic", rejected)
            self.assertIn("Issue draft revised", accepted)
            self.assertEqual(planner.revise_calls[0]["local_repo_path"], "/cache/original.git")

    def test_created_issue_has_code_and_issue_buttons(self):
        class SuccessfulExecution:
            @staticmethod
            def execute(draft_id, user_id, chat_id, thread_id):
                return IssueResult(
                    repo="owner/repo",
                    number=12,
                    url="https://github.com/owner/repo/issues/12",
                    title="Broken button",
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.upsert_user(10, "admin", "admin")
            manager = IssueManager(db, FakePlanner(), SuccessfulExecution())

            response = manager.confirm(
                IncomingMessage(20, 10, "admin", "/confirm i-abcdef12"),
                "i-abcdef12",
            )

            self.assertIsInstance(response, OutgoingMessage)
            assert isinstance(response, OutgoingMessage)
            buttons = response.reply_markup()["inline_keyboard"][0]
            self.assertEqual(buttons[0]["callback_data"], "command:/code owner/repo#12")
            self.assertEqual(buttons[1]["url"], "https://github.com/owner/repo/issues/12")

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

    def test_original_body_preview_omits_generated_fields(self):
        issue = IssueDraft(
            "Generated title",
            "",
            "",
            "",
            body_mode="original",
            raw_body="exact user body",
        )
        preview = IssueManager._format_preview(
            heading="Issue draft created.",
            draft_id="i-12345678",
            repo="owner/repo",
            issue=issue,
            revision=1,
            image_count=0,
        )
        self.assertIn("Body mode: original prompt", preview)
        self.assertIn("Body: exact user body", preview)
        self.assertNotIn("Actual behavior:", preview)

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

    def test_author_feedback_revises_same_draft_and_appends_image(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.upsert_user(10, "admin", "admin")
            self.create_pending_draft(db)
            planner = FakePlanner()
            manager = IssueManager(db, planner, FakeExecution())
            before = int(time.time()) + 3500
            response = manager.revise(
                IncomingMessage(
                    20,
                    10,
                    "admin",
                    "make the title shorter",
                    attachments=(
                        IncomingAttachment("new-file", "new-unique", "image/jpeg", 200),
                    ),
                ),
                "i-abcdef12",
                "make the title shorter",
            )

            self.assertIn("Issue draft revised.", response)
            self.assertIn("Draft ID: i-abcdef12", response)
            self.assertIn("Revision: 2", response)
            self.assertIn("Images: 2", response)
            stored = db.get_issue_draft("i-abcdef12")
            assert stored is not None
            self.assertEqual(stored["issue_json"]["title"], "Short title")
            self.assertEqual(len(stored["attachments"]), 2)
            self.assertGreaterEqual(stored["expires_at"], before)
            revisions = db.get_issue_draft_revisions("i-abcdef12")
            self.assertEqual([item["revision_number"] for item in revisions], [1, 2])
            self.assertEqual(revisions[1]["feedback_text"], "make the title shorter")
            self.assertEqual(planner.revise_calls[0]["feedback_history"], [])

    def test_only_original_author_can_edit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.upsert_user(10, "author", "admin")
            db.upsert_user(11, "other", "admin")
            self.create_pending_draft(db)
            planner = FakePlanner()
            manager = IssueManager(db, planner, FakeExecution())
            response = manager.revise(
                IncomingMessage(20, 11, "other", "change it"),
                "i-abcdef12",
                "change it",
            )
            self.assertIn("Only the original author", response)
            self.assertEqual(planner.revise_calls, [])

    def test_image_only_edit_skips_llm_and_duplicate_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.upsert_user(10, "admin", "admin")
            self.create_pending_draft(db)
            planner = FakePlanner()
            manager = IssueManager(db, planner, FakeExecution())
            duplicate = IncomingAttachment("retry-file", "old-unique", "image/png", 100)
            self.assertIn(
                "No changes supplied",
                manager.revise(
                    IncomingMessage(20, 10, "admin", "", attachments=(duplicate,)),
                    "i-abcdef12",
                    "",
                ),
            )
            new_image = IncomingAttachment("new-file", "new-unique", "image/png", 100)
            response = manager.revise(
                IncomingMessage(20, 10, "admin", "", attachments=(new_image,)),
                "i-abcdef12",
                "",
            )
            self.assertIn("Revision: 2", response)
            self.assertEqual(planner.revise_calls, [])

    def test_planner_failure_does_not_append_revision_images(self):
        class FailingRevisionPlanner(FakePlanner):
            def revise_draft(self, **kwargs):
                raise RepositoryContextError("context unavailable")

        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.upsert_user(10, "admin", "admin")
            self.create_pending_draft(db)
            manager = IssueManager(db, FailingRevisionPlanner(), FakeExecution())
            response = manager.revise(
                IncomingMessage(
                    20,
                    10,
                    "admin",
                    "change",
                    attachments=(IncomingAttachment("new", "new", "image/png", 100),),
                ),
                "i-abcdef12",
                "change",
            )
            self.assertIn("Issue draft not revised", response)
            stored = db.get_issue_draft("i-abcdef12")
            assert stored is not None
            self.assertEqual(len(stored["attachments"]), 1)
            self.assertEqual(stored["revision_number"], 1)


if __name__ == "__main__":
    unittest.main()
