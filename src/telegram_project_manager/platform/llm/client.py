from __future__ import annotations

import json
import urllib.error
import urllib.request

from telegram_project_manager.platform.config import normalize_config_value
from telegram_project_manager.platform.secrets import SecretStore
from telegram_project_manager.platform.storage.db import Database


class LlmError(RuntimeError):
    pass


class OpenAICompatibleClient:
    def __init__(self, db: Database, secrets: SecretStore) -> None:
        self.db = db
        self.secrets = secrets

    def chat_json(self, system_prompt: str, user_prompt: str) -> dict:
        configured_base_url = self.secrets.get("OPENAI_BASE_URL") or self.db.get_setting(
            "openai_base_url", "https://api.openai.com/v1"
        )
        try:
            base_url = normalize_config_value("openai_base_url", configured_base_url)
        except ValueError as exc:
            raise LlmError(f"Invalid OpenAI base URL: {exc}") from exc
        model = self.db.get_setting("openai_model", "")
        if not model:
            raise LlmError("OpenAI model is not configured. Admin: /config set openai_model <model>")
        api_key = self.secrets.require("OPENAI_API_KEY")
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise LlmError(f"LLM HTTP {exc.code}: {details[:500]}") from exc
        except OSError as exc:
            raise LlmError(f"LLM request failed: {exc}") from exc

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError("LLM response missing message content") from exc
        return parse_json_object(content)


def parse_json_object(content: str) -> dict:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LlmError("LLM returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise LlmError("LLM JSON root must be an object")
    return value
