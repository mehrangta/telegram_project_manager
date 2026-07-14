from __future__ import annotations

from dataclasses import dataclass
from typing import Any


CODE_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "details": {"type": "string"},
                    "files": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
                },
                "required": ["title", "details", "files"],
                "additionalProperties": False,
            },
        },
        "tests": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "risks": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "questions": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
    },
    "required": ["summary", "steps", "tests", "risks", "questions"],
    "additionalProperties": False,
}


CODE_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "commit_message": {"type": "string"},
        "tests": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "status": {"type": "string", "enum": ["passed", "failed", "not_run"]},
                    "summary": {"type": "string"},
                },
                "required": ["command", "status", "summary"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "commit_message", "tests"],
    "additionalProperties": False,
}


class CodeJobValidationError(ValueError):
    pass


@dataclass(frozen=True)
class PlanStep:
    title: str
    details: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class CodePlan:
    summary: str
    steps: tuple[PlanStep, ...]
    tests: tuple[str, ...]
    risks: tuple[str, ...]
    questions: tuple[str, ...]

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "CodePlan":
        steps = tuple(
            PlanStep(
                title=str(item.get("title") or "").strip(),
                details=str(item.get("details") or "").strip(),
                files=tuple(str(path).strip() for path in item.get("files") or [] if str(path).strip()),
            )
            for item in raw.get("steps") or []
            if isinstance(item, dict)
        )
        plan = cls(
            summary=str(raw.get("summary") or "").strip(),
            steps=steps,
            tests=tuple(str(item).strip() for item in raw.get("tests") or [] if str(item).strip()),
            risks=tuple(str(item).strip() for item in raw.get("risks") or [] if str(item).strip()),
            questions=tuple(str(item).strip() for item in raw.get("questions") or [] if str(item).strip()),
        )
        plan.validate()
        return plan

    def validate(self) -> None:
        if not self.summary:
            raise CodeJobValidationError("plan summary is required")
        if not self.steps or len(self.steps) > 8:
            raise CodeJobValidationError("plan requires 1 to 8 steps")
        for step in self.steps:
            if not step.title or not step.details:
                raise CodeJobValidationError("every plan step requires a title and details")
            if len(step.title) > 160 or len(step.details) > 800:
                raise CodeJobValidationError("plan step is too long")

    def to_json(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "steps": [
                {"title": step.title, "details": step.details, "files": list(step.files)}
                for step in self.steps
            ],
            "tests": list(self.tests),
            "risks": list(self.risks),
            "questions": list(self.questions),
        }

    def to_markdown(self, job_id: str, repo: str, issue_number: int, revision: int) -> str:
        lines = [
            f"# Codex plan for {repo}#{issue_number}",
            "",
            f"Job: `{job_id}` · Revision: {revision}",
            "",
            self.summary,
            "",
            "## Steps",
            "",
        ]
        for index, step in enumerate(self.steps, 1):
            lines.append(f"{index}. **{step.title}** — {step.details}")
            if step.files:
                paths = ", ".join(f"`{path}`" for path in step.files)
                lines.append(f"   - Likely files: {paths}")
        lines.extend(["", "## Validation", ""])
        lines.extend(f"- {item}" for item in self.tests)
        if self.risks:
            lines.extend(["", "## Risks", "", *(f"- {item}" for item in self.risks)])
        if self.questions:
            lines.extend(["", "## Open questions", "", *(f"- {item}" for item in self.questions)])
        return "\n".join(lines).strip() + "\n"


@dataclass(frozen=True)
class TestResult:
    command: str
    status: str
    summary: str


@dataclass(frozen=True)
class CodeResult:
    summary: str
    commit_message: str
    tests: tuple[TestResult, ...]

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "CodeResult":
        result = cls(
            summary=str(raw.get("summary") or "").strip(),
            commit_message=str(raw.get("commit_message") or "").strip(),
            tests=tuple(
                TestResult(
                    command=str(item.get("command") or "").strip(),
                    status=str(item.get("status") or "").strip(),
                    summary=str(item.get("summary") or "").strip(),
                )
                for item in raw.get("tests") or []
                if isinstance(item, dict)
            ),
        )
        result.validate()
        return result

    def validate(self) -> None:
        if not self.summary or not self.commit_message:
            raise CodeJobValidationError("Codex coding result requires summary and commit message")
        if len(self.commit_message) > 200:
            raise CodeJobValidationError("commit message is too long")
        if any(item.status not in {"passed", "failed", "not_run"} for item in self.tests):
            raise CodeJobValidationError("invalid test status")
        if any(item.status == "failed" for item in self.tests):
            raise CodeJobValidationError("Codex reported a failed validation command")
        if not any(item.status == "passed" for item in self.tests):
            raise CodeJobValidationError("Codex did not report a successful validation command")

    def to_json(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "commit_message": self.commit_message,
            "tests": [
                {"command": item.command, "status": item.status, "summary": item.summary}
                for item in self.tests
            ],
        }
