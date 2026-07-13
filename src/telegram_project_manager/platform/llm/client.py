from __future__ import annotations

import json

from langchain_openai import ChatOpenAI

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
        try:
            llm = ChatOpenAI(
                model=model,
                api_key=api_key,
                base_url=base_url,
                temperature=0.1,
                timeout=90,
                max_retries=2,
            ).bind(response_format={"type": "json_object"})
            response = llm.invoke(
                [
                    ("system", system_prompt),
                    ("human", user_prompt),
                ]
            )
        except Exception as exc:
            raise LlmError(f"LLM request failed: {exc}") from exc

        content = response.content
        if not isinstance(content, str):
            raise LlmError("LLM response missing text content")
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
