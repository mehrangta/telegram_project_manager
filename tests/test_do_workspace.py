import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from telegram_project_manager.bots.do_manager.workspace import DoWorkspaceService
from telegram_project_manager.integrations.git.local_repository import LocalRepositoryService


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class DoWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        git("init", "-b", "main", str(self.source))
        git("config", "user.email", "test@example.com", cwd=self.source)
        git("config", "user.name", "Test", cwd=self.source)
        (self.source / "README.md").write_text("initial\n", encoding="utf-8")
        git("add", "README.md", cwd=self.source)
        git("commit", "-m", "initial", cwd=self.source)
        git("remote", "add", "origin", "git@github.com:owner/repo.git", cwd=self.source)
        self.repositories = LocalRepositoryService()
        self.service = DoWorkspaceService(
            repositories=self.repositories,
            root=self.root / "do-workspaces",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_workspace_is_reused_without_discarding_changes(self):
        with mock.patch.object(
            self.repositories, "fetch", side_effect=lambda path, branch: (Path(path), "sha")
        ):
            first = self.service.prepare(
                source_path=str(self.source), repo="owner/repo", branch="main"
            )
            (first / "unfinished.txt").write_text("keep me", encoding="utf-8")
            second = self.service.prepare(
                source_path=str(self.source), repo="owner/repo", branch="main"
            )
        self.assertEqual(first, second)
        self.assertEqual((second / "unfinished.txt").read_text(encoding="utf-8"), "keep me")
        origin = subprocess.run(
            ["git", "-C", str(second), "config", "--get", "remote.origin.url"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(origin, "git@github.com:owner/repo.git")

    def test_symlinked_workspace_is_rejected(self):
        destination = self.service.root / "owner--repo"
        destination.parent.mkdir(parents=True)
        try:
            destination.symlink_to(self.root / "outside", target_is_directory=True)
        except OSError:
            self.skipTest("symlinks require an unavailable Windows privilege")
        with self.assertRaisesRegex(Exception, "symbolic link"):
            self.service.prepare(
                source_path=str(self.source), repo="owner/repo", branch="main"
            )


if __name__ == "__main__":
    unittest.main()
