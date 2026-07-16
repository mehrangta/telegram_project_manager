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
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.session() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
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
                    local_repo_path TEXT,
                    updated_by_user_id INTEGER,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS topic_settings (
                    telegram_chat_id INTEGER NOT NULL,
                    telegram_thread_id INTEGER NOT NULL,
                    active_repo TEXT,
                    default_branch TEXT NOT NULL DEFAULT 'main',
                    local_repo_path TEXT,
                    updated_by_user_id INTEGER,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (telegram_chat_id, telegram_thread_id)
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS secrets (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS allowed_repos (
                    repo TEXT PRIMARY KEY,
                    deploy_workflow TEXT,
                    deploy_enabled INTEGER NOT NULL DEFAULT 0,
                    added_by_user_id INTEGER,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY,
                    telegram_chat_id INTEGER NOT NULL,
                    telegram_thread_id INTEGER,
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

                CREATE TABLE IF NOT EXISTS issue_drafts (
                    id TEXT PRIMARY KEY,
                    telegram_chat_id INTEGER NOT NULL,
                    telegram_thread_id INTEGER,
                    telegram_user_id INTEGER NOT NULL,
                    repo TEXT NOT NULL,
                    default_branch TEXT NOT NULL,
                    local_repo_path TEXT NOT NULL DEFAULT '',
                    request_text TEXT NOT NULL,
                    issue_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    github_issue_number INTEGER,
                    github_issue_url TEXT,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS issue_attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    draft_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    telegram_file_id TEXT NOT NULL,
                    telegram_file_unique_id TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    asset_path TEXT,
                    FOREIGN KEY(draft_id) REFERENCES issue_drafts(id) ON DELETE CASCADE,
                    UNIQUE(draft_id, position)
                );

                CREATE TABLE IF NOT EXISTS issue_draft_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    draft_id TEXT NOT NULL,
                    revision_number INTEGER NOT NULL,
                    feedback_text TEXT NOT NULL,
                    issue_json TEXT NOT NULL,
                    added_attachments_json TEXT NOT NULL,
                    telegram_user_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(draft_id) REFERENCES issue_drafts(id) ON DELETE CASCADE,
                    UNIQUE(draft_id, revision_number)
                );

                CREATE INDEX IF NOT EXISTS idx_issue_draft_revisions_draft_number
                ON issue_draft_revisions (draft_id, revision_number);

                CREATE TABLE IF NOT EXISTS code_jobs (
                    id TEXT PRIMARY KEY,
                    telegram_chat_id INTEGER NOT NULL,
                    telegram_user_id INTEGER NOT NULL,
                    telegram_thread_id INTEGER,
                    telegram_message_id INTEGER,
                    telegram_plan_message_id INTEGER,
                    repo TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    issue_title TEXT NOT NULL,
                    issue_url TEXT NOT NULL,
                    issue_context_json TEXT NOT NULL,
                    base_branch TEXT NOT NULL,
                    base_sha TEXT,
                    target_branch TEXT NOT NULL,
                    workspace_path TEXT NOT NULL,
                    source_repo_path TEXT,
                    status TEXT NOT NULL,
                    resume_phase TEXT NOT NULL,
                    skip_plan INTEGER NOT NULL DEFAULT 0,
                    plan_json TEXT,
                    plan_revision INTEGER NOT NULL DEFAULT 0,
                    feedback_json TEXT NOT NULL DEFAULT '[]',
                    codex_thread_id TEXT,
                    pull_request_number INTEGER,
                    pull_request_url TEXT,
                    github_plan_question_comment_id INTEGER,
                    github_plan_question_revision INTEGER NOT NULL DEFAULT 0,
                    github_plan_comment_cursor INTEGER NOT NULL DEFAULT 0,
                    latest_activity TEXT NOT NULL DEFAULT '',
                    result_json TEXT,
                    ci_head_sha TEXT,
                    ci_wait_started_at INTEGER,
                    ci_repair_attempts INTEGER NOT NULL DEFAULT 0,
                    ci_checks_json TEXT,
                    deployment_mode TEXT,
                    deployment_status TEXT,
                    deployment_conflict_attempts INTEGER NOT NULL DEFAULT 0,
                    deployment_merge_sha TEXT,
                    deployment_run_id INTEGER,
                    deployment_run_url TEXT,
                    deployment_started_at INTEGER,
                    deployment_error TEXT,
                    error TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_code_jobs_active_issue
                ON code_jobs (repo, issue_number)
                WHERE status NOT IN ('ready', 'discarded');

                CREATE INDEX IF NOT EXISTS idx_code_jobs_status_updated
                ON code_jobs (status, updated_at);

                CREATE TABLE IF NOT EXISTS code_job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES code_jobs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_code_job_events_job_id_id
                ON code_job_events (job_id, id);

                CREATE TABLE IF NOT EXISTS code_plan_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    author TEXT NOT NULL,
                    body TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    applied_revision INTEGER,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES code_jobs(id) ON DELETE CASCADE,
                    UNIQUE(source, source_id)
                );

                CREATE INDEX IF NOT EXISTS idx_code_plan_feedback_job_state
                ON code_plan_feedback (job_id, state, id);

                CREATE TABLE IF NOT EXISTS do_jobs (
                    id TEXT PRIMARY KEY,
                    telegram_chat_id INTEGER NOT NULL,
                    telegram_user_id INTEGER NOT NULL,
                    telegram_thread_id INTEGER,
                    telegram_message_id INTEGER,
                    mode TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    repo TEXT,
                    default_branch TEXT,
                    source_repo_path TEXT,
                    workspace_path TEXT,
                    payload_path TEXT NOT NULL,
                    image_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    latest_activity TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_do_jobs_status_created
                ON do_jobs (status, created_at);

                CREATE INDEX IF NOT EXISTS idx_do_jobs_scope_updated
                ON do_jobs (telegram_chat_id, telegram_thread_id, updated_at);

                CREATE TABLE IF NOT EXISTS do_job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES do_jobs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_do_job_events_job_id_id
                ON do_job_events (job_id, id);
                """
            )
            self._ensure_column(conn, "chat_settings", "local_repo_path", "TEXT")
            self._ensure_column(conn, "plans", "telegram_thread_id", "INTEGER")
            self._ensure_column(conn, "issue_drafts", "telegram_thread_id", "INTEGER")
            self._ensure_column(
                conn, "issue_drafts", "local_repo_path", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(conn, "allowed_repos", "deploy_workflow", "TEXT")
            self._ensure_column(
                conn, "allowed_repos", "deploy_enabled", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(conn, "code_jobs", "source_repo_path", "TEXT")
            self._ensure_column(conn, "code_jobs", "telegram_plan_message_id", "INTEGER")
            self._ensure_column(conn, "code_jobs", "ci_head_sha", "TEXT")
            self._ensure_column(conn, "code_jobs", "ci_wait_started_at", "INTEGER")
            self._ensure_column(
                conn, "code_jobs", "ci_repair_attempts", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(conn, "code_jobs", "ci_checks_json", "TEXT")
            self._ensure_column(conn, "code_jobs", "deployment_mode", "TEXT")
            self._ensure_column(conn, "code_jobs", "deployment_status", "TEXT")
            self._ensure_column(
                conn,
                "code_jobs",
                "deployment_conflict_attempts",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(conn, "code_jobs", "deployment_merge_sha", "TEXT")
            self._ensure_column(conn, "code_jobs", "deployment_run_id", "INTEGER")
            self._ensure_column(conn, "code_jobs", "deployment_run_url", "TEXT")
            self._ensure_column(conn, "code_jobs", "deployment_started_at", "INTEGER")
            self._ensure_column(conn, "code_jobs", "deployment_error", "TEXT")
            self._ensure_column(conn, "code_jobs", "github_plan_question_comment_id", "INTEGER")
            self._ensure_column(
                conn, "code_jobs", "github_plan_question_revision", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(
                conn, "code_jobs", "github_plan_comment_cursor", "INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute(
                """
                UPDATE code_jobs
                SET deployment_mode = 'deploy'
                WHERE deployment_status IS NOT NULL AND deployment_mode IS NULL
                """
            )

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

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

    def set_secret(self, key: str, value: str) -> None:
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO secrets (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now),
            )

    def get_secret(self, key: str, default: str = "") -> str:
        with self.session() as conn:
            row = conn.execute("SELECT value FROM secrets WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def has_secret(self, key: str) -> bool:
        with self.session() as conn:
            row = conn.execute("SELECT 1 FROM secrets WHERE key = ?", (key,)).fetchone()
        return bool(row)

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
        return {
            "telegram_chat_id": chat_id,
            "active_repo": None,
            "default_branch": "main",
            "local_repo_path": None,
        }

    def get_scope_settings(self, chat_id: int, thread_id: int | None) -> dict[str, Any]:
        if thread_id is None:
            return self.get_chat_settings(chat_id)
        with self.session() as conn:
            row = conn.execute(
                """
                SELECT * FROM topic_settings
                WHERE telegram_chat_id = ? AND telegram_thread_id = ?
                """,
                (chat_id, thread_id),
            ).fetchone()
        if row:
            return dict(row)
        return {
            "telegram_chat_id": chat_id,
            "telegram_thread_id": thread_id,
            "active_repo": None,
            "default_branch": "main",
            "local_repo_path": None,
        }

    def set_scope_repo(
        self,
        chat_id: int,
        thread_id: int | None,
        repo: str | None,
        user_id: int,
        default_branch: str = "main",
    ) -> None:
        if thread_id is None:
            self.set_chat_repo(chat_id, repo, user_id, default_branch)
            return
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO topic_settings (
                    telegram_chat_id, telegram_thread_id, active_repo, default_branch,
                    updated_by_user_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_chat_id, telegram_thread_id) DO UPDATE SET
                    active_repo = excluded.active_repo,
                    default_branch = excluded.default_branch,
                    updated_by_user_id = excluded.updated_by_user_id,
                    updated_at = excluded.updated_at
                """,
                (chat_id, thread_id, repo, default_branch, user_id, now),
            )

    def set_scope_local_repo(
        self,
        chat_id: int,
        thread_id: int | None,
        path: str | None,
        user_id: int,
    ) -> None:
        if thread_id is None:
            self.set_chat_local_repo(chat_id, path, user_id)
            return
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO topic_settings (
                    telegram_chat_id, telegram_thread_id, active_repo, default_branch,
                    local_repo_path, updated_by_user_id, updated_at
                ) VALUES (?, ?, NULL, 'main', ?, ?, ?)
                ON CONFLICT(telegram_chat_id, telegram_thread_id) DO UPDATE SET
                    local_repo_path = excluded.local_repo_path,
                    updated_by_user_id = excluded.updated_by_user_id,
                    updated_at = excluded.updated_at
                """,
                (chat_id, thread_id, path, user_id, now),
            )

    def set_chat_local_repo(self, chat_id: int, path: str | None, user_id: int) -> None:
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO chat_settings (
                    telegram_chat_id, active_repo, default_branch, local_repo_path,
                    updated_by_user_id, updated_at
                ) VALUES (?, NULL, 'main', ?, ?, ?)
                ON CONFLICT(telegram_chat_id) DO UPDATE SET
                    local_repo_path = excluded.local_repo_path,
                    updated_by_user_id = excluded.updated_by_user_id,
                    updated_at = excluded.updated_at
                """,
                (chat_id, path, user_id, now),
            )

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

    def complete_repo_setup(
        self,
        chat_id: int,
        thread_id: int | None,
        repo: str,
        default_branch: str,
        local_repo_path: str,
        user_id: int,
    ) -> None:
        """Allow and configure a repository only after its cache is ready."""
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
            if thread_id is None:
                conn.execute(
                    """
                    INSERT INTO chat_settings (
                        telegram_chat_id, active_repo, default_branch, local_repo_path,
                        updated_by_user_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(telegram_chat_id) DO UPDATE SET
                        active_repo = excluded.active_repo,
                        default_branch = excluded.default_branch,
                        local_repo_path = excluded.local_repo_path,
                        updated_by_user_id = excluded.updated_by_user_id,
                        updated_at = excluded.updated_at
                    """,
                    (chat_id, repo, default_branch, local_repo_path, user_id, now),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO topic_settings (
                        telegram_chat_id, telegram_thread_id, active_repo, default_branch,
                        local_repo_path, updated_by_user_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(telegram_chat_id, telegram_thread_id) DO UPDATE SET
                        active_repo = excluded.active_repo,
                        default_branch = excluded.default_branch,
                        local_repo_path = excluded.local_repo_path,
                        updated_by_user_id = excluded.updated_by_user_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        chat_id,
                        thread_id,
                        repo,
                        default_branch,
                        local_repo_path,
                        user_id,
                        now,
                    ),
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

    def set_repo_deploy_workflow(self, repo: str, workflow: str | None) -> None:
        with self.session() as conn:
            cursor = conn.execute(
                "UPDATE allowed_repos SET deploy_workflow = ? WHERE repo = ?",
                (workflow, repo),
            )
        if cursor.rowcount != 1:
            raise ValueError("Repo is not allowed.")

    def get_repo_deploy_workflow(self, repo: str) -> str:
        with self.session() as conn:
            row = conn.execute(
                "SELECT deploy_workflow FROM allowed_repos WHERE repo = ?", (repo,)
            ).fetchone()
        return str(row["deploy_workflow"] or "") if row else ""

    def set_repo_deploy_enabled(self, repo: str, enabled: bool) -> None:
        with self.session() as conn:
            cursor = conn.execute(
                "UPDATE allowed_repos SET deploy_enabled = ? WHERE repo = ?",
                (int(enabled), repo),
            )
        if cursor.rowcount != 1:
            raise ValueError("Repo is not allowed.")

    def is_repo_deploy_enabled(self, repo: str) -> bool:
        with self.session() as conn:
            row = conn.execute(
                "SELECT deploy_enabled FROM allowed_repos WHERE repo = ?", (repo,)
            ).fetchone()
        return bool(row and row["deploy_enabled"])

    def create_plan(self, plan: dict[str, Any]) -> None:
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO plans (
                    id, telegram_chat_id, telegram_thread_id, telegram_user_id,
                    repo, base_branch, target_branch,
                    request_text, plan_json, status, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan["id"],
                    plan["telegram_chat_id"],
                    plan.get("telegram_thread_id"),
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

    def create_issue_draft(self, draft: dict[str, Any], attachments: list[dict[str, Any]]) -> None:
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO issue_drafts (
                    id, telegram_chat_id, telegram_thread_id, telegram_user_id,
                    repo, default_branch, local_repo_path, request_text,
                    issue_json, status, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft["id"],
                    draft["telegram_chat_id"],
                    draft.get("telegram_thread_id"),
                    draft["telegram_user_id"],
                    draft["repo"],
                    draft["default_branch"],
                    draft.get("local_repo_path", ""),
                    draft["request_text"],
                    json.dumps(draft["issue_json"], separators=(",", ":")),
                    draft["status"],
                    draft["created_at"],
                    draft["expires_at"],
                ),
            )
            conn.executemany(
                """
                INSERT INTO issue_attachments (
                    draft_id, position, telegram_file_id, telegram_file_unique_id,
                    mime_type, file_size
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        draft["id"],
                        item["position"],
                        item["telegram_file_id"],
                        item["telegram_file_unique_id"],
                        item["mime_type"],
                        item["file_size"],
                    )
                    for item in attachments
                ],
            )
            conn.execute(
                """
                INSERT INTO issue_draft_revisions (
                    draft_id, revision_number, feedback_text, issue_json,
                    added_attachments_json, telegram_user_id, created_at
                ) VALUES (?, 1, '', ?, ?, ?, ?)
                """,
                (
                    draft["id"],
                    json.dumps(draft["issue_json"], separators=(",", ":")),
                    json.dumps(attachments, separators=(",", ":")),
                    draft["telegram_user_id"],
                    draft["created_at"],
                ),
            )

    def get_issue_draft(self, draft_id: str) -> dict[str, Any] | None:
        with self.session() as conn:
            row = conn.execute("SELECT * FROM issue_drafts WHERE id = ?", (draft_id,)).fetchone()
            attachment_rows = conn.execute(
                "SELECT * FROM issue_attachments WHERE draft_id = ? ORDER BY position",
                (draft_id,),
            ).fetchall()
            revision_row = conn.execute(
                "SELECT MAX(revision_number) AS revision_number FROM issue_draft_revisions WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        if not row:
            return None
        draft = dict(row)
        draft["issue_json"] = json.loads(str(draft["issue_json"]))
        draft["attachments"] = [dict(item) for item in attachment_rows]
        draft["revision_number"] = int(revision_row["revision_number"] or 1)
        return draft

    def get_issue_draft_revisions(self, draft_id: str) -> list[dict[str, Any]]:
        with self.session() as conn:
            rows = conn.execute(
                """
                SELECT * FROM issue_draft_revisions
                WHERE draft_id = ?
                ORDER BY revision_number
                """,
                (draft_id,),
            ).fetchall()
        revisions = [dict(row) for row in rows]
        for revision in revisions:
            revision["issue_json"] = json.loads(str(revision["issue_json"]))
            revision["added_attachments"] = json.loads(str(revision.pop("added_attachments_json")))
        return revisions

    def revise_issue_draft(
        self,
        *,
        draft_id: str,
        telegram_chat_id: int,
        telegram_user_id: int,
        feedback_text: str,
        issue_json: dict[str, Any],
        attachments: list[dict[str, Any]],
        expires_at: int,
    ) -> int:
        now = int(time.time())
        encoded_issue = json.dumps(issue_json, separators=(",", ":"))
        encoded_attachments = json.dumps(attachments, separators=(",", ":"))
        with self.session() as conn:
            row = conn.execute(
                "SELECT * FROM issue_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
            if not row:
                raise ValueError("Issue draft not found.")
            if int(row["telegram_chat_id"]) != telegram_chat_id:
                raise ValueError("Issue draft belongs to a different chat.")
            if int(row["telegram_user_id"]) != telegram_user_id:
                raise ValueError("Only the original author can edit this issue draft.")
            if str(row["status"]) != "pending":
                raise ValueError(f"Issue draft is not pending. Current status: {row['status']}")
            if int(row["expires_at"]) < now:
                conn.execute("UPDATE issue_drafts SET status = 'expired' WHERE id = ?", (draft_id,))
                conn.commit()
                raise ValueError("Issue draft expired. Create a new draft with /issue.")

            revision_row = conn.execute(
                "SELECT MAX(revision_number) AS revision_number FROM issue_draft_revisions WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
            current_revision = int(revision_row["revision_number"] or 0)
            if current_revision == 0:
                conn.execute(
                    """
                    INSERT INTO issue_draft_revisions (
                        draft_id, revision_number, feedback_text, issue_json,
                        added_attachments_json, telegram_user_id, created_at
                    ) VALUES (?, 1, '', ?, '[]', ?, ?)
                    """,
                    (
                        draft_id,
                        str(row["issue_json"]),
                        int(row["telegram_user_id"]),
                        int(row["created_at"]),
                    ),
                )
                current_revision = 1

            next_position_row = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS position FROM issue_attachments WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
            next_position = int(next_position_row["position"])
            conn.executemany(
                """
                INSERT INTO issue_attachments (
                    draft_id, position, telegram_file_id, telegram_file_unique_id,
                    mime_type, file_size
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        draft_id,
                        next_position + offset,
                        item["telegram_file_id"],
                        item["telegram_file_unique_id"],
                        item["mime_type"],
                        item["file_size"],
                    )
                    for offset, item in enumerate(attachments)
                ],
            )
            revision_number = current_revision + 1
            conn.execute(
                "UPDATE issue_drafts SET issue_json = ?, expires_at = ? WHERE id = ?",
                (encoded_issue, expires_at, draft_id),
            )
            conn.execute(
                """
                INSERT INTO issue_draft_revisions (
                    draft_id, revision_number, feedback_text, issue_json,
                    added_attachments_json, telegram_user_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_id,
                    revision_number,
                    feedback_text,
                    encoded_issue,
                    encoded_attachments,
                    telegram_user_id,
                    now,
                ),
            )
        return revision_number

    def update_issue_draft_status(
        self,
        draft_id: str,
        status: str,
        issue_number: int | None = None,
        issue_url: str | None = None,
    ) -> None:
        with self.session() as conn:
            conn.execute(
                """
                UPDATE issue_drafts
                SET status = ?,
                    github_issue_number = COALESCE(?, github_issue_number),
                    github_issue_url = COALESCE(?, github_issue_url)
                WHERE id = ?
                """,
                (status, issue_number, issue_url, draft_id),
            )

    def set_issue_attachment_paths(self, draft_id: str, paths: list[str]) -> None:
        with self.session() as conn:
            conn.executemany(
                "UPDATE issue_attachments SET asset_path = ? WHERE draft_id = ? AND position = ?",
                [(path, draft_id, position) for position, path in enumerate(paths)],
            )

    def create_do_job(self, job: dict[str, Any]) -> None:
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO do_jobs (
                    id, telegram_chat_id, telegram_user_id, telegram_thread_id,
                    mode, lane, repo, default_branch, source_repo_path,
                    workspace_path, payload_path, image_count, status,
                    latest_activity, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job["id"], job["telegram_chat_id"], job["telegram_user_id"],
                    job.get("telegram_thread_id"), job["mode"], job["lane"],
                    job.get("repo"), job.get("default_branch"),
                    job.get("source_repo_path"), job.get("workspace_path"),
                    job["payload_path"], int(job.get("image_count") or 0),
                    job.get("status", "preparing"), job.get("latest_activity", ""),
                    now, now,
                ),
            )

    def get_do_job(self, job_id: str) -> dict[str, Any] | None:
        with self.session() as conn:
            row = conn.execute("SELECT * FROM do_jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list_do_jobs(
        self,
        *,
        chat_id: int | None = None,
        thread_id: int | None = None,
        exact_thread: bool = False,
        statuses: tuple[str, ...] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if chat_id is not None:
            conditions.append("telegram_chat_id = ?")
            params.append(chat_id)
        if exact_thread:
            conditions.append("telegram_thread_id IS ?")
            params.append(thread_id)
        if statuses:
            conditions.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        with self.session() as conn:
            rows = conn.execute(
                f"SELECT * FROM do_jobs {where} ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def count_active_do_jobs(self) -> int:
        with self.session() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM do_jobs WHERE status IN ('preparing','queued','running')"
            ).fetchone()
        return int(row["count"]) if row else 0

    def update_do_job(
        self,
        job_id: str,
        values: dict[str, Any],
        *,
        allowed_statuses: tuple[str, ...] | None = None,
    ) -> bool:
        allowed = {
            "telegram_message_id", "workspace_path", "status", "latest_activity", "error"
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unsupported do job columns: {sorted(unknown)}")
        if not values:
            return False
        encoded = {**values, "updated_at": int(time.time())}
        query = f"UPDATE do_jobs SET {', '.join(f'{key} = ?' for key in encoded)} WHERE id = ?"
        params: list[Any] = [*encoded.values(), job_id]
        if allowed_statuses:
            query += f" AND status IN ({','.join('?' for _ in allowed_statuses)})"
            params.extend(allowed_statuses)
        with self.session() as conn:
            cursor = conn.execute(query, params)
        return cursor.rowcount == 1

    def claim_do_job(self, job_id: str) -> bool:
        return self.update_do_job(
            job_id,
            {"status": "running", "latest_activity": "Worker started", "error": None},
            allowed_statuses=("queued",),
        )

    def add_do_job_event(self, job_id: str, event_type: str, summary: dict[str, Any]) -> None:
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO do_job_events (job_id, event_type, summary_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (job_id, event_type, json.dumps(summary, separators=(",", ":")), int(time.time())),
            )

    def list_do_job_events(self, job_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
        with self.session() as conn:
            rows = conn.execute(
                """
                SELECT event_type, summary_json, created_at FROM do_job_events
                WHERE job_id = ? ORDER BY id DESC LIMIT ?
                """,
                (job_id, limit),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in reversed(rows):
            try:
                summary = json.loads(str(row["summary_json"]))
            except (TypeError, json.JSONDecodeError):
                summary = {}
            events.append({
                "event_type": row["event_type"],
                "summary": summary if isinstance(summary, dict) else {},
                "created_at": row["created_at"],
            })
        return events

    def mark_running_do_jobs_interrupted(self) -> list[str]:
        now = int(time.time())
        with self.session() as conn:
            rows = conn.execute("SELECT id FROM do_jobs WHERE status = 'running'").fetchall()
            conn.execute(
                """
                UPDATE do_jobs SET status = 'interrupted',
                    error = 'Do worker restarted during an active Codex turn.',
                    latest_activity = 'Interrupted by worker restart', updated_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )
        return [str(row["id"]) for row in rows]

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

    def create_code_job(self, job: dict[str, Any]) -> None:
        now = int(time.time())
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO code_jobs (
                    id, telegram_chat_id, telegram_user_id, telegram_thread_id,
                    repo, issue_number, issue_title, issue_url, issue_context_json,
                    base_branch, target_branch, workspace_path, source_repo_path, status, resume_phase,
                    skip_plan, feedback_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?)
                """,
                (
                    job["id"],
                    job["telegram_chat_id"],
                    job["telegram_user_id"],
                    job.get("telegram_thread_id"),
                    job["repo"],
                    job["issue_number"],
                    job["issue_title"],
                    job["issue_url"],
                    json.dumps(job["issue_context_json"], separators=(",", ":")),
                    job["base_branch"],
                    job["target_branch"],
                    job["workspace_path"],
                    job["source_repo_path"],
                    job["status"],
                    job["resume_phase"],
                    int(bool(job.get("skip_plan"))),
                    now,
                    now,
                ),
            )

    def get_code_job(self, job_id: str) -> dict[str, Any] | None:
        with self.session() as conn:
            row = conn.execute("SELECT * FROM code_jobs WHERE id = ?", (job_id,)).fetchone()
        return self._decode_code_job(row)

    def get_active_code_job(self, repo: str, issue_number: int) -> dict[str, Any] | None:
        with self.session() as conn:
            row = conn.execute(
                """
                SELECT * FROM code_jobs
                WHERE repo = ? AND issue_number = ?
                  AND status NOT IN ('ready', 'discarded')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (repo, issue_number),
            ).fetchone()
        return self._decode_code_job(row)

    def list_code_jobs(
        self,
        *,
        chat_id: int | None = None,
        thread_id: int | None = None,
        exact_thread: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self.session() as conn:
            if chat_id is None:
                rows = conn.execute(
                    "SELECT * FROM code_jobs ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            elif exact_thread:
                rows = conn.execute(
                    """
                    SELECT * FROM code_jobs
                    WHERE telegram_chat_id = ? AND telegram_thread_id IS ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (chat_id, thread_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM code_jobs
                    WHERE telegram_chat_id = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (chat_id, limit),
                ).fetchall()
        return [job for row in rows if (job := self._decode_code_job(row)) is not None]

    def count_queued_code_jobs(self) -> int:
        with self.session() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count FROM code_jobs
                WHERE status IN (
                    'queued_plan', 'queued_plan_edit', 'queued_code', 'queued_checks', 'queued_rebase'
                )
                """
            ).fetchone()
        return int(row["count"]) if row else 0

    def update_code_job(
        self,
        job_id: str,
        values: dict[str, Any],
        *,
        allowed_statuses: tuple[str, ...] | None = None,
    ) -> bool:
        allowed_columns = {
            "telegram_message_id",
            "telegram_plan_message_id",
            "base_sha",
            "source_repo_path",
            "status",
            "resume_phase",
            "plan_json",
            "plan_revision",
            "feedback_json",
            "codex_thread_id",
            "pull_request_number",
            "pull_request_url",
            "github_plan_question_comment_id",
            "github_plan_question_revision",
            "github_plan_comment_cursor",
            "latest_activity",
            "result_json",
            "ci_head_sha",
            "ci_wait_started_at",
            "ci_repair_attempts",
            "ci_checks_json",
            "deployment_mode",
            "deployment_status",
            "deployment_conflict_attempts",
            "deployment_merge_sha",
            "deployment_run_id",
            "deployment_run_url",
            "deployment_started_at",
            "deployment_error",
            "error",
        }
        unknown = set(values) - allowed_columns
        if unknown:
            raise ValueError(f"unsupported code job columns: {sorted(unknown)}")
        if not values:
            return False
        encoded = dict(values)
        for key in ("plan_json", "feedback_json", "result_json", "ci_checks_json"):
            if key in encoded and encoded[key] is not None and not isinstance(encoded[key], str):
                encoded[key] = json.dumps(encoded[key], separators=(",", ":"))
        encoded["updated_at"] = int(time.time())
        assignments = ", ".join(f"{key} = ?" for key in encoded)
        params = list(encoded.values())
        query = f"UPDATE code_jobs SET {assignments} WHERE id = ?"
        params.append(job_id)
        if allowed_statuses:
            placeholders = ",".join("?" for _ in allowed_statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(allowed_statuses)
        with self.session() as conn:
            cursor = conn.execute(query, params)
        return cursor.rowcount == 1

    def add_code_plan_feedback(
        self,
        job_id: str,
        *,
        source: str,
        source_id: str,
        author: str,
        body: str,
    ) -> str:
        text = body.strip()[:8192]
        if source not in {"telegram", "github"}:
            raise ValueError("unsupported plan feedback source")
        if not text:
            raise ValueError("Plan feedback is required.")
        now = int(time.time())
        with self.session() as conn:
            job = conn.execute(
                "SELECT status, feedback_json FROM code_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not job:
                raise ValueError("Code job not found.")
            if str(job["status"]) not in {
                "awaiting_clarification",
                "awaiting_approval",
                "queued_plan_edit",
                "editing_plan",
                "planning",
            }:
                raise ValueError("Code job is not awaiting plan feedback.")
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO code_plan_feedback (
                    job_id, source, source_id, author, body, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (job_id, source, source_id, author, text, now),
            )
            if cursor.rowcount != 1:
                return "duplicate"
            try:
                history = json.loads(str(job["feedback_json"] or "[]"))
            except json.JSONDecodeError:
                history = []
            if not isinstance(history, list):
                history = []
            history.append(text)
            queued = str(job["status"]) in {"awaiting_clarification", "awaiting_approval"}
            conn.execute(
                """
                UPDATE code_jobs
                SET feedback_json = ?, status = CASE WHEN status IN (
                        'awaiting_clarification', 'awaiting_approval'
                    ) THEN 'queued_plan_edit' ELSE status END,
                    resume_phase = 'plan',
                    latest_activity = CASE WHEN status IN (
                        'awaiting_clarification', 'awaiting_approval'
                    ) THEN 'Plan feedback received' ELSE latest_activity END,
                    updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(history, separators=(",", ":")), now, job_id),
            )
        return "queued" if queued else "pending"

    def list_pending_code_plan_feedback(self, job_id: str) -> list[dict[str, Any]]:
        with self.session() as conn:
            rows = conn.execute(
                """
                SELECT * FROM code_plan_feedback
                WHERE job_id = ? AND state = 'pending'
                ORDER BY id
                """,
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_code_plan_feedback_applied(
        self, job_id: str, feedback_ids: list[int], revision: int
    ) -> None:
        if not feedback_ids:
            return
        placeholders = ",".join("?" for _ in feedback_ids)
        with self.session() as conn:
            conn.execute(
                f"""
                UPDATE code_plan_feedback
                SET state = 'applied', applied_revision = ?
                WHERE job_id = ? AND state = 'pending' AND id IN ({placeholders})
                """,
                (revision, job_id, *feedback_ids),
            )

    def queue_pending_code_plan_feedback(self, job_id: str) -> bool:
        now = int(time.time())
        with self.session() as conn:
            cursor = conn.execute(
                """
                UPDATE code_jobs
                SET status = 'queued_plan_edit', resume_phase = 'plan',
                    latest_activity = 'Additional plan feedback received', updated_at = ?
                WHERE id = ? AND status IN ('awaiting_clarification', 'awaiting_approval')
                  AND EXISTS (
                    SELECT 1 FROM code_plan_feedback
                    WHERE job_id = code_jobs.id AND state = 'pending'
                  )
                """,
                (now, job_id),
            )
        return cursor.rowcount == 1

    def list_plan_feedback_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.session() as conn:
            rows = conn.execute(
                """
                SELECT * FROM code_jobs
                WHERE status IN ('awaiting_clarification', 'awaiting_approval')
                  AND pull_request_number IS NOT NULL
                ORDER BY updated_at ASC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [job for row in rows if (job := self._decode_code_job(row)) is not None]

    def start_code_job_operation(self, job_id: str, mode: str) -> str:
        if mode not in {"merge", "deploy"}:
            raise ValueError("unsupported code job operation")
        now = int(time.time())
        with self.session() as conn:
            job = conn.execute(
                """
                SELECT status, deployment_mode, deployment_status, deployment_merge_sha
                FROM code_jobs WHERE id = ?
                """,
                (job_id,),
            ).fetchone()
            if not job:
                return "missing"
            if str(job["status"]) != "ready":
                return "not_ready"
            operation_status = str(job["deployment_status"] or "")
            if operation_status in {
                "queued", "merging", "resolving_conflicts", "waiting_workflow",
                "dispatching", "deploying"
            }:
                return "active"
            merge_sha = str(job["deployment_merge_sha"] or "")
            if mode == "merge" and merge_sha:
                return "merged"
            next_status = "waiting_workflow" if mode == "deploy" and merge_sha else "queued"
            cursor = conn.execute(
                """
                UPDATE code_jobs SET
                    deployment_mode = ?, deployment_status = ?, deployment_error = NULL,
                    deployment_conflict_attempts = 0,
                    deployment_run_id = NULL, deployment_run_url = NULL,
                    deployment_started_at = ?, updated_at = ?
                WHERE id = ? AND status = 'ready'
                """,
                (mode, next_status, now, now, job_id),
            )
        return "started" if cursor.rowcount == 1 else "active"

    def list_active_deployments(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.session() as conn:
            rows = conn.execute(
                """
                SELECT * FROM code_jobs
                WHERE deployment_status IN (
                    'queued', 'merging', 'resolving_conflicts', 'waiting_workflow',
                    'dispatching', 'deploying'
                )
                ORDER BY updated_at ASC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [job for row in rows if (job := self._decode_code_job(row)) is not None]

    def add_code_job_event(self, job_id: str, event_type: str, summary: dict[str, Any]) -> None:
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO code_job_events (job_id, event_type, summary_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    job_id,
                    event_type,
                    json.dumps(summary, separators=(",", ":")),
                    int(time.time()),
                ),
            )

    def list_code_job_events(self, job_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
        with self.session() as conn:
            rows = conn.execute(
                """
                SELECT event_type, summary_json, created_at
                FROM code_job_events
                WHERE job_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (job_id, limit),
            ).fetchall()
        events = []
        for row in reversed(rows):
            try:
                summary = json.loads(row["summary_json"])
            except (TypeError, json.JSONDecodeError):
                summary = {}
            events.append(
                {
                    "event_type": row["event_type"],
                    "summary": summary if isinstance(summary, dict) else {},
                    "created_at": row["created_at"],
                }
            )
        return events

    def mark_running_code_jobs_interrupted(self) -> int:
        running = (
            "preparing",
            "planning",
            "editing_plan",
            "coding",
            "validating",
            "pushing",
            "repairing_checks",
            "rebasing",
        )
        placeholders = ",".join("?" for _ in running)
        with self.session() as conn:
            cursor = conn.execute(
                f"""
                UPDATE code_jobs
                SET status = 'interrupted',
                    error = 'Bot restarted during an active Codex turn.',
                    latest_activity = 'Interrupted by service restart',
                    updated_at = ?
                WHERE status IN ({placeholders})
                """,
                (int(time.time()), *running),
            )
        return cursor.rowcount

    @staticmethod
    def _decode_code_job(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        job = dict(row)
        for key, default in (
            ("issue_context_json", {}),
            ("plan_json", None),
            ("feedback_json", []),
            ("result_json", None),
            ("ci_checks_json", None),
        ):
            value = job.get(key)
            job[key] = json.loads(str(value)) if value else default
        job["skip_plan"] = bool(job["skip_plan"])
        return job
