import unittest

from telegram_project_manager.bots.commit_manager.schemas import CommitPlan, PlanValidationError, validate_path, validate_repo


class SchemaTests(unittest.TestCase):
    def test_valid_plan(self):
        plan = CommitPlan.from_llm(
            {
                "intent": "create_commit",
                "commit_message": "Add README",
                "changes": [{"path": "README.md", "content": "hello"}],
            },
            fallback_repo="owner/repo",
            fallback_branch="main",
            target_branch="bot/1/abc",
        )
        self.assertEqual(plan.repo, "owner/repo")
        self.assertEqual(plan.changes[0].path, "README.md")

    def test_rejects_bad_repo(self):
        with self.assertRaises(PlanValidationError):
            validate_repo("bad")

    def test_rejects_sensitive_path(self):
        with self.assertRaises(PlanValidationError):
            validate_path(".env")


if __name__ == "__main__":
    unittest.main()

