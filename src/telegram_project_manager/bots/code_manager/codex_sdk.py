from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from openai_codex import ApprovalMode, AsyncCodex, LocalImageInput, Sandbox, TextInput
from openai_codex.client import CodexConfig
from openai_codex.types import ReasoningEffort

from telegram_project_manager.platform.config import CodexModelRole


ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]
ThreadCallback = Callable[[str], Awaitable[None]]
CODEX_JOB_SANDBOX = Sandbox.full_access


class CodexSdkError(RuntimeError):
    pass


class CodexSdkAdapter:
    """Small boundary around the beta SDK so the rest of the bot remains stable."""

    def __init__(
        self,
        api_key_provider: Callable[[], str],
        base_url_provider: Callable[[], str],
        model_provider: Callable[[CodexModelRole], str],
    ) -> None:
        self.api_key_provider = api_key_provider
        self.base_url_provider = base_url_provider
        self.model_provider = model_provider
        self._client: AsyncCodex | None = None
        self._start_lock = asyncio.Lock()
        self._active_turns: dict[str, Any] = {}

    async def ensure_started(self) -> AsyncCodex:
        if self._client is not None:
            return self._client
        async with self._start_lock:
            if self._client is not None:
                return self._client
            api_key = self.api_key_provider().strip()
            base_url = self.base_url_provider().strip().rstrip("/")
            if not api_key:
                raise CodexSdkError(
                    "Codex API key is not configured. Admin private chat: "
                    "/config set codex_api_key <key>"
                )
            if not base_url:
                raise CodexSdkError(
                    "Codex provider is incomplete. Configure codex_base_url."
                )
            client = AsyncCodex(_codex_config(api_key, base_url))
            try:
                await client.__aenter__()
                await client.login_api_key(api_key)
                account = await client.account(refresh_token=False)
                if not getattr(account, "account", None):
                    raise CodexSdkError("Codex authentication did not return an active account")
            except Exception as exc:
                await client.close()
                if isinstance(exc, CodexSdkError):
                    raise
                raise CodexSdkError(f"Codex authentication failed: {exc}") from exc
            self._client = client
            return client

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    async def interrupt(self, job_id: str) -> None:
        handle = self._active_turns.get(job_id)
        if handle is not None:
            try:
                await handle.interrupt()
            except Exception:
                pass

    async def run_turn(
        self,
        *,
        job_id: str,
        cwd: str,
        prompt: str,
        image_paths: tuple[str, ...] = (),
        output_schema: dict[str, Any],
        sandbox: Sandbox,
        effort: ReasoningEffort,
        model_role: CodexModelRole,
        developer_instructions: str,
        thread_id: str | None,
        timeout_seconds: int,
        on_progress: ProgressCallback,
        on_thread: ThreadCallback,
    ) -> tuple[str, dict[str, Any]]:
        resolved_thread_id, final_text = await self._run_final_text(
            job_id=job_id,
            cwd=cwd,
            prompt=prompt,
            image_paths=image_paths,
            output_schema=output_schema,
            sandbox=sandbox,
            effort=effort,
            model_role=model_role,
            developer_instructions=developer_instructions,
            thread_id=thread_id,
            timeout_seconds=timeout_seconds,
            on_progress=on_progress,
            on_thread=on_thread,
        )
        try:
            parsed = json.loads(final_text)
        except json.JSONDecodeError as exc:
            raise CodexSdkError("Codex final response was not valid structured JSON") from exc
        if not isinstance(parsed, dict):
            raise CodexSdkError("Codex final response must be a JSON object")
        return resolved_thread_id, parsed

    async def run_text_turn(
        self,
        *,
        job_id: str,
        cwd: str,
        prompt: str,
        image_paths: tuple[str, ...] = (),
        sandbox: Sandbox,
        effort: ReasoningEffort,
        model_role: CodexModelRole,
        developer_instructions: str,
        thread_id: str | None,
        timeout_seconds: int,
        on_progress: ProgressCallback,
        on_thread: ThreadCallback,
    ) -> tuple[str, str]:
        return await self._run_final_text(
            job_id=job_id,
            cwd=cwd,
            prompt=prompt,
            image_paths=image_paths,
            output_schema=None,
            sandbox=sandbox,
            effort=effort,
            model_role=model_role,
            developer_instructions=developer_instructions,
            thread_id=thread_id,
            timeout_seconds=timeout_seconds,
            on_progress=on_progress,
            on_thread=on_thread,
        )

    async def _run_final_text(
        self,
        *,
        job_id: str,
        cwd: str,
        prompt: str,
        image_paths: tuple[str, ...],
        output_schema: dict[str, Any] | None,
        sandbox: Sandbox,
        effort: ReasoningEffort,
        model_role: CodexModelRole,
        developer_instructions: str,
        thread_id: str | None,
        timeout_seconds: int,
        on_progress: ProgressCallback,
        on_thread: ThreadCallback,
    ) -> tuple[str, str]:
        model = self.model_provider(model_role).strip()
        if not model:
            raise CodexSdkError(
                f"Codex {model_role} model is not configured. Admin: "
                f"/config set codex_{model_role}_model <model> "
                "(or set codex_model as a shared fallback)."
            )
        client = await self.ensure_started()
        try:
            if thread_id:
                thread = await client.thread_resume(
                    thread_id,
                    approval_mode=ApprovalMode.auto_review,
                    cwd=cwd,
                    developer_instructions=developer_instructions,
                    model=model,
                    sandbox=sandbox,
                )
            else:
                thread = await client.thread_start(
                    approval_mode=ApprovalMode.auto_review,
                    cwd=cwd,
                    developer_instructions=developer_instructions,
                    model=model,
                    sandbox=sandbox,
                )
            await on_thread(str(thread.id))
            turn_options: dict[str, Any] = {
                "approval_mode": ApprovalMode.auto_review,
                "cwd": cwd,
                "effort": effort,
                "sandbox": sandbox,
            }
            if output_schema is not None:
                turn_options["output_schema"] = output_schema
            turn = await thread.turn(_turn_input(prompt, image_paths), **turn_options)
            self._active_turns[job_id] = turn
            final_text = ""
            final_status = ""
            async with asyncio.timeout(timeout_seconds):
                async for notification in turn.stream():
                    data = _notification_data(notification)
                    method = str(data.get("method") or "")
                    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
                    summary = _safe_progress(method, payload)
                    if summary:
                        await on_progress(summary)
                    if method == "item/completed":
                        item = _root_item(payload.get("item"))
                        if item.get("type") == "agentMessage" and item.get("text"):
                            final_text = str(item["text"])
                    elif method == "turn/completed":
                        turn_data = payload.get("turn") if isinstance(payload.get("turn"), dict) else {}
                        final_status = str(turn_data.get("status") or "")
            if final_status != "completed":
                raise CodexSdkError(f"Codex turn ended with status: {final_status or 'unknown'}")
            if not final_text.strip():
                raise CodexSdkError("Codex turn completed without a final response")
            return str(thread.id), final_text
        except TimeoutError as exc:
            await self.interrupt(job_id)
            raise CodexSdkError(f"Codex turn exceeded {timeout_seconds // 60} minutes") from exc
        except CodexSdkError:
            raise
        except Exception as exc:
            raise CodexSdkError(f"Codex SDK failed: {exc}") from exc
        finally:
            self._active_turns.pop(job_id, None)


