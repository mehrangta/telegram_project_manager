from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from telegram_project_manager.bots.ask_manager.commands import AskManager
from telegram_project_manager.bots.ask_manager.service import AskService
from telegram_project_manager.bots.commit_manager.commands import CommitManager
from telegram_project_manager.bots.commit_manager.repository_setup import RepositorySetupService
from telegram_project_manager.bots.code_manager.codex_sdk import CodexSdkAdapter
from telegram_project_manager.bots.code_manager.commands import CodeManager
from telegram_project_manager.bots.code_manager.progress import CodeProgressReporter
from telegram_project_manager.bots.code_manager.service import CodeJobService
from telegram_project_manager.bots.code_manager.workspace import CodeGitHubService, GitWorkspaceService
from telegram_project_manager.bots.do_manager.commands import DoManager
from telegram_project_manager.bots.do_manager.progress import DoProgressReporter
from telegram_project_manager.bots.do_manager.service import DoService
from telegram_project_manager.bots.do_manager.workspace import DoWorkspaceService
from telegram_project_manager.bots.issue_manager.commands import IssueManager
from telegram_project_manager.bots.issue_manager.executor import IssueExecutionService
from telegram_project_manager.bots.issue_manager.planner import IssuePlanner
from telegram_project_manager.bots.pull_request_manager.commands import PullRequestManager
from telegram_project_manager.bots.pull_request_manager.github import DeploymentGitHubService
from telegram_project_manager.bots.pull_request_manager.service import MergeDeploymentService
from telegram_project_manager.integrations.gh.commits import GhCommitExecutor
from telegram_project_manager.integrations.gh.issues import GhIssueExecutor, GhIssueReader
from telegram_project_manager.integrations.gh.repository_context import RepositoryContextService
from telegram_project_manager.integrations.git.local_repository import LocalRepositoryService
from telegram_project_manager.integrations.gh.runner import GhRunner
from telegram_project_manager.platform.config import (
    SECRET_CONFIG_KEYS,
    SUPPORTED_CONFIG_KEYS,
    normalize_config_value,
    resolve_codex_model,
)
from telegram_project_manager.platform.llm.client import OpenAICompatibleClient
from telegram_project_manager.platform.router import TelegramRouter
from telegram_project_manager.platform.secrets import SecretStore
from telegram_project_manager.platform.storage.db import Database
from telegram_project_manager.platform.telegram_bot import TelegramBotApi, run_polling


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="telegram-project-manager")
    parser.add_argument("--db", default="data/bot.db", help="SQLite DB path")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    sub.add_parser("run")
    sub.add_parser("run-do-worker")

    admin = sub.add_parser("admin")
    admin_sub = admin.add_subparsers(dest="admin_command", required=True)
    add = admin_sub.add_parser("add")
    add.add_argument("telegram_user_id", type=int)
    add.add_argument("--username", default="")
    remove = admin_sub.add_parser("remove")
    remove.add_argument("telegram_user_id", type=int)

    config = sub.add_parser("config")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("show")
    config_set = config_sub.add_parser("set")
    config_set.add_argument("key", choices=sorted(SUPPORTED_CONFIG_KEYS))
    config_set.add_argument("value")
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args()
    db = Database(Path(args.db))
    db.initialize()

    if args.command == "init-db":
        print(f"Initialized {db.path}")
        return

    if args.command == "admin":
        if args.admin_command == "add":
            db.upsert_user(args.telegram_user_id, args.username, "admin")
            print(f"Admin added: {args.telegram_user_id}")
            return
        if args.admin_command == "remove":
            db.remove_user(args.telegram_user_id)
            print(f"Admin removed: {args.telegram_user_id}")
            return

    if args.command == "config":
        if args.config_command == "show":
            settings = db.all_settings()
            print("Config:")
            for key, value in settings.items():
                print(f"- {key}={value}")
            for key in sorted(SECRET_CONFIG_KEYS):
                print(f"- {key}={'<set>' if db.has_secret(key) else '<not set>'}")
            return
        if args.config_command == "set":
            try:
                value = normalize_config_value(args.key, args.value)
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
            if args.key in SECRET_CONFIG_KEYS:
                db.set_secret(args.key, value)
            else:
                db.set_setting(args.key, value)
            print(f"Config set: {args.key}")
            return

    if args.command == "run":
        asyncio.run(run_bot(db))
    elif args.command == "run-do-worker":
        asyncio.run(run_do_worker(db))


