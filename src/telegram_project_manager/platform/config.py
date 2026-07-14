from __future__ import annotations

from collections.abc import Callable
from typing import Literal
from urllib.parse import urlsplit


CodexModelRole = Literal["plan", "code"]

SECRET_CONFIG_KEYS = frozenset({"openai_api_key", "codex_api_key"})

SUPPORTED_CONFIG_KEYS = frozenset(
    {
        "openai_api_key",
        "openai_base_url",
        "openai_model",
        "codex_api_key",
        "codex_base_url",
        "codex_model",
        "codex_plan_model",
        "codex_code_model",
        "max_files_per_commit",
        "max_bytes_per_commit",
        "require_confirmation",
        "issue_body_llm_enabled",
        "llm_memory_max_messages",
    }
)


def resolve_codex_model(
    setting_provider: Callable[[str, str], str],
    role: CodexModelRole,
) -> str:
    """Return the phase-specific Codex model, falling back to the legacy setting."""
    fallback = setting_provider("codex_model", "")
    return setting_provider(f"codex_{role}_model", fallback).strip()


def normalize_config_value(key: str, value: str) -> str:
    if key not in SUPPORTED_CONFIG_KEYS:
        raise ValueError("Unsupported config key.")

    normalized = value.strip()
    if not normalized:
        raise ValueError("Config value cannot be empty.")

    if key in {"openai_base_url", "codex_base_url"}:
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Base URL must be an absolute HTTP or HTTPS URL.")
        normalized = normalized.rstrip("/")

    if key == "llm_memory_max_messages":
        try:
            message_limit = int(normalized)
        except ValueError as exc:
            raise ValueError("LLM memory limit must be an integer.") from exc
        if message_limit < 2:
            raise ValueError("LLM memory limit must be at least 2 messages.")
        if message_limit % 2:
            raise ValueError("LLM memory limit must be an even number of messages.")
        normalized = str(message_limit)

    if key == "issue_body_llm_enabled":
        normalized = normalized.lower()
        if normalized not in {"true", "false"}:
            raise ValueError("Issue body LLM setting must be true or false.")

    return normalized
