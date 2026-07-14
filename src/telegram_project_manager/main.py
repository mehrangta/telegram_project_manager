from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from telegram_project_manager.bots.commit_manager.commands import CommitManager
from telegram_project_manager.bots.issue_manager.commands import IssueManager
from telegram_project_manager.bots.issue_manager.executor import IssueExecutionService
from telegram_project_manager.bots.issue_manager.planner import IssuePlanner
from telegram_project_manager.integrations.gh.commits import GhCommitExecutor
from telegram_project_manager.integrations.gh.issues import GhIssueExecutor
from telegram_project_manager.integrations.gh.runner import GhRunner
from telegram_project_manager.platform.config import SECRET_CONFIG_KEYS, SUPPORTED_CONFIG_KEYS, normalize_config_value
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
            print(f"- openai_api_key={'<set>' if db.has_secret('openai_api_key') else '<not set>'}")
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


async def run_bot(db: Database) -> None:
    secrets = SecretStore(Path("data/secrets.json"))
    bot_token = secrets.require("TELEGRAM_BOT_TOKEN")

    llm = OpenAICompatibleClient(db)
    gh = GhRunner()
    bot = TelegramBotApi(bot_token)
    commit_executor = GhCommitExecutor(gh)
    commit_manager = CommitManager(db=db, llm=llm, gh=gh, executor=commit_executor)
    issue_planner = IssuePlanner(db, llm)
    issue_execution = IssueExecutionService(db, GhIssueExecutor(gh, bot))
    issue_manager = IssueManager(db, issue_planner, issue_execution)
    router = TelegramRouter(db=db, handlers=[issue_manager, commit_manager])
    await run_polling(bot, router)
