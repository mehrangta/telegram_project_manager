import unittest

from telegram_project_manager.integrations.gh.issues import GhIssueExecutor, _validate_image


class FakeTelegram:
    def download_file(self, file_id, max_bytes):
        return bytes.fromhex("89504e470d0a1a0a") + b"image"


class FakeGh:
    def __init__(self, existing_issue=None):
        self.existing_issue = existing_issue
        self.calls = []

    def api_value(self, endpoint, method="GET", body=None):
        self.calls.append((endpoint, method, body))
        return [self.existing_issue] if self.existing_issue else []

    def api_json(self, endpoint, method="GET", body=None):
        self.calls.append((endpoint, method, body))
        if endpoint.endswith("/git/ref/heads/issue-assets"):
            return {"object": {"sha": "base-sha"}}
        if endpoint.endswith("/git/commits/base-sha"):
            return {"tree": {"sha": "base-tree"}}
        if endpoint.endswith("/git/blobs"):
            return {"sha": "blob-sha"}
        if endpoint.endswith("/git/trees"):
            return {"sha": "tree-sha"}
        if endpoint.endswith("/git/commits"):
            return {"sha": "commit-sha"}
        if endpoint.endswith("/git/refs/heads/issue-assets"):
            return {"object": {"sha": "commit-sha"}}
        if endpoint.endswith("/issues"):
            return {
                "number": 42,
                "html_url": "https://github.com/owner/repo/issues/42",
                "title": body["title"],
            }
        raise AssertionError(endpoint)


def record(with_image=True):
    return {
        "id": "i-12345678",
        "repo": "owner/repo",
        "default_branch": "main",
        "issue_json": {
            "title": "Fix button",
            "summary": "Button is broken.",
            "actual_behavior": "Nothing happens.",
            "expected_behavior": "Form saves.",
        },
        "attachments": (
            [
                {
                    "position": 0,
                    "telegram_file_id": "file",
                    "mime_type": "image/png",
                    "asset_path": None,
                }
            ]
            if with_image
            else []
        ),
    }


class GitHubIssueTests(unittest.TestCase):
    def test_uploads_image_and_embeds_asset_branch_link(self):
        gh = FakeGh()
        result, paths = GhIssueExecutor(gh, FakeTelegram()).create_issue(record())
        self.assertEqual(result.number, 42)
        self.assertEqual(len(paths), 1)
        issue_call = next(call for call in gh.calls if call[0].endswith("/issues") and call[1] == "POST")
        self.assertIn("../blob/issue-assets/.issue-assets/i-12345678/", issue_call[2]["body"])
        self.assertIn("telegram-project-manager:draft=i-12345678", issue_call[2]["body"])

    def test_recovers_existing_issue_by_marker(self):
        existing = {
            "number": 7,
            "html_url": "https://github.com/owner/repo/issues/7",
            "title": "Existing",
            "body": "<!-- telegram-project-manager:draft=i-12345678 -->",
        }
        gh = FakeGh(existing)
        result, _ = GhIssueExecutor(gh, FakeTelegram()).create_issue(record(False))
        self.assertEqual(result.number, 7)
        self.assertFalse(any(call[1] == "POST" for call in gh.calls))

    def test_rejects_mime_signature_mismatch(self):
        with self.assertRaisesRegex(ValueError, "not valid"):
            _validate_image(b"not a png", "image/png")


if __name__ == "__main__":
    unittest.main()
