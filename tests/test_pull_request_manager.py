import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from telegram_project_manager.bots.code_manager.workspace import PullRequestCheck
from telegram_project_manager.bots.pull_request_manager.commands import PullRequestManager
from telegram_project_manager.bots.pull_request_manager.github import (
    DeploymentGitHubService,
    PullRequestSnapshot,
    WorkflowRun,
)
from telegram_project_manager.integrations.gh.runner import GhResult
from telegram_project_manager.bots.pull_request_manager.service import (
    DeploymentError,
    MergeDeploymentService,
)
from telegram_project_manager.platform.router import IncomingMessage
from telegram_project_manager.platform.storage.db import Database


def check(bucket="pass", state="success"):
    return PullRequestCheck("CI", state, bucket, "", "CI", "")


def pr(*, state="OPEN", head="checked-sha", merge_sha="", checks=(check(),)):
    return PullRequestSnapshot(
        state=state,
        is_draft=False,
        base_branch="main",
        head_sha=head,
        mergeable="MERGEABLE",
        merge_state_status="CLEAN",
        review_decision="APPROVED",
        merged_at="2026-01-01T00:00:00Z" if state == "MERGED" else "",
        merge_sha=merge_sha,
        checks=checks,
    )


class FakeGitHub:
    def __init__(self):
        self.prs = [pr(), pr(state="MERGED", merge_sha="merge-sha")]
        self.merges = []
        self.dispatches = []
        self.workflow = WorkflowRun(91, "completed", "success", "https://run/91", "merge-sha", "Deploy")

    def get_pr(self, url):
        if len(self.prs) > 1:
            return self.prs.pop(0)
        return self.prs[0]

    def squash_merge(self, **kwargs):
        self.merges.append(kwargs)

    def dispatch_workflow(self, **kwargs):
        self.dispatches.append(kwargs)
        return self.workflow

    def find_dispatched_workflow_run(self, **kwargs):
        return self.workflow

    def get_workflow_run(self, **kwargs):
        return self.workflow


class FakeReporter:
    def __init__(self):
        self.refreshed = []
        self.notified = []

    async def refresh(self, job_id, force=False):
        self.refreshed.append((job_id, force))

    async def notify_deployment(self, job_id):
        self.notified.append(job_id)


async def wait_for_deployment(db, job_id, expected):
    for _ in range(200):
        job = db.get_code_job(job_id)
        if job and job.get("deployment_status") == expected:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError(f"deployment did not reach {expected}")


class MergeDeploymentServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "bot.db")
        self.db.initialize()
        self.db.allow_repo("owner/repo", 1)
        self.db.set_repo_deploy_workflow("owner/repo", "deploy.yml")
        self.db.create_code_job(
            {
                "id": "c-abcdef12",
                "telegram_chat_id": 10,
                "telegram_user_id": 20,
                "telegram_thread_id": 30,
                "repo": "owner/repo",
                "issue_number": 12,
                "issue_title": "Issue",
                "issue_url": "https://github.com/owner/repo/issues/12",
                "issue_context_json": {},
                "base_branch": "main",
                "target_branch": "codex/issue-12-c-abcdef12",
                "workspace_path": "/tmp/job",
                "source_repo_path": "/tmp/repo.git",
                "status": "ready",
                "resume_phase": "checks",
                "skip_plan": True,
            }
        )
        self.db.update_code_job(
            "c-abcdef12",
            {
                "pull_request_number": 42,
                "pull_request_url": "https://github.com/owner/repo/pull/42",
                "ci_head_sha": "checked-sha",
                "ci_checks_json": [check().to_json()],
            },
        )
        self.github = FakeGitHub()
        self.reporter = FakeReporter()
        self.service = MergeDeploymentService(
            db=self.db,
            github=self.github,
            reporter=self.reporter,
            poll_seconds=0,
            merge_timeout_seconds=1,
            discovery_seconds=1,
            deploy_timeout_seconds=1,
        )

    async def asyncTearDown(self):
        await self.service.shutdown()
        self.temp.cleanup()

    async def test_ready_job_is_squash_merged_and_deployed(self):
        response = await self.service.start("c-abcdef12")
        self.assertIn("queued", response)
        deployed = await wait_for_deployment(self.db, "c-abcdef12", "succeeded")
        self.assertEqual(deployed["deployment_merge_sha"], "merge-sha")
        self.assertEqual(deployed["deployment_run_id"], 91)
        self.assertEqual(deployed["deployment_run_url"], "https://run/91")
        self.assertEqual(
            self.github.merges,
            [{"pr_url": "https://github.com/owner/repo/pull/42", "head_sha": "checked-sha"}],
        )
        self.assertEqual(
            self.github.dispatches,
            [{"repo": "owner/repo", "workflow": "deploy.yml", "commit_sha": "merge-sha"}],
        )
        self.assertEqual(self.reporter.notified, ["c-abcdef12"])

    async def test_changed_head_fails_without_merging(self):
        self.github.prs = [pr(head="new-sha")]
        await self.service.start("c-abcdef12")
        failed = await wait_for_deployment(self.db, "c-abcdef12", "failed")
        self.assertIn("head changed", failed["deployment_error"])
        self.assertEqual(self.github.merges, [])

    async def test_missing_workflow_refuses_to_start(self):
        self.db.set_repo_deploy_workflow("owner/repo", None)
        with self.assertRaises(DeploymentError):
            await self.service.start("c-abcdef12")
        self.assertIsNone(self.db.get_code_job("c-abcdef12")["deployment_status"])

    async def test_missing_workflow_run_reports_merge_succeeded(self):
        self.github.dispatch_workflow = lambda **kwargs: None
        self.github.find_dispatched_workflow_run = lambda **kwargs: None
        self.service.discovery_seconds = 0
        await self.service.start("c-abcdef12")
        failed = await wait_for_deployment(self.db, "c-abcdef12", "failed")
        self.assertEqual(failed["deployment_merge_sha"], "merge-sha")
        self.assertIn("did not start", failed["deployment_error"])

    async def test_failed_workflow_preserves_run_link_and_allows_retry(self):
        self.github.workflow = WorkflowRun(
            91, "completed", "failure", "https://run/91", "merge-sha", "Deploy"
        )
        await self.service.start("c-abcdef12")
        failed = await wait_for_deployment(self.db, "c-abcdef12", "failed")
        self.assertEqual(failed["deployment_run_url"], "https://run/91")
        self.assertIn("failure", failed["deployment_error"])

        self.github.workflow = WorkflowRun(
            92, "completed", "success", "https://run/92", "merge-sha", "Deploy"
        )
        retry = await self.service.start("c-abcdef12")
        self.assertIn("queued", retry)
        deployed = await wait_for_deployment(self.db, "c-abcdef12", "succeeded")
        self.assertEqual(deployed["deployment_run_id"], 92)

    async def test_recovery_resumes_waiting_workflow(self):
        self.db.update_code_job(
            "c-abcdef12",
            {
                "deployment_status": "waiting_workflow",
                "deployment_merge_sha": "merge-sha",
                "deployment_started_at": int(time.time()),
            },
        )
        await self.service.recover()
        deployed = await wait_for_deployment(self.db, "c-abcdef12", "succeeded")
        self.assertEqual(deployed["deployment_run_id"], 91)
        self.assertEqual(self.github.merges, [])

    async def test_recovery_from_dispatching_discovers_run_without_redispatch(self):
        self.db.update_code_job(
            "c-abcdef12",
            {
                "deployment_status": "dispatching",
                "deployment_merge_sha": "merge-sha",
                "deployment_started_at": int(time.time()),
            },
        )
        await self.service.recover()
        deployed = await wait_for_deployment(self.db, "c-abcdef12", "succeeded")
        self.assertEqual(deployed["deployment_run_id"], 91)
        self.assertEqual(self.github.dispatches, [])


class PullRequestCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_requires_admin_and_same_chat(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.create_code_job(
                {
                    "id": "c-abcdef12", "telegram_chat_id": 10, "telegram_user_id": 20,
                    "telegram_thread_id": None, "repo": "owner/repo", "issue_number": 1,
                    "issue_title": "Issue", "issue_url": "url", "issue_context_json": {},
                    "base_branch": "main", "target_branch": "branch", "workspace_path": "/tmp/job",
                    "source_repo_path": "/tmp/repo", "status": "ready", "resume_phase": "checks",
                    "skip_plan": True,
                }
            )

            class Service:
                async def start(self, job_id):
                    return f"started {job_id}"

            manager = PullRequestManager(db=db, service=Service())
            unauthorized = await manager.handle(IncomingMessage(10, 99, "user", "/deploy c-abcdef12"))
            self.assertIn("Unauthorized", unauthorized)
            db.upsert_user(99, "admin", "admin")
            wrong_chat = await manager.handle(IncomingMessage(11, 99, "admin", "/deploy c-abcdef12"))
            self.assertIn("different chat", wrong_chat)
            started = await manager.handle(IncomingMessage(10, 99, "admin", "/deploy c-abcdef12"))
            self.assertEqual(started, "started c-abcdef12")

    async def test_command_uses_replied_code_job_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "bot.db")
            db.initialize()
            db.upsert_user(99, "admin", "admin")
            db.create_code_job(
                {
                    "id": "c-abcdef12", "telegram_chat_id": 10, "telegram_user_id": 20,
                    "telegram_thread_id": None, "repo": "owner/repo", "issue_number": 1,
                    "issue_title": "Issue", "issue_url": "url", "issue_context_json": {},
                    "base_branch": "main", "target_branch": "branch", "workspace_path": "/tmp/job",
                    "source_repo_path": "/tmp/repo", "status": "ready", "resume_phase": "checks",
                    "skip_plan": True,
                }
            )

            class Service:
                async def start(self, job_id):
                    return f"started {job_id}"

            manager = PullRequestManager(db=db, service=Service())
            message = IncomingMessage(
                10,
                99,
                "admin",
                "/deploy",
                reply_to_code_job_id="c-abcdef12",
            )
            self.assertEqual(await manager.handle(message), "started c-abcdef12")


class DeploymentGitHubAdapterTests(unittest.TestCase):
    def test_dispatch_passes_merge_sha_and_parses_created_run_url(self):
        class Runner:
            def __init__(self):
                self.args = None

            def run(self, args):
                self.args = args
                return GhResult(
                    ["gh", *args],
                    0,
                    "https://github.com/owner/repo/actions/runs/12345\n",
                    "",
                    10,
                )

        runner = Runner()
        github = DeploymentGitHubService(runner)
        run = github.dispatch_workflow(
            repo="owner/repo", workflow="deploy.yml", commit_sha="merge-sha"
        )
        self.assertEqual(
            runner.args,
            [
                "workflow", "run", "deploy.yml", "--repo", "owner/repo",
                "--ref", "main", "--raw-field", "ref=merge-sha",
            ],
        )
        assert run is not None
        self.assertEqual(run.run_id, 12345)
        self.assertEqual(run.url, "https://github.com/owner/repo/actions/runs/12345")


if __name__ == "__main__":
    unittest.main()
