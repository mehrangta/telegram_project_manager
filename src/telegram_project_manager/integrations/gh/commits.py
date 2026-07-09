from __future__ import annotations

from dataclasses import dataclass

from telegram_project_manager.bots.commit_manager.schemas import CommitPlan
from telegram_project_manager.integrations.gh.runner import GhRunner


@dataclass(frozen=True)
class CommitResult:
    repo: str
    branch: str
    sha: str
    commit_url: str
    comment_url: str | None
    files: list[str]


class GhCommitExecutor:
    def __init__(self, gh: GhRunner) -> None:
        self.gh = gh

    def create_commit(self, plan: CommitPlan) -> CommitResult:
        owner_repo = plan.repo
        base_ref = self.gh.api_json(f"repos/{owner_repo}/git/ref/heads/{plan.base_branch}")
        base_sha = base_ref["object"]["sha"]
        base_commit = self.gh.api_json(f"repos/{owner_repo}/git/commits/{base_sha}")
        base_tree = base_commit["tree"]["sha"]

        tree_entries = []
        for change in plan.changes:
            blob = self.gh.api_json(
                f"repos/{owner_repo}/git/blobs",
                method="POST",
                body={"content": change.content, "encoding": "utf-8"},
            )
            tree_entries.append(
                {
                    "path": change.path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob["sha"],
                }
            )

        tree = self.gh.api_json(
            f"repos/{owner_repo}/git/trees",
            method="POST",
            body={"base_tree": base_tree, "tree": tree_entries},
        )
        commit = self.gh.api_json(
            f"repos/{owner_repo}/git/commits",
            method="POST",
            body={"message": plan.commit_message, "tree": tree["sha"], "parents": [base_sha]},
        )
        commit_sha = commit["sha"]
        self.gh.api_json(
            f"repos/{owner_repo}/git/refs",
            method="POST",
            body={"ref": f"refs/heads/{plan.target_branch}", "sha": commit_sha},
        )

        comment_url = None
        if plan.github_comment:
            comment = self.gh.api_json(
                f"repos/{owner_repo}/commits/{commit_sha}/comments",
                method="POST",
                body={"body": plan.github_comment},
            )
            comment_url = comment.get("html_url")

        return CommitResult(
            repo=owner_repo,
            branch=plan.target_branch,
            sha=commit_sha,
            commit_url=f"https://github.com/{owner_repo}/commit/{commit_sha}",
            comment_url=comment_url,
            files=[change.path for change in plan.changes],
        )

