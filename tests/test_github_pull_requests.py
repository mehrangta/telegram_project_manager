import json
import unittest

from telegram_project_manager.integrations.gh.pull_requests import (
    PULL_REQUEST_TITLE_LIMIT,
    GhPullRequestReader,
)
from telegram_project_manager.integrations.gh.runner import GhError, GhResult


class FakePullRequestListGh:
    def __init__(self, value=None, *, stdout=None, error=None):
        self.value = [] if value is None else value
        self.stdout = stdout
        self.error = error
        self.calls = []

    def run(self, args):
        self.calls.append(args)
        if self.error:
            raise self.error
        stdout = self.stdout if self.stdout is not None else json.dumps(self.value)
        return GhResult(["gh", *args], 0, stdout, "", 1)


class GitHubPullRequestTests(unittest.TestCase):
    def test_lists_open_pull_requests_in_cli_order(self):
        gh = FakePullRequestListGh(
            [
                {"number": 9, "title": "  Newest\n pull request  "},
                {"number": 4, "title": "Older pull request"},
            ]
        )

        pull_requests = GhPullRequestReader(gh).list_open_pull_requests("owner/repo")

        self.assertEqual(
            gh.calls,
            [[
                "pr", "list", "--repo", "owner/repo", "--state", "open",
                "--limit", "20", "--search", "sort:updated-desc", "--json",
                "number,title",
            ]],
        )
        self.assertEqual([item.number for item in pull_requests], [9, 4])
        self.assertEqual(pull_requests[0].title, "Newest pull request")
        self.assertEqual(pull_requests[0].url, "https://github.com/owner/repo/pull/9")

    def test_pull_request_list_truncates_long_titles(self):
        pull_request = GhPullRequestReader(
            FakePullRequestListGh([{"number": 1, "title": "x" * 200}])
        ).list_open_pull_requests("owner/repo")[0]

        self.assertEqual(len(pull_request.title), PULL_REQUEST_TITLE_LIMIT)
        self.assertTrue(pull_request.title.endswith("..."))

    def test_pull_request_list_defensively_enforces_limit(self):
        pull_requests = GhPullRequestReader(
            FakePullRequestListGh(
                [
                    {"number": number, "title": f"Pull request {number}"}
                    for number in range(1, 26)
                ]
            )
        ).list_open_pull_requests("owner/repo")

        self.assertEqual(len(pull_requests), 20)
        self.assertEqual(pull_requests[-1].number, 20)

    def test_pull_request_list_accepts_empty_output(self):
        self.assertEqual(
            GhPullRequestReader(FakePullRequestListGh()).list_open_pull_requests(
                "owner/repo"
            ),
            [],
        )

    def test_pull_request_list_rejects_invalid_limit_and_malformed_output(self):
        with self.assertRaisesRegex(ValueError, "positive"):
            GhPullRequestReader(FakePullRequestListGh()).list_open_pull_requests(
                "owner/repo", 0
            )
        invalid_values = [
            ({"number": 1}, "unexpected response"),
            (["invalid"], "invalid pull request"),
            ([{"number": 0, "title": "Title"}], "invalid pull request number"),
            ([{"number": 1, "title": "  "}], "empty pull request title"),
        ]
        for value, message in invalid_values:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, message):
                    GhPullRequestReader(
                        FakePullRequestListGh(value)
                    ).list_open_pull_requests("owner/repo")
        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            GhPullRequestReader(
                FakePullRequestListGh(stdout="not json")
            ).list_open_pull_requests("owner/repo")

    def test_pull_request_list_propagates_gh_error(self):
        error = GhError(GhResult(["gh", "pr", "list"], 1, "", "auth failed", 1))
        with self.assertRaises(GhError):
            GhPullRequestReader(
                FakePullRequestListGh(error=error)
            ).list_open_pull_requests("owner/repo")


if __name__ == "__main__":
    unittest.main()
