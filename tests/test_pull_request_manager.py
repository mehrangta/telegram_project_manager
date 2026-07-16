import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from telegram_project_manager.bots.code_manager.progress import CodeProgressReporter
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


def pr(
    *,
    state="OPEN",
    head="checked-sha",
    base="main",
    merge_sha="",
    checks=(check(),),
    mergeable="MERGEABLE",
    merge_state_status="CLEAN",
):
    return PullRequestSnapshot(
        state=state,
        is_draft=False,
        base_branch=base,
        head_sha=head,
        mergeable=mergeable,
        merge_state_status=merge_state_status,
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
        self.merge_notified = []

    async def refresh(self, job_id, force=False):
        self.refreshed.append((job_id, force))

    async def notify_deployment(self, job_id):
        self.notified.append(job_id)

    async def notify_merge(self, job_id):
        self.merge_notified.append(job_id)


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
        self.db.set_repo_deploy_enabled("owner/repo", True)
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
        self.conflict_rebase_calls = []
        self.conflict_rebase_handler = None

        async def conflict_rebaser(job_id):
            self.conflict_rebase_calls.append(job_id)
            if self.conflict_rebase_handler:
                await self.conflict_rebase_handler(job_id)

        self.service = MergeDeploymentService(
            db=self.db,
            github=self.github,
            reporter=self.reporter,
            poll_seconds=0,
            merge_timeout_seconds=1,
            discovery_seconds=1,
            deploy_timeout_seconds=1,
            conflict_rebaser=conflict_rebaser,
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
            [
                {
                    "pr_url": "https://github.com/owner/repo/pull/42",
                    "head_sha": "checked-sha",
                    "commit_subject": "Fix #12: Issue (#42)",
                    "commit_body": "Closes #12",
                }
            ],
        )
        self.assertEqual(
            self.github.dispatches,
            [{"repo": "owner/repo", "workflow": "deploy.yml", "commit_sha": "merge-sha"}],
        )
        self.assertEqual(self.reporter.notified, ["c-abcdef12"])

    async def test_merge_only_accepts_checked_non_main_base_without_deploy_config(self):
        self.db.set_repo_deploy_enabled("owner/repo", False)
        self.db.set_repo_deploy_workflow("owner/repo", None)
        self.db.update_code_job("c-abcdef12", {"deployment_status": None})
        with self.db.session() as conn:
            conn.execute(
                "UPDATE code_jobs SET base_branch = 'develop' WHERE id = 'c-abcdef12'"
            )
        self.github.prs = [
            pr(base="develop"),
            pr(state="MERGED", base="develop", merge_sha="merge-sha"),
        ]

        response = await self.service.start_merge("c-abcdef12")
        self.assertIn("Merge queued", response)
        merged = await wait_for_deployment(self.db, "c-abcdef12", "merged")

        self.assertEqual(merged["deployment_mode"], "merge")
        self.assertEqual(merged["deployment_merge_sha"], "merge-sha")
        self.assertEqual(self.github.dispatches, [])
        self.assertEqual(self.reporter.merge_notified, ["c-abcdef12"])

    async def test_deploy_after_merge_only_dispatches_without_remerging(self):
        self.db.update_code_job(
            "c-abcdef12",
            {
                "deployment_mode": "merge",
                "deployment_status": "merged",
                "deployment_merge_sha": "existing-merge-sha",
            },
        )
        self.github.merges.clear()

        response = await self.service.start_deploy("c-abcdef12")
        self.assertIn("queued", response)
        deployed = await wait_for_deployment(self.db, "c-abcdef12", "succeeded")

        self.assertEqual(deployed["deployment_mode"], "deploy")
        self.assertEqual(self.github.merges, [])
        self.assertEqual(
            self.github.dispatches,
            [{"repo": "owner/repo", "workflow": "deploy.yml", "commit_sha": "existing-merge-sha"}],
        )

    async def test_deploy_rejects_non_main_job_even_after_merge(self):
        with self.db.session() as conn:
            conn.execute(
                """
                UPDATE code_jobs SET base_branch = 'develop', deployment_mode = 'merge',
                    deployment_status = 'merged', deployment_merge_sha = 'merge-sha'
                WHERE id = 'c-abcdef12'
                """
            )
        with self.assertRaisesRegex(DeploymentError, "targeting main"):
            await self.service.start_deploy("c-abcdef12")

    async def test_deploy_cannot_replace_active_merge_only_operation(self):
        self.db.update_code_job(
            "c-abcdef12",
            {"deployment_mode": "merge", "deployment_status": "merging"},
        )
        with self.assertRaisesRegex(DeploymentError, "merge-only operation"):
            await self.service.start_deploy("c-abcdef12")

    async def test_recovery_finishes_merge_only_without_deploying(self):
        self.db.update_code_job(
            "c-abcdef12",
            {"deployment_mode": "merge", "deployment_status": "merging"},
        )
        self.github.prs = [pr(state="MERGED", merge_sha="merge-sha")]

        await self.service.recover()
        merged = await wait_for_deployment(self.db, "c-abcdef12", "merged")

        self.assertEqual(merged["deployment_merge_sha"], "merge-sha")
        self.assertEqual(self.github.dispatches, [])

    async def test_changed_head_fails_without_merging(self):
        self.github.prs = [pr(head="new-sha")]
        await self.service.start("c-abcdef12")
        failed = await wait_for_deployment(self.db, "c-abcdef12", "failed")
        self.assertIn("head changed", failed["deployment_error"])
        self.assertEqual(self.github.merges, [])

    async def test_conflict_is_rebased_checked_and_merge_resumes(self):
        async def complete_rebase(job_id):
            self.db.update_code_job(
                job_id,
                {
                    "status": "ready",
                    "resume_phase": "checks",
                    "ci_head_sha": "rebased-sha",
                    "ci_checks_json": [check().to_json()],
                },
            )

        self.conflict_rebase_handler = complete_rebase
        self.github.prs = [
            pr(mergeable="CONFLICTING", merge_state_status="DIRTY"),
            pr(head="rebased-sha"),
            pr(state="MERGED", head="rebased-sha", merge_sha="merge-sha"),
        ]

        await self.service.start_merge("c-abcdef12")
        merged = await wait_for_deployment(self.db, "c-abcdef12", "merged")

        self.assertEqual(self.conflict_rebase_calls, ["c-abcdef12"])
        self.assertEqual(merged["deployment_conflict_attempts"], 1)
        self.assertEqual(merged["ci_head_sha"], "rebased-sha")
        self.assertEqual(self.github.merges[0]["head_sha"], "rebased-sha")

    async def test_conflict_recovery_resumes_deployment(self):
        async def complete_rebase(job_id):
            self.db.update_code_job(
                job_id,
                {
                    "status": "ready",
                    "resume_phase": "checks",
                    "ci_head_sha": "rebased-sha",
                    "ci_checks_json": [check().to_json()],
                },
            )

        self.conflict_rebase_handler = complete_rebase
        self.github.prs = [
            pr(mergeable="CONFLICTING", merge_state_status="DIRTY"),
            pr(head="rebased-sha"),
            pr(state="MERGED", head="rebased-sha", merge_sha="merge-sha"),
        ]

        await self.service.start_deploy("c-abcdef12")
        deployed = await wait_for_deployment(self.db, "c-abcdef12", "succeeded")

        self.assertEqual(deployed["deployment_conflict_attempts"], 1)
        self.assertEqual(
            self.github.dispatches,
            [{"repo": "owner/repo", "workflow": "deploy.yml", "commit_sha": "merge-sha"}],
        )

    async def test_conflict_recovery_failure_leaves_pr_open(self):
        async def fail_rebase(job_id):
            self.db.update_code_job(
                job_id,
                {
                    "status": "failed",
                    "resume_phase": "rebase",
                    "error": "sensitive conflict path blocked",
                },
            )

        self.conflict_rebase_handler = fail_rebase
        self.github.prs = [
            pr(mergeable="CONFLICTING", merge_state_status="DIRTY")
        ]

        await self.service.start_merge("c-abcdef12")
        failed = await wait_for_deployment(self.db, "c-abcdef12", "failed")

        self.assertIn("sensitive conflict path blocked", failed["deployment_error"])
        self.assertEqual(self.github.merges, [])
        self.assertEqual(self.reporter.merge_notified, ["c-abcdef12"])

    async def test_conflict_failure_notification_offers_admin_recovery(self):
        class Bot:
            def __init__(self):
                self.sent = []

            def send_message(self, chat_id, text, thread_id=None, **options):
                self.sent.append((chat_id, text, thread_id, options))
                return {"message_id": 1}

        self.db.update_code_job(
            "c-abcdef12",
            {
                "status": "failed",
                "resume_phase": "rebase",
                "error": "sensitive conflict path blocked",
                "deployment_mode": "merge",
                "deployment_status": "failed",
                "deployment_conflict_attempts": 1,
                "deployment_error": "Automatic conflict resolution failed",
            },
        )
        bot = Bot()
        reporter = CodeProgressReporter(self.db, bot, min_interval=0)

        await reporter.notify_merge("c-abcdef12")

        text = bot.sent[0][1]
        callbacks = [
            button["callback_data"]
            for row in bot.sent[0][3]["reply_markup"]["inline_keyboard"]
            for button in row
            if "callback_data" in button
        ]
        self.assertIn("Automatic conflict resolution could not complete", text)
        self.assertIn("command:/code retry c-abcdef12", callbacks)
        self.assertIn("confirm_merge:c-abcdef12", callbacks)

    async def test_unknown_mergeability_does_not_trigger_conflict_recovery(self):
        self.github.prs = [pr(mergeable="UNKNOWN")]

        await self.service.start_merge("c-abcdef12")
        failed = await wait_for_deployment(self.db, "c-abcdef12", "failed")

        self.assertIn("not mergeable: UNKNOWN", failed["deployment_error"])
        self.assertEqual(self.conflict_rebase_calls, [])

    async def test_conflict_recovery_stops_after_two_attempts(self):
        rebased_heads = iter(("rebased-one", "rebased-two"))

        async def complete_rebase(job_id):
            self.db.update_code_job(
                job_id,
                {
                    "status": "ready",
                    "resume_phase": "checks",
                    "ci_head_sha": next(rebased_heads),
                    "ci_checks_json": [check().to_json()],
                },
            )

        self.conflict_rebase_handler = complete_rebase
        self.github.prs = [
            pr(mergeable="CONFLICTING", merge_state_status="DIRTY"),
            pr(
                head="rebased-one",
                mergeable="CONFLICTING",
                merge_state_status="DIRTY",
            ),
            pr(
                head="rebased-two",
                mergeable="CONFLICTING",
                merge_state_status="DIRTY",
            ),
        ]

        await self.service.start_merge("c-abcdef12")
        failed = await wait_for_deployment(self.db, "c-abcdef12", "failed")

        self.assertEqual(len(self.conflict_rebase_calls), 2)
        self.assertEqual(failed["deployment_conflict_attempts"], 2)
        self.assertIn("stopped after 2 attempts", failed["deployment_error"])
        self.assertEqual(self.github.merges, [])

    async def test_recovery_resumes_operation_waiting_after_conflict_ci(self):
        self.db.update_code_job(
            "c-abcdef12",
            {
                "deployment_mode": "merge",
                "deployment_status": "resolving_conflicts",
                "deployment_conflict_attempts": 1,
            },
        )
        self.github.prs = [
            pr(),
            pr(state="MERGED", merge_sha="merge-sha"),
        ]

        await self.service.recover()
        merged = await wait_for_deployment(self.db, "c-abcdef12", "merged")

        self.assertEqual(merged["deployment_merge_sha"], "merge-sha")
        self.assertEqual(self.conflict_rebase_calls, [])

    async def test_already_merged_pr_still_requires_checked_head_identity(self):
        self.github.prs = [
            pr(state="MERGED", head="new-sha", merge_sha="untrusted-merge-sha")
        ]
        await self.service.start_merge("c-abcdef12")
        failed = await wait_for_deployment(self.db, "c-abcdef12", "failed")

        self.assertIn("head changed", failed["deployment_error"])
        self.assertIsNone(failed["deployment_merge_sha"])

    async def test_missing_workflow_refuses_to_start(self):
        self.db.set_repo_deploy_workflow("owner/repo", None)
        with self.assertRaises(DeploymentError):
            await self.service.start("c-abcdef12")
        self.assertIsNone(self.db.get_code_job("c-abcdef12")["deployment_status"])

    async def test_disabled_repo_refuses_to_start(self):
        self.db.set_repo_deploy_enabled("owner/repo", False)
        with self.assertRaisesRegex(DeploymentError, "disabled"):
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
    async def test_command_requires_admin_and_same_chat_and_topic(self):
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
                async def start_deploy(self, job_id):
                    return f"deployed {job_id}"

                async def start_merge(self, job_id):
                    return f"merged {job_id}"

            manager = PullRequestManager(db=db, service=Service())
            unauthorized = await manager.handle(IncomingMessage(10, 99, "user", "/deploy c-abcdef12"))
            self.assertIn("Unauthorized", unauthorized)
            db.upsert_user(99, "admin", "admin")
            wrong_chat = await manager.handle(IncomingMessage(11, 99, "admin", "/deploy c-abcdef12"))
            self.assertIn("different chat", wrong_chat)
            wrong_topic = await manager.handle(
                IncomingMessage(10, 99, "admin", "/deploy c-abcdef12", thread_id=30)
            )
            self.assertIn("different topic", wrong_topic)
            started = await manager.handle(IncomingMessage(10, 99, "admin", "/deploy c-abcdef12"))
            merged = await manager.handle(IncomingMessage(10, 99, "admin", "/merge c-abcdef12"))
            self.assertEqual(started, "deployed c-abcdef12")
            self.assertEqual(merged, "merged c-abcdef12")

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
                async def start_deploy(self, job_id):
                    return f"started {job_id}"

                async def start_merge(self, job_id):
                    return f"merged {job_id}"

            manager = PullRequestManager(db=db, service=Service())
            message = IncomingMessage(
                10,
                99,
                "admin",
                "/deploy",
                reply_to_code_job_id="c-abcdef12",
            )
            self.assertEqual(await manager.handle(message), "started c-abcdef12")

            merge_message = IncomingMessage(
                10, 99, "admin", "/merge", reply_to_code_job_id="c-abcdef12"
            )
            self.assertEqual(await manager.handle(merge_message), "merged c-abcdef12")


class DeploymentGitHubAdapterTests(unittest.TestCase):
    def test_squash_merge_uses_explicit_commit_message(self):
        class Runner:
            def __init__(self):
                self.args = None

            def run(self, args):
                self.args = args
                return GhResult(["gh", *args], 0, "", "", 10)

        runner = Runner()
        github = DeploymentGitHubService(runner)
        github.squash_merge(
            pr_url="https://github.com/owner/repo/pull/42",
            head_sha="checked-sha",
            commit_subject="Fix #12: Issue (#42)",
            commit_body="Closes #12",
        )

        self.assertEqual(
            runner.args,
            [
                "pr", "merge", "https://github.com/owner/repo/pull/42",
                "--squash", "--delete-branch", "--match-head-commit", "checked-sha",
                "--subject", "Fix #12: Issue (#42)",
                "--body", "Closes #12",
            ],
        )

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
