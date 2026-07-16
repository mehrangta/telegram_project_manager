from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from telegram_project_manager.integrations.git.local_repository import (
    LocalRepositoryError,
    LocalRepositoryService,
    github_repo_from_remote,
)


class DoWorkspaceService:
    def __init__(self, *, repositories: LocalRepositoryService, root: Path) -> None:
        self.repositories = repositories
        self.root = root.resolve()

    def validate_source(self, *, source_path: str, repo: str) -> str:
        return str(self.repositories.validate(source_path, repo))

    def prepare(self, *, source_path: str, repo: str, branch: str) -> Path:
        source = self.repositories.validate(source_path, repo)
        destination = self.root / repo.lower().replace("/", "--")
        if destination.is_symlink():
            raise LocalRepositoryError(f"Do workspace is a symbolic link: {destination}")
        with self.repositories.lock_for(source):
            if not destination.exists():
                self._create(source=source, destination=destination, repo=repo, branch=branch)
            workspace = self.repositories.validate(destination, repo)
            self.repositories.fetch(workspace, branch)
        return workspace

    def _create(self, *, source: Path, destination: Path, repo: str, branch: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise LocalRepositoryError(f"Do workspace root is a symbolic link: {self.root}")
        origin = self.repositories.commands.run(
            ["git", "-C", str(source), "config", "--get", "remote.origin.url"]
        ).strip()
        if github_repo_from_remote(origin).lower() != repo.lower():
            raise LocalRepositoryError(f"Repository origin does not match {repo}.")
        temporary = self.root / f".{destination.name}.tmp-{uuid.uuid4().hex}"
        try:
            self.repositories.commands.run(
                [
                    "git", "clone", "--no-hardlinks", "--branch", branch,
                    str(source), str(temporary),
                ],
                timeout=900,
            )
            self.repositories.commands.run(
                ["git", "-C", str(temporary), "remote", "set-url", "origin", origin]
            )
            temporary.rename(destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
