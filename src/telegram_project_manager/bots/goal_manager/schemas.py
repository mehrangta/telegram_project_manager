from __future__ import annotations

from dataclasses import dataclass
from typing import Any

GOAL_SUMMARY_MAX_LENGTH = 1200
GOAL_NEXT_STEP_MAX_LENGTH = 600

GOAL_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["continue", "complete", "blocked"]},
        "summary": {"type": "string", "maxLength": GOAL_SUMMARY_MAX_LENGTH},
        "next_step": {"type": "string", "maxLength": GOAL_NEXT_STEP_MAX_LENGTH},
    },
    "required": ["status", "summary", "next_step"],
    "additionalProperties": False,
}


class GoalValidationError(ValueError):
    pass


@dataclass(frozen=True)
class GoalTurnResult:
    status: str
    summary: str
    next_step: str

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "GoalTurnResult":
        status = str(raw.get("status") or "").strip().lower()
        if status not in {"continue", "complete", "blocked"}:
            raise GoalValidationError("Codex returned an invalid goal status")
        summary = " ".join(str(raw.get("summary") or "").split())
        next_step = " ".join(str(raw.get("next_step") or "").split())
        if not summary:
            raise GoalValidationError("Codex returned an empty goal summary")
        if len(summary) > GOAL_SUMMARY_MAX_LENGTH:
            raise GoalValidationError("Codex goal summary is too long")
        if len(next_step) > GOAL_NEXT_STEP_MAX_LENGTH:
            raise GoalValidationError("Codex goal next step is too long")
        if status == "continue" and not next_step:
            raise GoalValidationError("A continuing goal requires a next step")
        return cls(status=status, summary=summary, next_step=next_step)