def _turn_input(
    prompt: str, image_paths: tuple[str, ...]
) -> str | list[TextInput | LocalImageInput]:
    if not image_paths:
        return prompt
    return [TextInput(prompt), *(LocalImageInput(path) for path in image_paths)]


def _notification_data(notification: Any) -> dict[str, Any]:
    method = str(getattr(notification, "method", ""))
    payload = getattr(notification, "payload", None)
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json", by_alias=True)
    if not isinstance(payload, dict):
        payload = {}
    return {"method": method, "payload": payload}


def _root_item(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    root = value.get("root")
    return root if isinstance(root, dict) else value


def _safe_progress(method: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    if method == "turn/started":
        return {"kind": "phase", "text": "Codex turn started"}
    if method == "turn/plan/updated":
        plan = payload.get("plan") if isinstance(payload.get("plan"), list) else []
        safe_steps = []
        for item in plan[:8]:
            if isinstance(item, dict):
                safe_steps.append(
                    {
                        "step": str(item.get("step") or "")[:240],
                        "status": str(item.get("status") or "")[:32],
                    }
                )
        return {"kind": "plan", "steps": safe_steps}
    if method in {"item/started", "item/completed"}:
        item = _root_item(payload.get("item"))
        item_type = str(item.get("type") or "")
        state = "started" if method.endswith("started") else str(item.get("status") or "completed")
        if item_type == "commandExecution":
            command = item.get("command")
            if isinstance(command, list):
                command = " ".join(str(part) for part in command)
            return {"kind": "command", "text": _sanitize_text(str(command or "command"))[:240], "status": state}
        if item_type == "fileChange":
            paths = []
            changes = item.get("changes") if isinstance(item.get("changes"), list) else []
            for change in changes[:20]:
                if isinstance(change, dict) and change.get("path"):
                    paths.append(_sanitize_text(str(change["path"]))[:240])
            return {"kind": "files", "paths": paths, "status": state}
    if method == "error":
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        message = _sanitize_text(str(error.get("message") or "Codex error"))[:500]
        reconnect = re.fullmatch(r"Reconnecting\.\.\.\s*(\d+)/(\d+)", message, re.IGNORECASE)
        if reconnect:
            attempt, maximum = reconnect.groups()
            return {
                "kind": "connection",
                "text": (
                    "Codex provider stream interrupted; "
                    f"reconnecting {attempt}/{maximum} (job still running)"
                ),
            }
        details = [
            str(error.get(key) or "").strip()
            for key in ("code", "type", "status")
            if error.get(key)
        ]
        if details:
            message = f"{message} ({', '.join(details)})"[:500]
        return {"kind": "error", "text": message}
    if method == "turn/completed":
        turn = payload.get("turn") if isinstance(payload.get("turn"), dict) else {}
        return {"kind": "phase", "text": f"Codex turn {turn.get('status') or 'completed'}"}
    return None


def _sanitize_text(value: str) -> str:
    value = re.sub(
        r"sk-[A-Za-z0-9_-]+(?:\*+[A-Za-z0-9_-]+)?",
        "[REDACTED_API_KEY]",
        value,
    )
    value = re.sub(r"(?i)(authorization:\s*bearer\s+)\S+", r"\1[REDACTED]", value)
    return value


def _codex_config(api_key: str, base_url: str) -> CodexConfig:
    return CodexConfig(
        env={
            "OPENAI_API_KEY": api_key,
            "OPENAI_BASE_URL": base_url,
        },
        config_overrides=(
            f"openai_base_url={json.dumps(base_url)}",
            "sandbox_workspace_write.network_access=true",
        ),
    )
