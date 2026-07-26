from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from telegram_project_manager.integrations.gh.runner import GhRunner

PULL_REQUEST_TITLE_LIMIT = 120


@dataclass(frozen=True)
class PullRequestSummary:
    number: int
    title: str
    url: str


class GhPullRequestReader:
    def __init__(self, gh: GhRunner) -> None:
        self.gh = gh

    def list_open_pull_requests(
        self, repo: str, limit: int = 20
    ) -> list[PullRequestSummary]:
        if limit < 1:
            raise ValueError("Pull request list limit must be positive")
        result = self.gh.run(
            [
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--limit",
                str(limit),
                "--search",
                "sort:updated-desc",
                "--json",
                "number,title",
            ]
        )
        try:
            value = result.json()
        except (TypeError, ValueError) as exc:
            raise ValueError("GitHub pull request list returned invalid JSON") from exc
        if not isinstance(value, list):
            raise ValueError("GitHub pull request list returned an unexpected response")

        pull_requests: list[PullRequestSummary] = []
        encoded_repo = quote(repo, safe="/")
        for item in value[:limit]:
            if not isinstance(item, dict):
                raise ValueError("GitHub pull request list returned an invalid pull request")
            number = item.get("number")
            title = item.get("title")
            if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                raise ValueError("GitHub pull request list returned an invalid pull request number")
            if not isinstance(title, str):
                raise ValueError("GitHub pull request list returned an invalid pull request title")
            normalized_title = " ".join(title.split())
            if not normalized_title:
                raise ValueError("GitHub pull request list returned an empty pull request title")
            if len(normalized_title) > PULL_REQUEST_TITLE_LIMIT:
                normalized_title = (
                    normalized_title[: PULL_REQUEST_TITLE_LIMIT - 3].rstrip() + "..."
                )
            pull_requests.append(
                PullRequestSummary(
                    number=number,
                    title=normalized_title,
                    url=f"https://github.com/{encoded_repo}/pull/{number}",
                )
            )
        return pull_requests
