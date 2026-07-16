import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from telegram_project_manager.bots.code_manager.workspace import GitWorkspaceService
from telegram_project_manager.integrations.git.local_repository import (
    LocalRepositoryError,
    LocalRepositoryService,
    github_repo_from_remote,
)
from telegram_project_manager.platform.storage.db import Database


def git(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed.stdout.strip()


class LocalRepositoryTests(unittest.TestCase):
    def test_normalizes_supported_github_remotes(self):
        self.assertEqual(
            github_repo_from_remote("https://github.com/Owner/Repo.git"),
            "Owner/Repo",
        )
        self.assertEqual(github_repo_from_remote("git@github.com:Owner/Repo.git"), "Owner/Repo")

    def test_fetches_and_uses_isolated_worktree_without_clone(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote = root / "remote.git"
            seed = root / "seed"
            cache = root / "cache.git"
            workspace = root / "jobs" / "c-test" / "repo"
            git("init", "--bare", str(remote))
            git("init", str(seed))
            git("config", "user.name", "Test", cwd=seed)
            git("config", "user.email", "test@example.test", cwd=seed)
            (seed / "README.md").write_text("hello\n", encoding="utf-8")
            git("add", "README.md", cwd=seed)
            git("commit", "-m", "initial", cwd=seed)
            git("branch", "-M", "main", cwd=seed)
            git("remote", "add", "origin", str(remote), cwd=seed)
            git("push", "-u", "origin", "main", cwd=seed)
            git("clone", "--bare", str(remote), str(cache))
            github_url = "https://github.com/owner/repo.git"
            git("remote", "set-url", "origin", github_url, cwd=cache)
            git("config", f"url.{remote.as_uri()}.insteadOf", github_url, cwd=cache)

            repositories = LocalRepositoryService()
            workspaces = GitWorkspaceService(repositories=repositories)
            resolved = workspaces.validate_source(source_path=str(cache), repo="owner/repo")
            base_sha = workspaces.prepare(
                source_path=resolved,
                repo="owner/repo",
                base_branch="main",
                target_branch="codex/issue-1-c-test",
                path=workspace,
            )

            self.assertTrue((workspace / ".git").is_file())
            self.assertEqual((workspace / "README.md").read_text(encoding="utf-8"), "hello\n")
            self.assertEqual(git("rev-parse", "HEAD", cwd=workspace), base_sha)
            workspaces.cleanup(
                source_path=resolved,
                path=workspace,
                target_branch="codex/issue-1-c-test",
            )
            self.assertFalse(workspace.exists())

    def test_rejects_origin_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo.git"
            git("init", "--bare", str(repo))
            git("remote", "add", "origin", "https://github.com/other/repo.git", cwd=repo)
            with self.assertRaisesRegex(LocalRepositoryError, "expected owner/repo"):
                LocalRepositoryService().validate(repo, "owner/repo")

    def test_pushes_rebased_branch_when_remote_tracking_ref_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote = root / "remote.git"
            seed = root / "seed"
            cache = root / "cache.git"
            workspace = root / "jobs" / "c-test" / "repo"
            branch = "codex/issue-1-c-test"
            git("init", "--bare", str(remote))
            git("init", str(seed))
            git("config", "user.name", "Test", cwd=seed)
            git("config", "user.email", "test@example.test", cwd=seed)
            (seed / "README.md").write_text("hello\n", encoding="utf-8")
            git("add", "README.md", cwd=seed)
            git("commit", "-m", "initial", cwd=seed)
            git("branch", "-M", "main", cwd=seed)
            git("remote", "add", "origin", str(remote), cwd=seed)
            git("push", "-u", "origin", "main", cwd=seed)
            git("clone", "--bare", str(remote), str(cache))
            github_url = "https://github.com/owner/repo.git"
            git("remote", "set-url", "origin", github_url, cwd=cache)
            git("config", f"url.{remote.as_uri()}.insteadOf", github_url, cwd=cache)

            workspaces = GitWorkspaceService(repositories=LocalRepositoryService())
            workspaces.prepare(
                source_path=str(cache),
                repo="owner/repo",
                base_branch="main",
                target_branch=branch,
                path=workspace,
            )
            workspaces.commit_plan(
                path=workspace,
                plan_path=".codex/plan.md",
                markdown="plan\n",
                message="add plan",
                first_push=True,
            )
            git("update-ref", "-d", f"refs/remotes/origin/{branch}", cwd=cache)

            (seed / "README.md").write_text("hello again\n", encoding="utf-8")
            git("add", "README.md", cwd=seed)
            git("commit", "-m", "update main", cwd=seed)
            git("push", "origin", "main", cwd=seed)

            workspaces.refresh_base(workspace, "main")
            workspaces.push_rebased_branch(workspace)

            self.assertEqual(
                git("rev-parse", "HEAD", cwd=workspace),
                git("rev-parse", f"refs/heads/{branch}", cwd=remote),
            )


class LocalRepositoryMigrationTests(unittest.TestCase):
    def test_initialize_adds_cache_columns_to_existing_tables(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            path = Path(temp_dir) / "bot.db"
            db = Database(path)
            db.initialize()
            with sqlite3.connect(path) as conn:
                conn.execute("ALTER TABLE chat_settings DROP COLUMN local_repo_path")
                conn.execute("ALTER TABLE code_jobs DROP COLUMN source_repo_path")
            db.initialize()
            with db.session() as conn:
                chat_columns = {row["name"] for row in conn.execute("PRAGMA table_info(chat_settings)")}
                job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(code_jobs)")}
            self.assertIn("local_repo_path", chat_columns)
            self.assertIn("source_repo_path", job_columns)


if __name__ == "__main__":
    unittest.main()
