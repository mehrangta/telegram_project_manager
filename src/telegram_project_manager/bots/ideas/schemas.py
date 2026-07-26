from __future__ import annotations

from dataclasses import dataclass
from typing import Any

IMPROVEMENT_COUNT = 3
MAX_TITLE_LENGTH = 100
MAX_DETAIL_LENGTH = 650
MAX_SOURCES = 4
MAX_SOURCE_LENGTH = 180

IMPROVEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": MAX_TITLE_LENGTH},
        "problem": {"type": "string", "maxLength": MAX_DETAIL_LENGTH},
        "recommendation": {"type": "string", "maxLength": MAX_DETAIL_LENGTH},
        "benefit": {"type": "string", "maxLength": MAX_DETAIL_LENGTH},
        "sources": {
            "type": "array",
            "maxItems": MAX_SOURCES,
            "items": {"type": "string", "maxLength": MAX_SOURCE_LENGTH},
        },
    },
    "required": ["title", "problem", "recommendation", "benefit", "sources"],
    "additionalProperties": False,
}

BRAINSTORM_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "improvements": {
            "type": "array",
            "minItems": IMPROVEMENT_COUNT,
            "maxItems": IMPROVEMENT_COUNT,
            "items": IMPROVEMENT_SCHEMA,
        }
    },
    "required": ["improvements"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Improvement:
    title: str
    problem: str
    recommendation: str
    benefit: str
    sources: tuple[str, ...]


@dataclass(frozen=True)
class BrainstormResponse:
    improvements: tuple[Improvement, ...]

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "BrainstormResponse":
        items = raw.get("improvements")
        if not isinstance(items, list) or len(items) != IMPROVEMENT_COUNT:
            raise ValueError("Codex must return exactly 3 repository improvements")
        improvements: list[Improvement] = []
        titles: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Codex returned an invalid repository improvement")
            title = _required(item, "title", MAX_TITLE_LENGTH)
            normalized_title = title.casefold()
            if normalized_title in titles:
                raise ValueError("Codex returned duplicate repository improvements")
            titles.add(normalized_title)
            raw_sources = item.get("sources")
            if not isinstance(raw_sources, list):
                raise ValueError("Codex improvement has invalid sources")
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
            improvements.append(
                Improvement(
                    title=title,
                    problem=_required(item, "problem", MAX_DETAIL_LENGTH),
                    recommendation=_required(item, "recommendation", MAX_DETAIL_LENGTH),
                    benefit=_required(item, "benefit", MAX_DETAIL_LENGTH),
                    sources=tuple(sources),
                )
            )
        return cls(improvements=tuple(improvements))


def _required(item: dict[str, Any], key: str, limit: int) -> str:
    value = str(item.get(key) or "").strip()
    if not value:
        raise ValueError(f"Codex improvement has an empty {key}")
    return value[:limit]
