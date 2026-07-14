from __future__ import annotations

import json

from langchain_openai import ChatOpenAI
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from telegram_project_manager.platform.config import normalize_config_value
from telegram_project_manager.platform.llm.memory import DEFAULT_MEMORY_MAX_MESSAGES, SQLiteChatMessageHistory
from telegram_project_manager.platform.storage.db import Database


class LlmError(RuntimeError):
    pass


COMMIT_PLAN_RESPONSE_SCHEMA = {
    "title": "commit_plan",
    "description": "A safe GitHub commit plan generated from the user's request.",
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["create_commit"]},
        "repo": {"type": "string"},
        "base_branch": {"type": "string"},
        "target_branch": {"type": "string"},
        "commit_message": {"type": "string"},
        "actual_behavior": {"type": "string"},
        "expected_behavior": {"type": "string"},
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "operation": {
                        "type": "string",
                        "enum": ["create", "update", "create_or_update"],
                    },
                    "content": {"type": "string"},
                },
                "required": ["path", "operation", "content"],
                "additionalProperties": False,
            },
        },
        "github_comment": {"type": "string"},
        "requires_confirmation": {"type": "boolean"},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "intent",
        "repo",
        "base_branch",
        "target_branch",
        "commit_message",
        "actual_behavior",
        "expected_behavior",
        "changes",
        "github_comment",
        "requires_confirmation",
        "questions",
    ],
    "additionalProperties": False,
}


class OpenAICompatibleClient:
    def __init__(self, db: Database) -> None:
        self.db = db

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        memory_key: str | None = None,
        response_schema: dict | None = None,
    ) -> dict:
        configured_base_url = self.db.get_setting("openai_base_url", "https://api.openai.com/v1")
        try:
            base_url = normalize_config_value("openai_base_url", configured_base_url)
        except ValueError as exc:
            raise LlmError(f"Invalid OpenAI base URL: {exc}") from exc
        model = self.db.get_setting("openai_model", "")
        if not model:
            raise LlmError("OpenAI model is not configured. Admin: /config set openai_model <model>")
        api_key = self.db.get_secret("openai_api_key")
        if not api_key:
            raise LlmError(
                "OpenAI API key is not configured. Admin private chat: /config set openai_api_key <key>"
            )
        history = None
        try:
            llm = ChatOpenAI(
                model=model,
                api_key=api_key,
                base_url=base_url,
                temperature=0.1,
                timeout=90,
                max_retries=2,
            ).with_structured_output(
                response_schema or COMMIT_PLAN_RESPONSE_SCHEMA,
                method="json_schema",
                include_raw=True,
            )
            if memory_key:
                max_messages = self._memory_limit()
                history = SQLiteChatMessageHistory(self.db, memory_key, max_messages)
                prompt = ChatPromptTemplate.from_messages(
                    [
                        ("system", "{system_prompt}"),
                        MessagesPlaceholder(variable_name="history"),
                        ("human", "{user_prompt}"),
                    ]
                )
                prompt_value = prompt.invoke(
                    {
                        "system_prompt": system_prompt,
                        "history": history.messages,
                        "user_prompt": user_prompt,
                    }
                )
                response = llm.invoke(prompt_value)
            else:
                response = llm.invoke(
                    [
                        ("system", system_prompt),
                        ("human", user_prompt),
                    ]
                )
        except Exception as exc:
            raise LlmError(f"LLM request failed: {exc}") from exc

        if not isinstance(response, dict):
            raise LlmError("LLM structured response is invalid")
        parsing_error = response.get("parsing_error")
        if parsing_error:
            raise LlmError(f"LLM returned invalid structured output: {parsing_error}")
        parsed = response.get("parsed")
        if hasattr(parsed, "model_dump"):
            parsed = parsed.model_dump()
        if not isinstance(parsed, dict):
            raise LlmError("LLM structured response missing parsed object")
        if history is not None:
            raw = response.get("raw")
            content = raw.content if isinstance(raw, AIMessage) and isinstance(raw.content, str) else json.dumps(parsed)
            history.add_messages([HumanMessage(content=user_prompt), AIMessage(content=content)])
        return parsed

    def _memory_limit(self) -> int:
        raw = self.db.get_setting("llm_memory_max_messages", str(DEFAULT_MEMORY_MAX_MESSAGES))
        try:
            limit = int(raw)
        except ValueError as exc:
            raise LlmError("LLM memory limit must be an integer.") from exc
        if limit < 2 or limit % 2:
            raise LlmError("LLM memory limit must be an even number of at least 2 messages.")
        return limit


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
