from __future__ import annotations

from collections.abc import Iterable


def bullet_list(items: Iterable[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def truncate(value: str, limit: int = 3500) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 20].rstrip() + "\n... truncated ..."

