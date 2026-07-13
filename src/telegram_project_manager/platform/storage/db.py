from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator
from typing import Any


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.session() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_user_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_settings (
                    telegram_chat_id INTEGER PRIMARY KEY,
                    active_repo TEXT,
                    default_branch TEXT NOT NULL DEFAULT 'main',
                    updated_by_user_id INTEGER,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS allowed_repos (
                    repo TEXT PRIMARY KEY,
                    added_by_user_id INTEGER,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY,
                    telegram_chat_id INTEGER NOT NULL,
                    telegram_user_id INTEGER NOT NULL,
                    repo TEXT NOT NULL,
                    base_branch TEXT NOT NULL,
                    target_branch TEXT NOT NULL,
                    request_text TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id TEXT,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS llm_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_llm_messages_session_id_id
                ON llm_messages (session_id, id);
                """
            )

    def upsert_user(self, telegram_user_id: int, username: str, role: str) -> None:
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO users (telegram_user_id, username, role, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    username = excluded.username,
                    role = excluded.role
                """,
                (telegram_user_id, username or "", role, now),
            )

    def remove_user(self, telegram_user_id: int) -> None:
        with self.session() as conn:
            conn.execute("DELETE FROM users WHERE telegram_user_id = ?", (telegram_user_id,))

    def get_user(self, telegram_user_id: int) -> dict[str, Any] | None:
        with self.session() as conn:
            row = conn.execute("SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,)).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        with self.session() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY telegram_user_id").fetchall()
        return [dict(row) for row in rows]

    def set_setting(self, key: str, value: str) -> None:
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now),
            )

    def get_setting(self, key: str, default: str = "") -> str:
        with self.session() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def all_settings(self) -> dict[str, str]:
        with self.session() as conn:
            rows = conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def list_llm_messages(self, session_id: str, limit: int) -> list[dict[str, str]]:
        with self.session() as conn:
            rows = conn.execute(
                """
                SELECT role, content
                FROM (
                    SELECT id, role, content
                    FROM llm_messages
                    WHERE session_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id
                """,
                (session_id, limit),
            ).fetchall()
        return [{"role": str(row["role"]), "content": str(row["content"])} for row in rows]

    def add_llm_messages(self, session_id: str, messages: list[tuple[str, str]], limit: int) -> None:
        if not messages:
            return
        now = int(time.time())
        with self.session() as conn:
            conn.executemany(
                "INSERT INTO llm_messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                [(session_id, role, content, now) for role, content in messages],
            )
            conn.execute(
                """
                DELETE FROM llm_messages
                WHERE session_id = ?
                  AND id NOT IN (
                      SELECT id
                      FROM llm_messages
                      WHERE session_id = ?
                      ORDER BY id DESC
                      LIMIT ?
                  )
                """,
                (session_id, session_id, limit),
            )

    def clear_llm_messages(self, session_id: str) -> None:
        with self.session() as conn:
            conn.execute("DELETE FROM llm_messages WHERE session_id = ?", (session_id,))

    def count_llm_messages(self, session_id: str) -> int:
        with self.session() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM llm_messages WHERE session_id = ?", (session_id,)
            ).fetchone()
        return int(row["count"]) if row else 0

    def set_chat_repo(self, chat_id: int, repo: str | None, user_id: int, default_branch: str = "main") -> None:
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO chat_settings (telegram_chat_id, active_repo, default_branch, updated_by_user_id, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(telegram_chat_id) DO UPDATE SET
                    active_repo = excluded.active_repo,
                    default_branch = excluded.default_branch,
                    updated_by_user_id = excluded.updated_by_user_id,
                    updated_at = excluded.updated_at
                """,
                (chat_id, repo, default_branch, user_id, now),
            )

    def get_chat_settings(self, chat_id: int) -> dict[str, Any]:
        with self.session() as conn:
            row = conn.execute("SELECT * FROM chat_settings WHERE telegram_chat_id = ?", (chat_id,)).fetchone()
        if row:
            return dict(row)
        return {"telegram_chat_id": chat_id, "active_repo": None, "default_branch": "main"}

    def allow_repo(self, repo: str, user_id: int) -> None:
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO allowed_repos (repo, added_by_user_id, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(repo) DO NOTHING
                """,
                (repo, user_id, now),
            )

    def disallow_repo(self, repo: str) -> None:
        with self.session() as conn:
            conn.execute("DELETE FROM allowed_repos WHERE repo = ?", (repo,))

    def is_repo_allowed(self, repo: str) -> bool:
        with self.session() as conn:
            row = conn.execute("SELECT 1 FROM allowed_repos WHERE repo = ?", (repo,)).fetchone()
        return bool(row)

    def list_allowed_repos(self) -> list[str]:
        with self.session() as conn:
            rows = conn.execute("SELECT repo FROM allowed_repos ORDER BY repo").fetchall()
        return [str(row["repo"]) for row in rows]

    def create_plan(self, plan: dict[str, Any]) -> None:
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO plans (
                    id, telegram_chat_id, telegram_user_id, repo, base_branch, target_branch,
                    request_text, plan_json, status, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan["id"],
                    plan["telegram_chat_id"],
                    plan["telegram_user_id"],
                    plan["repo"],
                    plan["base_branch"],
                    plan["target_branch"],
                    plan["request_text"],
                    json.dumps(plan["plan_json"], separators=(",", ":")),
                    plan["status"],
                    plan["created_at"],
                    plan["expires_at"],
                ),
            )

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        with self.session() as conn:
            row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if not row:
            return None
        plan = dict(row)
        plan["plan_json"] = json.loads(str(plan["plan_json"]))
        return plan

    def update_plan_status(self, plan_id: str, status: str) -> None:
        with self.session() as conn:
            conn.execute("UPDATE plans SET status = ? WHERE id = ?", (status, plan_id))

    def audit(self, action: str, status: str, details: dict[str, Any], plan_id: str | None = None) -> None:
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO audit_events (plan_id, action, status, details_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (plan_id, action, status, json.dumps(details, separators=(",", ":")), now),
            )
