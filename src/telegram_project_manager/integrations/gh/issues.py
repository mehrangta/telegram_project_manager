from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Any

from telegram_project_manager.bots.issue_manager.schemas import IssueDraft
from telegram_project_manager.integrations.gh.runner import GhError, GhRunner
from telegram_project_manager.platform.telegram_bot import TelegramBotApi


ASSET_BRANCH = "issue-assets"
MAX_IMAGE_BYTES = 10_000_000
MAX_TOTAL_IMAGE_BYTES = 20_000_000
MIME_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif"}


@dataclass(frozen=True)
class IssueResult:
    repo: str
    number: int
    url: str
    title: str


class GhIssueExecutor:
    def __init__(self, gh: GhRunner, telegram: TelegramBotApi) -> None:
        self.gh = gh
        self.telegram = telegram

    def create_issue(self, record: dict[str, Any]) -> tuple[IssueResult, list[str]]:
        draft_id = str(record["id"])
        repo = str(record["repo"])
        marker = f"<!-- telegram-project-manager:draft={draft_id} -->"
        existing = self._find_existing_issue(repo, marker)
        issue = IssueDraft.from_llm(record["issue_json"])
        if existing:
            return self._result(repo, existing, issue.title), [
                str(item["asset_path"]) for item in record["attachments"] if item.get("asset_path")
            ]

        attachments = list(record["attachments"])
        existing_paths = [str(item["asset_path"]) for item in attachments if item.get("asset_path")]
        if attachments and len(existing_paths) == len(attachments):
            paths = existing_paths
        else:
            paths = self._upload_images(repo, str(record["default_branch"]), draft_id, attachments)
        image_links = [
            f"![Issue image {position + 1}](../blob/{ASSET_BRANCH}/{path}?raw=true)"
            for position, path in enumerate(paths)
        ]
        created = self.gh.api_json(
            f"repos/{repo}/issues",
            method="POST",
            body={"title": issue.title, "body": issue.body(image_links, marker)},
        )
        return self._result(repo, created, issue.title), paths

    def _find_existing_issue(self, repo: str, marker: str) -> dict[str, Any] | None:
        value = self.gh.api_value(f"repos/{repo}/issues?state=all&per_page=100")
        if not isinstance(value, list):
            return None
        return next(
            (item for item in value if isinstance(item, dict) and marker in str(item.get("body") or "")),
            None,
        )

    def _upload_images(
        self,
        repo: str,
        default_branch: str,
        draft_id: str,
        attachments: list[dict[str, Any]],
    ) -> list[str]:
        content_items: list[tuple[str, bytes]] = []
        total_size = 0
        for item in attachments:
            mime_type = str(item["mime_type"])
            extension = MIME_EXTENSIONS.get(mime_type)
            if not extension:
                raise ValueError(f"Unsupported image type: {mime_type}")
            content = self.telegram.download_file(str(item["telegram_file_id"]), MAX_IMAGE_BYTES)
            _validate_image(content, mime_type)
            total_size += len(content)
            if total_size > MAX_TOTAL_IMAGE_BYTES:
                raise ValueError("Issue images exceed the 20 MB total limit.")
            digest = hashlib.sha256(content).hexdigest()[:16]
            position = int(item["position"]) + 1
            path = f".issue-assets/{draft_id}/{position}-{digest}.{extension}"
            content_items.append((path, content))
        if not content_items:
            return []

        entries = []
        for path, content in content_items:
            blob = self.gh.api_json(
                f"repos/{repo}/git/blobs",
                method="POST",
                body={"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
            )
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        for attempt in range(2):
            ref = self._ensure_asset_branch(repo, default_branch)
            base_sha = str(ref["object"]["sha"])
            base_commit = self.gh.api_json(f"repos/{repo}/git/commits/{base_sha}")
            tree = self.gh.api_json(
                f"repos/{repo}/git/trees",
                method="POST",
                body={"base_tree": base_commit["tree"]["sha"], "tree": entries},
            )
            commit = self.gh.api_json(
                f"repos/{repo}/git/commits",
                method="POST",
                body={
                    "message": f"Add issue images for {draft_id}",
                    "tree": tree["sha"],
                    "parents": [base_sha],
                },
            )
            try:
                self.gh.api_json(
                    f"repos/{repo}/git/refs/heads/{ASSET_BRANCH}",
                    method="PATCH",
                    body={"sha": commit["sha"], "force": False},
                )
                return [path for path, _ in content_items]
            except GhError as exc:
                if attempt or not any(code in str(exc) for code in ("409", "422")):
                    raise
        raise RuntimeError("Asset branch update retry exhausted")

    def _ensure_asset_branch(self, repo: str, default_branch: str) -> dict[str, Any]:
        try:
            return self.gh.api_json(f"repos/{repo}/git/ref/heads/{ASSET_BRANCH}")
        except GhError as exc:
            if "404" not in str(exc):
                raise
        base_ref = self.gh.api_json(f"repos/{repo}/git/ref/heads/{default_branch}")
        self.gh.api_json(
            f"repos/{repo}/git/refs",
            method="POST",
            body={"ref": f"refs/heads/{ASSET_BRANCH}", "sha": base_ref["object"]["sha"]},
        )
        return self.gh.api_json(f"repos/{repo}/git/ref/heads/{ASSET_BRANCH}")

    @staticmethod
    def _result(repo: str, value: dict[str, Any], fallback_title: str) -> IssueResult:
        return IssueResult(
            repo=repo,
            number=int(value["number"]),
            url=str(value["html_url"]),
            title=str(value.get("title") or fallback_title),
        )


def _validate_image(content: bytes, mime_type: str) -> None:
    header = content[:16].hex()
    valid = {
        "image/jpeg": header.startswith("ffd8ff"),
        "image/png": header.startswith("89504e470d0a1a0a"),
        "image/gif": header.startswith(("474946383761", "474946383961")),
    }
    if not valid.get(mime_type, False):
        raise ValueError(f"Downloaded file is not valid {mime_type}.")
