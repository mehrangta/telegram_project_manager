from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from telegram_project_manager.bots.commit_manager.commands import CommitManager
from telegram_project_manager.integrations.gh.commits import GhCommitExecutor
from telegram_project_manager.integrations.gh.runner import GhRunner
from telegram_project_manager.platform.config import SUPPORTED_CONFIG_KEYS, normalize_config_value
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
            if not settings:
                print("Config: no settings stored.")
                return
            print("Config:")
            for key, value in settings.items():
                print(f"- {key}={value}")
            return
        if args.config_command == "set":
            try:
                value = normalize_config_value(args.key, args.value)
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
            db.set_setting(args.key, value)
            print(f"Config set: {args.key}")
            return

    if args.command == "run":
        asyncio.run(run_bot(db))


async def run_bot(db: Database) -> None:
    secrets = SecretStore(Path("data/secrets.json"))
    bot_token = secrets.require("TELEGRAM_BOT_TOKEN")

    llm = OpenAICompatibleClient(db, secrets)
    gh = GhRunner()
    commit_executor = GhCommitExecutor(gh)
    manager = CommitManager(db=db, llm=llm, gh=gh, executor=commit_executor)
    router = TelegramRouter(db=db, handlers=[manager])
    await run_polling(TelegramBotApi(bot_token), router)
