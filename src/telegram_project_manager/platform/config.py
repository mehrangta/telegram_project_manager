from __future__ import annotations

from urllib.parse import urlsplit


SECRET_CONFIG_KEYS = frozenset({"openai_api_key", "codex_api_key"})

SUPPORTED_CONFIG_KEYS = frozenset(
    {
        "openai_api_key",
        "openai_base_url",
        "openai_model",
        "codex_api_key",
        "codex_base_url",
        "codex_model",
        "max_files_per_commit",
        "max_bytes_per_commit",
        "require_confirmation",
        "llm_memory_max_messages",
    }
)


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

    return normalized
