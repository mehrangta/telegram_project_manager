from __future__ import annotations

from telegram_project_manager.bots.commit_manager.executor import CommitExecutionService
from telegram_project_manager.bots.commit_manager.planner import CommitPlanner
from telegram_project_manager.bots.commit_manager.schemas import PlanValidationError, validate_repo
from telegram_project_manager.integrations.gh.commits import GhCommitExecutor
from telegram_project_manager.integrations.gh.runner import GhError, GhRunner
from telegram_project_manager.platform.config import normalize_config_value
from telegram_project_manager.platform.llm.client import LlmError, OpenAICompatibleClient
from telegram_project_manager.platform.llm.memory import DEFAULT_MEMORY_MAX_MESSAGES, memory_session_id
from telegram_project_manager.platform.permissions import PermissionService
from telegram_project_manager.platform.responses import bullet_list, truncate
from telegram_project_manager.platform.router import IncomingMessage
from telegram_project_manager.platform.storage.db import Database


class CommitManager:
    def __init__(self, db: Database, llm: OpenAICompatibleClient, gh: GhRunner, executor: GhCommitExecutor) -> None:
        self.db = db
        self.permissions = PermissionService(db)
        self.gh = gh
        self.planner = CommitPlanner(db, llm)
        self.execution = CommitExecutionService(db, executor)

    async def handle(self, message: IncomingMessage) -> str | None:
        text = message.text.strip()
        if not text:
            return None
        command, rest = split_command(text)
        if command in {"/start", "/help", "help"}:
            return self.help()
        if command == "/status":
            return self.status(message)
        if command == "/repo":
            return self.repo(message, rest)
        if command == "/repos":
            return self.repos()
        if command == "/config":
            return self.config(message, rest)
        if command == "/memory":
            return self.memory(message, rest)
        if command == "/admin":
            return self.admin(message, rest)
        if command == "/branch":
            return self.branch(message, rest)
        if command == "/commit":
            return self.commit(message, rest)
        if command == "/confirm":
            return self.confirm(message, rest)
        if command == "/cancel":
            return self.cancel(message, rest)
        return None

    def help(self) -> str:
        return """Telegram Project Manager

Commands:
/status
/repo allow owner/repository
/repo set owner/repository
/repo show
/commit <request>
/confirm <plan_id>
/cancel <plan_id>
/config show
/config set openai_base_url <url>
/config set openai_model <model>
/config set llm_memory_max_messages <count>
/memory status
/memory clear
"""

    def status(self, message: IncomingMessage) -> str:
        auth = self.gh.auth_status()
        chat = self.db.get_chat_settings(message.chat_id)
        admins = [str(user["telegram_user_id"]) for user in self.db.list_users() if user["role"] == "admin"]
        gh_line = "ok" if auth.returncode == 0 else "failed"
        return truncate(
            "\n".join(
                [
                    "Status",
                    f"GitHub auth: {gh_line}",
                    f"Active repo: {chat.get('active_repo') or 'not set'}",
                    f"Default branch: {chat.get('default_branch') or 'main'}",
                    f"Admins: {', '.join(admins) if admins else 'none'}",
                    f"OpenAI model: {self.db.get_setting('openai_model', 'not set')}",
                ]
            )
        )

    def repo(self, message: IncomingMessage, rest: str) -> str:
        parts = rest.split()
        if not parts or parts[0] == "show":
            chat = self.db.get_chat_settings(message.chat_id)
            return f"Active repo: {chat.get('active_repo') or 'not set'}\nDefault branch: {chat.get('default_branch') or 'main'}"
        admin_error = self.permissions.require_admin(message.user_id)
        if admin_error:
            return admin_error
        action = parts[0]
        if action == "allow" and len(parts) == 2:
            return self._allow_repo(parts[1], message.user_id)
        if action == "disallow" and len(parts) == 2:
            return self._disallow_repo(parts[1])
        if action == "set" and len(parts) == 2:
            return self._set_repo(message, parts[1])
        if action == "clear":
            self.db.set_chat_repo(message.chat_id, None, message.user_id)
            return "Active repo cleared for this chat."
        return "Usage: /repo show | /repo allow owner/repository | /repo set owner/repository | /repo clear"

    def _allow_repo(self, repo: str, user_id: int) -> str:
        try:
            validate_repo(repo)
        except PlanValidationError as exc:
            return str(exc)
        self.db.allow_repo(repo, user_id)
        return f"Repo allowed: {repo}"

    def _disallow_repo(self, repo: str) -> str:
        try:
            validate_repo(repo)
        except PlanValidationError as exc:
            return str(exc)
        self.db.disallow_repo(repo)
        return f"Repo disallowed: {repo}"

    def _set_repo(self, message: IncomingMessage, repo: str) -> str:
        try:
            validate_repo(repo)
        except PlanValidationError as exc:
            return str(exc)
        if not self.db.is_repo_allowed(repo):
            return "Repo is not allowed. Admin must run: /repo allow owner/repository"
        branch = self.db.get_chat_settings(message.chat_id).get("default_branch") or "main"
        self.db.set_chat_repo(message.chat_id, repo, message.user_id, branch)
        return f"Active repo set: {repo}"

    def repos(self) -> str:
        repos = self.db.list_allowed_repos()
        return "Allowed repos:\n" + (bullet_list(repos) if repos else "- none")

    def config(self, message: IncomingMessage, rest: str) -> str:
        parts = rest.split(maxsplit=2)
        if not parts or parts[0] == "show":
            settings = self.db.all_settings()
            if not settings:
                return "Config: no settings stored."
            return "Config:\n" + bullet_list(f"{key}={value}" for key, value in settings.items())
        admin_error = self.permissions.require_admin(message.user_id)
        if admin_error:
            return admin_error
        if len(parts) == 3 and parts[0] == "set":
            key, value = parts[1], parts[2]
            try:
                value = normalize_config_value(key, value)
            except ValueError as exc:
                return str(exc)
            self.db.set_setting(key, value)
            return f"Config set: {key}"
        return "Usage: /config show | /config set <key> <value>"

    def memory(self, message: IncomingMessage, rest: str) -> str:
        action = rest.strip().lower() or "status"
        session_id = memory_session_id(message.chat_id)
        limit = int(self.db.get_setting("llm_memory_max_messages", str(DEFAULT_MEMORY_MAX_MESSAGES)))
        if action in {"status", "show"}:
            count = self.db.count_llm_messages(session_id)
            return f"LLM memory: {count}/{limit} messages for this chat."
        if action == "clear":
            admin_error = self.permissions.require_admin(message.user_id)
            if admin_error:
                return admin_error
            self.db.clear_llm_messages(session_id)
            return "LLM memory cleared for this chat."
        return "Usage: /memory status | /memory clear"

    def admin(self, message: IncomingMessage, rest: str) -> str:
        admin_error = self.permissions.require_admin(message.user_id)
        if admin_error:
            return admin_error
        parts = rest.split()
        if len(parts) == 2 and parts[0] == "add":
            self.db.upsert_user(int(parts[1]), "", "admin")
            return f"Admin added: {parts[1]}"
        if len(parts) == 2 and parts[0] == "remove":
            self.db.remove_user(int(parts[1]))
            return f"Admin removed: {parts[1]}"
        return "Usage: /admin add <telegram_user_id> | /admin remove <telegram_user_id>"

    def branch(self, message: IncomingMessage, rest: str) -> str:
        admin_error = self.permissions.require_admin(message.user_id)
        if admin_error:
            return admin_error
        branch = rest.strip()
        if not branch:
            return "Usage: /branch <branch_name>"
        chat = self.db.get_chat_settings(message.chat_id)
        self.db.set_chat_repo(message.chat_id, chat.get("active_repo"), message.user_id, branch)
        return f"Default branch set: {branch}"

    def commit(self, message: IncomingMessage, rest: str) -> str:
        if not self.permissions.can_request_commit(message.user_id):
            return "Unauthorized. Admin role required."
        request_text = rest.strip()
        if not request_text:
            return "Usage: /commit <request>"
        chat = self.db.get_chat_settings(message.chat_id)
        repo = chat.get("active_repo")
        if not repo:
            return "No active repo for this chat. Admin: /repo set owner/repository"
        if not self.db.is_repo_allowed(repo):
            return "Active repo is not in allowed repo list. Admin: /repo allow owner/repository"
        branch = chat.get("default_branch") or "main"
        try:
            plan_id, plan = self.planner.create_plan(
                request_text=request_text,
                chat_id=message.chat_id,
                user_id=message.user_id,
                repo=repo,
                base_branch=branch,
            )
        except (LlmError, PlanValidationError, ValueError) as exc:
            self.db.audit("plan.create", "failed", {"error": str(exc), "repo": repo})
            return f"Commit plan not created.\nReason: {exc}"
        if plan.questions:
            return "Need clarification:\n" + bullet_list(plan.questions)
        return "\n".join(
            [
                "Commit plan created.",
                f"Plan ID: {plan_id}",
                f"Repo: {plan.repo}",
                f"Base branch: {plan.base_branch}",
                f"Target branch: {plan.target_branch}",
                f"Message: {plan.commit_message}",
                "Files:",
                bullet_list(change.path for change in plan.changes),
                "",
                f"Run: /confirm {plan_id}",
            ]
        )

    def confirm(self, message: IncomingMessage, rest: str) -> str:
        plan_id = rest.strip()
        if not plan_id:
            return "Usage: /confirm <plan_id>"
        if not self.permissions.can_request_commit(message.user_id):
            return "Unauthorized. Admin role required."
        try:
            result = self.execution.execute(plan_id, message.user_id)
        except (ValueError, GhError, PlanValidationError) as exc:
            self.db.audit("commit.create", "failed", {"error": str(exc)}, plan_id)
            return f"Commit not created.\nReason: {exc}"
        lines = [
            "Commit created.",
            f"Repo: {result.repo}",
            f"Branch: {result.branch}",
            f"Commit: {result.sha[:12]}",
            "Files:",
            bullet_list(result.files),
            f"Link: {result.commit_url}",
        ]
        if result.comment_url:
            lines.append(f"Comment: {result.comment_url}")
        return "\n".join(lines)

    def cancel(self, message: IncomingMessage, rest: str) -> str:
        plan_id = rest.strip()
        if not plan_id:
            return "Usage: /cancel <plan_id>"
        record = self.db.get_plan(plan_id)
        if not record:
            return "Plan not found."
        if record["telegram_user_id"] != message.user_id and not self.permissions.is_admin(message.user_id):
            return "Only the requester or an admin can cancel this plan."
        self.db.update_plan_status(plan_id, "cancelled")
        self.db.audit("plan.cancel", "ok", {}, plan_id)
        return f"Plan cancelled: {plan_id}"


def split_command(text: str) -> tuple[str, str]:
    stripped = text.strip()
    if not stripped:
        return "", ""
    first, _, rest = stripped.partition(" ")
    command = first.split("@", 1)[0].lower()
    return command, rest.strip()
