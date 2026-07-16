from __future__ import annotations

from dataclasses import dataclass
from typing import Any


PLAN_STEP_TITLE_MAX_LENGTH = 160
PLAN_STEP_DETAILS_MAX_LENGTH = 800
PLAN_QUESTION_MAX_LENGTH = 500


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
                    "title": {"type": "string", "maxLength": PLAN_STEP_TITLE_MAX_LENGTH},
                    "details": {"type": "string", "maxLength": PLAN_STEP_DETAILS_MAX_LENGTH},
                    "files": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
                },
                "required": ["title", "details", "files"],
                "additionalProperties": False,
            },
        },
        "tests": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "risks": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "questions": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "maxLength": PLAN_QUESTION_MAX_LENGTH},
                    "options": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 240},
                        "maxItems": 3,
                    },
                    "recommended_option": {"type": "string", "maxLength": 240},
                },
                "required": ["prompt", "options", "recommended_option"],
                "additionalProperties": False,
            },
        },
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
class PlanQuestion:
    prompt: str
    options: tuple[str, ...] = ()
    recommended_option: str = ""

    def render(self, index: int) -> list[str]:
        lines = [f"{index}. {self.prompt}"]
        for option_index, option in enumerate(self.options):
            label = chr(ord("A") + option_index)
            suffix = " (recommended)" if option == self.recommended_option else ""
            lines.append(f"   {label}. {option}{suffix}")
        return lines


@dataclass(frozen=True)
class CodePlan:
    summary: str
    steps: tuple[PlanStep, ...]
    tests: tuple[str, ...]
    risks: tuple[str, ...]
    questions: tuple[PlanQuestion, ...]

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "CodePlan":
        steps = tuple(
            PlanStep(
                title=str(item.get("title") or "").strip()[:PLAN_STEP_TITLE_MAX_LENGTH],
                details=str(item.get("details") or "").strip()[:PLAN_STEP_DETAILS_MAX_LENGTH],
                files=tuple(str(path).strip() for path in item.get("files") or [] if str(path).strip()),
            )
            for item in raw.get("steps") or []
            if isinstance(item, dict)
        )
        questions = []
        for item in raw.get("questions") or []:
            if isinstance(item, str):
                prompt = item.strip()[:PLAN_QUESTION_MAX_LENGTH]
                if prompt:
                    questions.append(PlanQuestion(prompt))
            elif isinstance(item, dict):
                prompt = str(item.get("prompt") or "").strip()[:PLAN_QUESTION_MAX_LENGTH]
                options = tuple(
                    str(option).strip()[:240]
                    for option in item.get("options") or []
                    if str(option).strip()
                )[:3]
                recommended = str(item.get("recommended_option") or "").strip()[:240]
                if prompt:
                    questions.append(PlanQuestion(prompt, options, recommended))
        plan = cls(
            summary=str(raw.get("summary") or "").strip(),
            steps=steps,
            tests=tuple(str(item).strip() for item in raw.get("tests") or [] if str(item).strip()),
            risks=tuple(str(item).strip() for item in raw.get("risks") or [] if str(item).strip()),
            questions=tuple(questions[:3]),
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
            if (
                len(step.title) > PLAN_STEP_TITLE_MAX_LENGTH
                or len(step.details) > PLAN_STEP_DETAILS_MAX_LENGTH
            ):
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
            "questions": [
                {
                    "prompt": item.prompt,
                    "options": list(item.options),
                    "recommended_option": item.recommended_option,
                }
                for item in self.questions
            ],
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
            lines.extend(["", "## Open questions", ""])
            for index, question in enumerate(self.questions, 1):
                lines.extend(question.render(index))
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
        if any(item.status == "passed" for item in self.tests):
            return
        failed = next((item for item in self.tests if item.status == "failed"), None)
        if failed is not None:
            command = failed.command[:240] or "unspecified command"
            summary = failed.summary[:500] or "No failure summary was provided."
            raise CodeJobValidationError(
                f"Codex reported failed validation command: {command} — {summary}"
            )
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
