from __future__ import annotations

import asyncio
import html

from telegram_project_manager.bots.commit_manager.executor import CommitExecutionService
from telegram_project_manager.bots.commit_manager.planner import CommitPlanner
from telegram_project_manager.bots.commit_manager.repository_setup import RepositorySetupService
from telegram_project_manager.bots.commit_manager.schemas import PlanValidationError, validate_repo
from telegram_project_manager.bots.issue_manager.planner import issue_memory_session_id
from telegram_project_manager.integrations.gh.commits import GhCommitExecutor
from telegram_project_manager.integrations.gh.issues import GhIssueReader
from telegram_project_manager.integrations.gh.runner import GhError, GhRunner
from telegram_project_manager.integrations.git.local_repository import (
    LocalRepositoryError,
    LocalRepositoryService,
)
from telegram_project_manager.platform.config import (
    SECRET_CONFIG_KEYS,
    normalize_config_value,
    resolve_codex_model,
)
from telegram_project_manager.platform.llm.client import LlmError, OpenAICompatibleClient
from telegram_project_manager.platform.llm.memory import DEFAULT_MEMORY_MAX_MESSAGES, memory_session_id
from telegram_project_manager.platform.permissions import PermissionService
from telegram_project_manager.platform.responses import OutgoingMessage, bullet_list, truncate
from telegram_project_manager.platform.router import IncomingMessage
from telegram_project_manager.platform.storage.db import Database


