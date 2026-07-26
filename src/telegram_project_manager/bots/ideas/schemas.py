from __future__ import annotations

from dataclasses import dataclass
from typing import Any

IDEA_COUNT = 3
MAX_TITLE_LENGTH = 100
MAX_DETAIL_LENGTH = 650
MAX_SOURCES = 4
MAX_SOURCE_LENGTH = 180

IDEA_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": MAX_TITLE_LENGTH},
        "opportunity": {"type": "string", "maxLength": MAX_DETAIL_LENGTH},
        "proposal": {"type": "string", "maxLength": MAX_DETAIL_LENGTH},
        "value": {"type": "string", "maxLength": MAX_DETAIL_LENGTH},
        "sources": {
            "type": "array",
            "maxItems": MAX_SOURCES,
            "items": {"type": "string", "maxLength": MAX_SOURCE_LENGTH},
        },
    },
    "required": ["title", "opportunity", "proposal", "value", "sources"],
    "additionalProperties": False,
}

BRAINSTORM_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "ideas": {
            "type": "array",
            "minItems": IDEA_COUNT,
            "maxItems": IDEA_COUNT,
            "items": IDEA_SCHEMA,
        }
    },
    "required": ["ideas"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class BrainstormIdea:
    title: str
    opportunity: str
    proposal: str
    value: str
    sources: tuple[str, ...]


@dataclass(frozen=True)
class BrainstormResponse:
    ideas: tuple[BrainstormIdea, ...]

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "BrainstormResponse":
        items = raw.get("ideas")
        if not isinstance(items, list) or len(items) != IDEA_COUNT:
            raise ValueError("Codex must return exactly 3 repository ideas")
        ideas: list[BrainstormIdea] = []
        titles: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Codex returned an invalid repository idea")
            title = _required(item, "title", MAX_TITLE_LENGTH)
            normalized_title = title.casefold()
            if normalized_title in titles:
                raise ValueError("Codex returned duplicate repository ideas")
            titles.add(normalized_title)
            raw_sources = item.get("sources")
            if not isinstance(raw_sources, list):
                raise ValueError("Codex idea has invalid sources")
            sources: list[str] = []
            seen: set[str] = set()
            for source in raw_sources:
                normalized = str(source or "").strip().replace("\\", "/")
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                sources.append(normalized[:MAX_SOURCE_LENGTH])
                if len(sources) == MAX_SOURCES:
                    break
            ideas.append(
                BrainstormIdea(
                    title=title,
                    opportunity=_required(item, "opportunity", MAX_DETAIL_LENGTH),
                    proposal=_required(item, "proposal", MAX_DETAIL_LENGTH),
                    value=_required(item, "value", MAX_DETAIL_LENGTH),
                    sources=tuple(sources),
                )
            )
        return cls(ideas=tuple(ideas))


def _required(item: dict[str, Any], key: str, limit: int) -> str:
    value = str(item.get(key) or "").strip()
    if not value:
        raise ValueError(f"Codex idea has an empty {key}")
    return value[:limit]
