from __future__ import annotations

from urllib.parse import urlsplit


SUPPORTED_CONFIG_KEYS = frozenset(
    {
        "openai_base_url",
        "openai_model",
        "max_files_per_commit",
        "max_bytes_per_commit",
        "require_confirmation",
    }
)


def normalize_config_value(key: str, value: str) -> str:
    if key not in SUPPORTED_CONFIG_KEYS:
        raise ValueError("Unsupported config key.")

    normalized = value.strip()
    if not normalized:
        raise ValueError("Config value cannot be empty.")

    if key == "openai_base_url":
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("OpenAI base URL must be an absolute HTTP or HTTPS URL.")
        normalized = normalized.rstrip("/")

    return normalized
