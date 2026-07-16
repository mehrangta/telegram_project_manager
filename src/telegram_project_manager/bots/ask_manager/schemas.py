from __future__ import annotations

from dataclasses import dataclass
from typing import Any


MAX_ANSWER_LENGTH = 2_800
MAX_SOURCES = 6
MAX_SOURCE_LENGTH = 180

ASK_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "maxLength": MAX_ANSWER_LENGTH},
        "sources": {
            "type": "array",
            "maxItems": MAX_SOURCES,
            "items": {"type": "string", "maxLength": MAX_SOURCE_LENGTH},
        },
    },
    "required": ["answer", "sources"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class AskResponse:
    answer: str
    sources: tuple[str, ...]

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "AskResponse":
        answer = str(raw.get("answer") or "").strip()
        if not answer:
            raise ValueError("Codex returned an empty repository answer")
        if len(answer) > MAX_ANSWER_LENGTH:
            answer = answer[: MAX_ANSWER_LENGTH - 16].rstrip() + "\n... truncated"
        raw_sources = raw.get("sources")
        if not isinstance(raw_sources, list):
            raise ValueError("Codex repository answer has invalid sources")
        sources: list[str] = []
        seen: set[str] = set()
        for item in raw_sources:
            source = str(item or "").strip().replace("\\", "/")
            if not source or source in seen:
                continue
            seen.add(source)
            sources.append(source[:MAX_SOURCE_LENGTH])
            if len(sources) == MAX_SOURCES:
                break
        return cls(answer=answer, sources=tuple(sources))
