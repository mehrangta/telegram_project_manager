from __future__ import annotations

from dataclasses import dataclass
from typing import Any


ISSUE_DRAFT_RESPONSE_SCHEMA = {
    "title": "issue_draft",
    "description": "An improved GitHub issue draft.",
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "actual_behavior": {"type": "string"},
        "expected_behavior": {"type": "string"},
    },
    "required": ["title", "summary", "actual_behavior", "expected_behavior"],
    "additionalProperties": False,
}


class IssueDraftValidationError(ValueError):
    pass


@dataclass(frozen=True)
class IssueDraft:
    title: str
    summary: str
    actual_behavior: str
    expected_behavior: str

    @classmethod
    def from_llm(cls, raw: dict[str, Any]) -> "IssueDraft":
        draft = cls(
            title=str(raw.get("title") or "").strip(),
            summary=str(raw.get("summary") or "").strip(),
            actual_behavior=str(raw.get("actual_behavior") or "").strip(),
            expected_behavior=str(raw.get("expected_behavior") or "").strip(),
        )
        draft.validate()
        return draft

    def validate(self) -> None:
        if not self.title:
            raise IssueDraftValidationError("issue title is required")
        if len(self.title) > 256:
            raise IssueDraftValidationError("issue title must be 256 characters or fewer")
        if not self.summary:
            raise IssueDraftValidationError("issue summary is required")
        if not self.actual_behavior:
            raise IssueDraftValidationError("actual behavior is required")
        if not self.expected_behavior:
            raise IssueDraftValidationError("expected behavior is required")

    def to_json(self) -> dict[str, str]:
        return {
            "title": self.title,
            "summary": self.summary,
            "actual_behavior": self.actual_behavior,
            "expected_behavior": self.expected_behavior,
        }

    def body(self, image_links: list[str], marker: str) -> str:
        sections = [
            "## Summary",
            "",
            self.summary,
            "",
            "## Actual behavior",
            "",
            self.actual_behavior,
            "",
            "## Expected behavior",
            "",
            self.expected_behavior,
        ]
        if image_links:
            sections.extend(["", "## Images", "", *image_links])
        sections.extend(["", marker])
        return chr(10).join(sections)
