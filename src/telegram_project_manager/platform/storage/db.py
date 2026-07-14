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
        except Exception:
            conn.rollback()
            raise
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

                CREATE TABLE IF NOT EXISTS secrets (
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

                CREATE TABLE IF NOT EXISTS issue_drafts (
                    id TEXT PRIMARY KEY,
                    telegram_chat_id INTEGER NOT NULL,
                    telegram_user_id INTEGER NOT NULL,
                    repo TEXT NOT NULL,
                    default_branch TEXT NOT NULL,
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
                    repo TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    issue_title TEXT NOT NULL,
                    issue_url TEXT NOT NULL,
                    issue_context_json TEXT NOT NULL,
                    base_branch TEXT NOT NULL,
                    base_sha TEXT,
                    target_branch TEXT NOT NULL,
                    workspace_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    resume_phase TEXT NOT NULL,
                    skip_plan INTEGER NOT NULL DEFAULT 0,
                    plan_json TEXT,
                    plan_revision INTEGER NOT NULL DEFAULT 0,
                    feedback_json TEXT NOT NULL DEFAULT '[]',
                    codex_thread_id TEXT,
                    pull_request_number INTEGER,
                    pull_request_url TEXT,
                    latest_activity TEXT NOT NULL DEFAULT '',
                    result_json TEXT,
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

    def create_issue_draft(self, draft: dict[str, Any], attachments: list[dict[str, Any]]) -> None:
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO issue_drafts (
                    id, telegram_chat_id, telegram_user_id, repo, default_branch,
                    request_text, issue_json, status, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft["id"],
                    draft["telegram_chat_id"],
                    draft["telegram_user_id"],
                    draft["repo"],
                    draft["default_branch"],
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
                    base_branch, target_branch, workspace_path, status, resume_phase,
                    skip_plan, feedback_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?)
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

    def list_code_jobs(self, *, chat_id: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
        with self.session() as conn:
            if chat_id is None:
                rows = conn.execute(
                    "SELECT * FROM code_jobs ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
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
                WHERE status IN ('queued_plan', 'queued_plan_edit', 'queued_code')
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
            "base_sha",
            "status",
            "resume_phase",
            "plan_json",
            "plan_revision",
            "feedback_json",
            "codex_thread_id",
            "pull_request_number",
            "pull_request_url",
            "latest_activity",
            "result_json",
            "error",
        }
        unknown = set(values) - allowed_columns
        if unknown:
            raise ValueError(f"unsupported code job columns: {sorted(unknown)}")
        if not values:
            return False
        encoded = dict(values)
        for key in ("plan_json", "feedback_json", "result_json"):
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

    def mark_running_code_jobs_interrupted(self) -> int:
        running = (
            "preparing",
            "planning",
            "editing_plan",
            "coding",
            "validating",
            "pushing",
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
        ):
            value = job.get(key)
            job[key] = json.loads(str(value)) if value else default
        job["skip_plan"] = bool(job["skip_plan"])
        return job
