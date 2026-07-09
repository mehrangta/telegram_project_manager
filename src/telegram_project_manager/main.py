from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from telegram_project_manager.bots.commit_manager.commands import CommitManager
from telegram_project_manager.integrations.gh.commits import GhCommitExecutor
from telegram_project_manager.integrations.gh.runner import GhRunner
from telegram_project_manager.platform.llm.client import OpenAICompatibleClient
from telegram_project_manager.platform.router import TelegramRouter
from telegram_project_manager.platform.secrets import SecretStore
from telegram_project_manager.platform.storage.db import Database


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

    if args.command == "run":
        asyncio.run(run_bot(db))


async def run_bot(db: Database) -> None:
    try:
        from telethon import TelegramClient, events
    except ImportError as exc:
        raise SystemExit("Telethon is not installed. Run: uv sync") from exc

    secrets = SecretStore(Path("data/secrets.json"))
    api_id = secrets.require_int("TELEGRAM_API_ID")
    api_hash = secrets.require("TELEGRAM_API_HASH")
    bot_token = secrets.require("TELEGRAM_BOT_TOKEN")

    llm = OpenAICompatibleClient(db, secrets)
    gh = GhRunner()
    commit_executor = GhCommitExecutor(gh)
    manager = CommitManager(db=db, llm=llm, gh=gh, executor=commit_executor)
    router = TelegramRouter(db=db, handlers=[manager])

    client = TelegramClient("data/telegram_project_manager", api_id, api_hash)
    await client.start(bot_token=bot_token)
    me = await client.get_me()
    router.set_bot_username(getattr(me, "username", None) or "")

    @client.on(events.NewMessage)
    async def on_message(event):  # type: ignore[no-untyped-def]
        response = await router.handle_event(event)
        if response:
            await event.reply(response)

    logging.info("bot running as @%s", router.bot_username)
    await client.run_until_disconnected()