class CommitManager:
    def __init__(
        self,
        db: Database,
        llm: OpenAICompatibleClient,
        gh: GhRunner,
        executor: GhCommitExecutor,
        repositories: LocalRepositoryService,
        repository_setup: RepositorySetupService,
        issue_reader: GhIssueReader,
    ) -> None:
        self.db = db
        self.permissions = PermissionService(db)
        self.gh = gh
        self.planner = CommitPlanner(db, llm)
        self.execution = CommitExecutionService(db, executor)
        self.repositories = repositories
        self.repository_setup = repository_setup
        self.issue_reader = issue_reader

    async def handle(self, message: IncomingMessage) -> str | OutgoingMessage | None:
        text = message.text.strip()
        if not text:
            return None
        command, rest = split_command(text)
        if command in {"/start", "/help", "help"}:
            return self.help()
        if command == "/status":
            return self.status(message)
        if command == "/repo":
            setup_parts = rest.split()
            if setup_parts and setup_parts[0] == "setup":
                return await self._repo_setup(message, setup_parts[1:])
            return self.repo(message, rest)
        if command == "/repos":
            return self.repos()
        if command == "/issues":
            return await self.issues(message, rest)
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
/repo setup owner/repository
/repo local set <absolute-path>
/repo deploy enable|disable owner/repository
/repo deploy set owner/repository <workflow-name-or-file>
/repo show
/repo check
/issues
/commit <request>
/issue <prompt> (text or photo/album caption)
/code #123 [--skip-plan]
/code approve|edit|discard|retry|rebase|status <code_job_id>
/deploy <code_job_id> (or reply /deploy to a code-job message)
/confirm <plan_id>
/cancel <plan_id>
/config show
/config set openai_api_key <key> (private chat only)
/config set openai_base_url <url>
/config set openai_model <model>
/config set codex_api_key <key> (private chat only)
/config set codex_base_url <url>
/config set codex_model <model> (shared fallback)
/config set codex_plan_model <model>
/config set codex_code_model <model>
/config set issue_body_llm_enabled <true|false>
/config set llm_memory_max_messages <count>
/memory status
/memory clear
"""

    def status(self, message: IncomingMessage) -> str:
        auth = self.gh.auth_status()
        chat = self.db.get_scope_settings(message.chat_id, message.thread_id)
        admins = [str(user["telegram_user_id"]) for user in self.db.list_users() if user["role"] == "admin"]
        gh_line = "ok" if auth.returncode == 0 else "failed"
        return truncate(
            "\n".join(
                [
                    "Status",
                    f"GitHub auth: {gh_line}",
                    f"Active repo: {chat.get('active_repo') or 'not set'}",
                    f"Default branch: {chat.get('default_branch') or 'main'}",
                    f"Local repo: {chat.get('local_repo_path') or 'not set'}",
                    f"Admins: {', '.join(admins) if admins else 'none'}",
                    f"OpenAI model: {self.db.get_setting('openai_model', 'not set')}",
                    f"Codex SDK auth: {'configured' if self.db.has_secret('codex_api_key') else 'not configured'}",
                    f"Codex plan model: {resolve_codex_model(self.db.get_setting, 'plan') or 'not set'}",
                    f"Codex coding model: {resolve_codex_model(self.db.get_setting, 'code') or 'not set'}",
                ]
            )
        )

    def repo(self, message: IncomingMessage, rest: str) -> str:
        deploy_parts = rest.split(maxsplit=3)
        if deploy_parts and deploy_parts[0] == "deploy":
            return self._repo_deploy(message, deploy_parts[1:])
        parts = rest.split(maxsplit=2)
        if not parts or parts[0] == "show":
            return self._repo_summary(message)
        if parts[0] == "check" and len(parts) == 1:
            return self._repo_summary(message, check_cache=True)
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
            settings = self.db.get_scope_settings(message.chat_id, message.thread_id)
            self.db.set_scope_repo(
                message.chat_id,
                message.thread_id,
                None,
                message.user_id,
                str(settings.get("default_branch") or "main"),
            )
            return f"Active repo cleared for this {_scope_name(message)}."
        if action == "local":
            if len(parts) >= 2 and parts[1] == "clear":
                self.db.set_scope_local_repo(
                    message.chat_id, message.thread_id, None, message.user_id
                )
                return f"Local repository cache cleared for this {_scope_name(message)}."
            if len(parts) == 3 and parts[1] == "set":
                return self._set_local_repo(message, parts[2])
        return (
            "Usage: /repo show | /repo check | /repo allow owner/repository | "
            "/repo set owner/repository | /repo setup owner/repository | "
            "/repo clear | /repo local set <absolute-path> | /repo local clear | "
            "/repo deploy enable|disable owner/repository | "
            "/repo deploy set owner/repository <workflow-name-or-file> | "
            "/repo deploy clear owner/repository"
        )

    async def _repo_setup(self, message: IncomingMessage, parts: list[str]) -> str:
        admin_error = self.permissions.require_admin(message.user_id)
        if admin_error:
            return admin_error
        if len(parts) != 1:
            return "Usage: /repo setup owner/repository"
        try:
            validate_repo(parts[0])
        except PlanValidationError as exc:
            return str(exc)
        return await self.repository_setup.start(message, parts[0])

    def _repo_deploy(self, message: IncomingMessage, parts: list[str]) -> str:
        admin_error = self.permissions.require_admin(message.user_id)
        if admin_error:
            return admin_error
        if len(parts) == 2 and parts[0] in {"enable", "disable"}:
            repo = parts[1]
            try:
                validate_repo(repo)
            except PlanValidationError as exc:
                return str(exc)
            if not self.db.is_repo_allowed(repo):
                return "Repo is not allowed. Admin must run: /repo allow owner/repository"
            enabled = parts[0] == "enable"
            self.db.set_repo_deploy_enabled(repo, enabled)
            state = "enabled" if enabled else "disabled"
            return f"Deployment {state} for {repo}."
        if len(parts) == 3 and parts[0] == "set":
            repo, workflow = parts[1], parts[2].strip()
            try:
                validate_repo(repo)
            except PlanValidationError as exc:
                return str(exc)
            if not self.db.is_repo_allowed(repo):
                return "Repo is not allowed. Admin must run: /repo allow owner/repository"
            if not workflow or len(workflow) > 256 or any(ord(char) < 32 for char in workflow):
                return "Deployment workflow must be a non-empty name or file up to 256 characters."
            self.db.set_repo_deploy_workflow(repo, workflow)
            return f"Deployment workflow set for {repo}: {workflow}"
        if len(parts) == 2 and parts[0] == "clear":
            repo = parts[1]
            try:
                validate_repo(repo)
                self.db.set_repo_deploy_workflow(repo, None)
            except (PlanValidationError, ValueError) as exc:
                return str(exc)
            return f"Deployment workflow cleared for {repo}."
        return (
            "Usage: /repo deploy enable|disable owner/repository | "
            "/repo deploy set owner/repository <workflow-name-or-file> | "
            "/repo deploy clear owner/repository"
        )

    def _repo_summary(self, message: IncomingMessage, *, check_cache: bool = False) -> str:
        chat = self.db.get_scope_settings(message.chat_id, message.thread_id)
        repo = str(chat.get("active_repo") or "")
        path = str(chat.get("local_repo_path") or "")
        if not path:
            cache = "not set"
        elif not repo:
            cache = f"{path} (active repo not set)"
        elif not check_cache:
            cache = f"{path} (configured; run /repo check to validate)"
        else:
            try:
                resolved = self.repositories.validate(path, repo)
                cache = f"{resolved} (ok)"
            except LocalRepositoryError as exc:
                cache = f"{path} (invalid: {exc})"
        return "\n".join(
            [
                f"Active repo: {repo or 'not set'}",
                f"Default branch: {chat.get('default_branch') or 'main'}",
                f"Local repo: {cache}",
                f"Deploy enabled: {'yes' if repo and self.db.is_repo_deploy_enabled(repo) else 'no'}",
                f"Deploy workflow: {self.db.get_repo_deploy_workflow(repo) or 'not set'}"
                if repo
                else "Deploy workflow: not set",
            ]
        )

    def _set_local_repo(self, message: IncomingMessage, path: str) -> str:
        chat = self.db.get_scope_settings(message.chat_id, message.thread_id)
        repo = str(chat.get("active_repo") or "")
        if not repo:
            return "Set the active repo before configuring its local cache."
        try:
            resolved = self.repositories.validate(path, repo)
        except LocalRepositoryError as exc:
            return f"Local repository cache not set.\nReason: {exc}"
        self.db.set_scope_local_repo(
            message.chat_id, message.thread_id, str(resolved), message.user_id
        )
        return f"Local repository cache set: {resolved}"

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
        branch = (
            self.db.get_scope_settings(message.chat_id, message.thread_id).get("default_branch")
            or "main"
        )
        self.db.set_scope_repo(
            message.chat_id, message.thread_id, repo, message.user_id, str(branch)
        )
        return f"Active repo set for this {_scope_name(message)}: {repo}"

    def repos(self) -> str:
        repos = self.db.list_allowed_repos()
        return "Allowed repos:\n" + (bullet_list(repos) if repos else "- none")

    async def issues(self, message: IncomingMessage, rest: str) -> str | OutgoingMessage:
        if rest:
            return "Usage: /issues"
        settings = self.db.get_scope_settings(message.chat_id, message.thread_id)
        repo = str(settings.get("active_repo") or "")
        if not repo:
            return f"No active repo for this {_scope_name(message)}. Admin: /repo set owner/repository"
        if not self.db.is_repo_allowed(repo):
            return "Active repo is not in allowed repo list. Admin: /repo allow owner/repository"
        try:
            issues = await asyncio.to_thread(self.issue_reader.list_open_issues, repo)
        except (GhError, ValueError) as exc:
            self.db.audit("issues.list", "failed", {"repo": repo, "error": str(exc)})
            return f"Issues not loaded.\nReason: {exc}"
        if not issues:
            return f"No open issues for {repo}."

        lines = [f"Open issues for {html.escape(repo)}:"]
        lines.extend(
            f'- <a href="{html.escape(issue.url, quote=True)}">#{issue.number}</a> — '
            f"{html.escape(issue.title)}"
            for issue in issues
        )
        return OutgoingMessage(text="\n".join(lines))

    def config(self, message: IncomingMessage, rest: str) -> str:
        parts = rest.split(maxsplit=2)
        admin_error = self.permissions.require_admin(message.user_id)
        if admin_error:
            return admin_error
        if not parts or parts[0] == "show":
            settings = self.db.all_settings()
            items = [f"{key}={value}" for key, value in settings.items()]
            for key in sorted(SECRET_CONFIG_KEYS):
                key_status = "<set>" if self.db.has_secret(key) else "<not set>"
                items.append(f"{key}={key_status}")
            return "Config:\n" + bullet_list(items)
        if len(parts) == 3 and parts[0] == "set":
            key, value = parts[1], parts[2]
            try:
                value = normalize_config_value(key, value)
            except ValueError as exc:
                return str(exc)
            if key in SECRET_CONFIG_KEYS:
                if not message.is_private:
                    return "API keys must be set in a private chat with the bot."
                self.db.set_secret(key, value)
            else:
                self.db.set_setting(key, value)
            return f"Config set: {key}"
        return "Usage: /config show | /config set <key> <value>"

    def memory(self, message: IncomingMessage, rest: str) -> str:
        action = rest.strip().lower() or "status"
        settings = self.db.get_scope_settings(message.chat_id, message.thread_id)
        repo = str(settings.get("active_repo") or "")
        session_id = memory_session_id(message.chat_id, message.thread_id)
        issue_session_id = (
            issue_memory_session_id(message.chat_id, message.thread_id, repo) if repo else ""
        )
        limit = int(self.db.get_setting("llm_memory_max_messages", str(DEFAULT_MEMORY_MAX_MESSAGES)))
        if action in {"status", "show"}:
            commit_count = self.db.count_llm_messages(session_id)
            issue_count = self.db.count_llm_messages(issue_session_id) if issue_session_id else 0
            return f"LLM memory: commits {commit_count}/{limit}; issues {issue_count}/{limit}."
        if action == "clear":
            admin_error = self.permissions.require_admin(message.user_id)
            if admin_error:
                return admin_error
            self.db.clear_llm_messages(session_id)
            if issue_session_id:
                self.db.clear_llm_messages(issue_session_id)
            return f"LLM memory cleared for this {_scope_name(message)}."
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
        chat = self.db.get_scope_settings(message.chat_id, message.thread_id)
        self.db.set_scope_repo(
            message.chat_id,
            message.thread_id,
            chat.get("active_repo"),
            message.user_id,
            branch,
        )
        return f"Default branch set: {branch}"

    def commit(self, message: IncomingMessage, rest: str) -> str:
        if not self.permissions.can_request_commit(message.user_id):
            return "Unauthorized. Admin role required."
        request_text = rest.strip()
        if not request_text:
            return "Usage: /commit <request>"
        chat = self.db.get_scope_settings(message.chat_id, message.thread_id)
        repo = chat.get("active_repo")
        if not repo:
            return f"No active repo for this {_scope_name(message)}. Admin: /repo set owner/repository"
        if not self.db.is_repo_allowed(repo):
            return "Active repo is not in allowed repo list. Admin: /repo allow owner/repository"
        branch = chat.get("default_branch") or "main"
        try:
            plan_id, plan = self.planner.create_plan(
                request_text=request_text,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
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
                f"Actual behavior: {plan.actual_behavior}",
                f"Expected behavior: {plan.expected_behavior}",
                "Files:",
                bullet_list(change.path for change in plan.changes),
                "",
                f"Run: /confirm {plan_id}",
                f"Cancel: /cancel {plan_id}",
            ]
        )

    def confirm(self, message: IncomingMessage, rest: str) -> str:
        plan_id = rest.strip()
        if not plan_id:
            return "Usage: /confirm <plan_id>"
        if not self.permissions.can_request_commit(message.user_id):
            return "Unauthorized. Admin role required."
        try:
            result = self.execution.execute(
                plan_id, message.user_id, message.chat_id, message.thread_id
            )
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
        if (
            int(record["telegram_chat_id"]) != message.chat_id
            or record.get("telegram_thread_id") != message.thread_id
        ):
            return "Plan belongs to a different chat or topic."
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


def _scope_name(message: IncomingMessage) -> str:
    return "topic" if message.thread_id is not None else "chat"
