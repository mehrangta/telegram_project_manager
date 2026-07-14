from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from openai_codex import ApprovalMode, AsyncCodex, Sandbox
from openai_codex.types import ReasoningEffort


ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]
ThreadCallback = Callable[[str], Awaitable[None]]


class CodexSdkError(RuntimeError):
    pass


class CodexSdkAdapter:
    """Small boundary around the beta SDK so the rest of the bot remains stable."""

    def __init__(self, api_key_provider: Callable[[], str]) -> None:
        self.api_key_provider = api_key_provider
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
            if not api_key:
                raise CodexSdkError(
                    "OpenAI API key is not configured. Admin private chat: "
                    "/config set openai_api_key <key>"
                )
            client = AsyncCodex()
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
        output_schema: dict[str, Any],
        sandbox: Sandbox,
        effort: ReasoningEffort,
        developer_instructions: str,
        thread_id: str | None,
        timeout_seconds: int,
        on_progress: ProgressCallback,
        on_thread: ThreadCallback,
    ) -> tuple[str, dict[str, Any]]:
        client = await self.ensure_started()
        try:
            if thread_id:
                thread = await client.thread_resume(
                    thread_id,
                    approval_mode=ApprovalMode.auto_review,
                    cwd=cwd,
                    developer_instructions=developer_instructions,
                    sandbox=sandbox,
                )
            else:
                thread = await client.thread_start(
                    approval_mode=ApprovalMode.auto_review,
                    cwd=cwd,
                    developer_instructions=developer_instructions,
                    sandbox=sandbox,
                )
            await on_thread(str(thread.id))
            turn = await thread.turn(
                prompt,
                approval_mode=ApprovalMode.auto_review,
                cwd=cwd,
                effort=effort,
                output_schema=output_schema,
                sandbox=sandbox,
            )
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
            try:
                parsed = json.loads(final_text)
            except json.JSONDecodeError as exc:
                raise CodexSdkError("Codex final response was not valid structured JSON") from exc
            if not isinstance(parsed, dict):
                raise CodexSdkError("Codex final response must be a JSON object")
            return str(thread.id), parsed
        except TimeoutError as exc:
            await self.interrupt(job_id)
            raise CodexSdkError(f"Codex turn exceeded {timeout_seconds // 60} minutes") from exc
        except CodexSdkError:
            raise
        except Exception as exc:
            raise CodexSdkError(f"Codex SDK failed: {exc}") from exc
        finally:
            self._active_turns.pop(job_id, None)


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
            return {"kind": "command", "text": str(command or "command")[:240], "status": state}
        if item_type == "fileChange":
            paths = []
            changes = item.get("changes") if isinstance(item.get("changes"), list) else []
            for change in changes[:20]:
                if isinstance(change, dict) and change.get("path"):
                    paths.append(str(change["path"])[:240])
            return {"kind": "files", "paths": paths, "status": state}
    if method == "error":
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        return {"kind": "error", "text": str(error.get("message") or "Codex error")[:500]}
    if method == "turn/completed":
        turn = payload.get("turn") if isinstance(payload.get("turn"), dict) else {}
        return {"kind": "phase", "text": f"Codex turn {turn.get('status') or 'completed'}"}
    return None
