from __future__ import annotations

from telegram_project_manager.platform.storage.db import Database


class PermissionService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def is_admin(self, telegram_user_id: int) -> bool:
        user = self.db.get_user(telegram_user_id)
        return bool(user and user["role"] == "admin")

    def require_admin(self, telegram_user_id: int) -> str | None:
        if self.is_admin(telegram_user_id):
            return None
        return "Unauthorized. Admin role required."

    def can_request_commit(self, telegram_user_id: int) -> bool:
        return self.is_admin(telegram_user_id)

