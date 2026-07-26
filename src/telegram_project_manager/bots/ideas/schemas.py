from __future__ import annotations

from dataclasses import dataclass
from typing import Any

IDEA_COUNT = 3
MAX_TITLE_LENGTH = 100
MAX_OPPORTUNITY_LENGTH = 240
MAX_PROPOSAL_LENGTH = 280
MAX_VALUE_LENGTH = 200
MAX_SOURCES = 3
MAX_SOURCE_LENGTH = 90

IDEA_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": MAX_TITLE_LENGTH},
        "opportunity": {"type": "string", "maxLength": MAX_OPPORTUNITY_LENGTH},
        "proposal": {"type": "string", "maxLength": MAX_PROPOSAL_LENGTH},
        "value": {"type": "string", "maxLength": MAX_VALUE_LENGTH},
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
            if len(raw_sources) > MAX_SOURCES:
                raise ValueError(f"Codex idea has more than {MAX_SOURCES} sources")
            sources: list[str] = []
            seen: set[str] = set()
            for source in raw_sources:
                normalized = str(source or "").strip().replace("\\", "/")
                if not normalized or normalized in seen:
                    continue
                if len(normalized) > MAX_SOURCE_LENGTH:
                    raise ValueError(
                        f"Codex idea source exceeds {MAX_SOURCE_LENGTH} characters"
                    )
                seen.add(normalized)
                sources.append(normalized)
            ideas.append(
                BrainstormIdea(
                    title=title,
                    opportunity=_required_detail(
                        item, "opportunity", MAX_OPPORTUNITY_LENGTH
                    ),
                    proposal=_required_detail(item, "proposal", MAX_PROPOSAL_LENGTH),
                    value=_required_detail(item, "value", MAX_VALUE_LENGTH),
                    sources=tuple(sources),
                )
            )
        return cls(ideas=tuple(ideas))


def _required(item: dict[str, Any], key: str, limit: int) -> str:
    value = " ".join(str(item.get(key) or "").split())
    if not value:
        raise ValueError(f"Codex idea has an empty {key}")
    if len(value) > limit:
        raise ValueError(f"Codex idea {key} exceeds {limit} characters")
    return value


def _required_detail(item: dict[str, Any], key: str, limit: int) -> str:
    value = _required(item, key, limit)
    if value.endswith(("...", "…")):
        raise ValueError(f"Codex idea has an incomplete {key}")
    return value