async def run_bot(db: Database) -> None:
    secrets = SecretStore(Path("data/secrets.json"))
    bot_token = secrets.require("TELEGRAM_BOT_TOKEN")

    llm = OpenAICompatibleClient(db)
    gh = GhRunner()
    bot = TelegramBotApi(bot_token)
    commit_executor = GhCommitExecutor(gh)
    repositories = LocalRepositoryService()
    repository_setup = RepositorySetupService(
        db=db,
        gh=gh,
        repositories=repositories,
        bot=bot,
    )
    commit_manager = CommitManager(
        db=db,
        llm=llm,
        gh=gh,
        executor=commit_executor,
        repositories=repositories,
        repository_setup=repository_setup,
        issue_reader=GhIssueReader(gh),
    )
    issue_planner = IssuePlanner(db, llm, RepositoryContextService(repositories))
    issue_execution = IssueExecutionService(db, GhIssueExecutor(gh, bot))
    issue_manager = IssueManager(db, issue_planner, issue_execution)
    code_github = CodeGitHubService(gh)
    code_reporter = CodeProgressReporter(db, bot)
    codex = CodexSdkAdapter(
        lambda: db.get_secret("codex_api_key"),
        lambda: db.get_setting("codex_base_url", ""),
        lambda role: resolve_codex_model(db.get_setting, role),
    )
    workspaces = GitWorkspaceService(repositories=repositories)
    code_service = CodeJobService(
        db=db,
        codex=codex,
        workspaces=workspaces,
        github=code_github,
        reporter=code_reporter,
    )
    ask_service = AskService(
        db=db,
        codex=codex,
        workspaces=workspaces,
        bot=bot,
    )
    ask_manager = AskManager(db=db, service=ask_service)
    do_reporter = DoProgressReporter(db, bot)
    do_workspaces = DoWorkspaceService(
        repositories=repositories,
        root=db.path.parent / "do-workspaces",
    )
    do_service = DoService(
        db=db,
        codex=codex,
        bot=bot,
        reporter=do_reporter,
        workspaces=do_workspaces,
        host_working_directory=Path.cwd().resolve(),
        payload_root=db.path.parent / "do-payloads",
    )
    do_manager = DoManager(db=db, service=do_service)
    code_manager = CodeManager(
        db=db,
        service=code_service,
        github=code_github,
        reporter=code_reporter,
    )
    deployment_service = MergeDeploymentService(
        db=db,
        github=DeploymentGitHubService(gh),
        reporter=code_reporter,
        conflict_rebaser=code_service.rebase_for_operation,
    )
    pull_request_manager = PullRequestManager(db=db, service=deployment_service)
    router = TelegramRouter(
        db=db,
        handlers=[
            do_manager,
            ask_manager,
            issue_manager,
            code_manager,
            pull_request_manager,
            commit_manager,
        ],
    )
    await code_service.recover()
    await deployment_service.recover()
    try:
        await run_polling(bot, router)
    finally:
        await repository_setup.shutdown()
        await ask_service.shutdown()
        await deployment_service.shutdown()
        await code_service.shutdown()


async def run_do_worker(db: Database) -> None:
    secrets = SecretStore(Path("data/secrets.json"))
    bot = TelegramBotApi(secrets.require("TELEGRAM_BOT_TOKEN"))
    repositories = LocalRepositoryService()
    codex = CodexSdkAdapter(
        lambda: db.get_secret("codex_api_key"),
        lambda: db.get_setting("codex_base_url", ""),
        lambda role: resolve_codex_model(db.get_setting, role),
    )
    reporter = DoProgressReporter(db, bot)
    service = DoService(
        db=db,
        codex=codex,
        bot=bot,
        reporter=reporter,
        workspaces=DoWorkspaceService(
            repositories=repositories,
            root=db.path.parent / "do-workspaces",
        ),
        host_working_directory=Path.cwd().resolve(),
        payload_root=db.path.parent / "do-payloads",
    )
    try:
        await service.run_worker()
    finally:
        await codex.close()
