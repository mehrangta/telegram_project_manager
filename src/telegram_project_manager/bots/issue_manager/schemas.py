from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote


ISSUE_DRAFT_RESPONSE_SCHEMA = {
    "title": "issue_draft",
    "description": "An improved GitHub issue draft.",
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "actual_behavior": {"type": "string"},
        "expected_behavior": {"type": "string"},
        "codebase_context": {"type": "string"},
        "relevant_files": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["path", "reason"],
                "additionalProperties": False,
            },
        },
        "possible_causes": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "hypothesis": {"type": "string"},
                    "evidence_paths": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["hypothesis", "evidence_paths"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "title", "summary", "actual_behavior", "expected_behavior",
        "codebase_context", "relevant_files", "possible_causes",
    ],
    "additionalProperties": False,
}

ISSUE_TITLE_RESPONSE_SCHEMA = {
    "title": "issue_title",
    "description": "A concise GitHub issue title.",
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
    "additionalProperties": False,
}

BODY_MODE_GENERATED = "generated"
BODY_MODE_ORIGINAL = "original"


class IssueDraftValidationError(ValueError):
    pass


@dataclass(frozen=True)
class RelevantFile:
    path: str
    reason: str


@dataclass(frozen=True)
class PossibleCause:
    hypothesis: str
    evidence_paths: tuple[str, ...]


@dataclass(frozen=True)
class IssueDraft:
    title: str
    summary: str
    actual_behavior: str
    expected_behavior: str
    codebase_context: str = ""
    relevant_files: tuple[RelevantFile, ...] = ()
    possible_causes: tuple[PossibleCause, ...] = ()
    context_branch: str = ""
    context_commit_sha: str = ""
    body_mode: str = BODY_MODE_GENERATED
    raw_body: str = ""

    @classmethod
    def from_llm(
        cls,
        raw: dict[str, Any],
        *,
        context_branch: str = "",
        context_commit_sha: str = "",
        allowed_paths: frozenset[str] | None = None,
    ) -> "IssueDraft":
        relevant_files = tuple(
            RelevantFile(
                path=str(item.get("path") or "").strip(),
                reason=str(item.get("reason") or "").strip(),
            )
            for item in raw.get("relevant_files") or []
            if isinstance(item, dict)
        )
        possible_causes = tuple(
            PossibleCause(
                hypothesis=str(item.get("hypothesis") or "").strip(),
                evidence_paths=tuple(
                    str(path).strip() for path in item.get("evidence_paths") or [] if str(path).strip()
                ),
            )
            for item in raw.get("possible_causes") or []
            if isinstance(item, dict)
        )
        draft = cls(
            title=str(raw.get("title") or "").strip(),
            summary=str(raw.get("summary") or "").strip(),
            actual_behavior=str(raw.get("actual_behavior") or "").strip(),
            expected_behavior=str(raw.get("expected_behavior") or "").strip(),
            codebase_context=str(raw.get("codebase_context") or "").strip(),
            relevant_files=relevant_files,
            possible_causes=possible_causes,
            context_branch=context_branch or str(raw.get("context_branch") or "").strip(),
            context_commit_sha=context_commit_sha or str(raw.get("context_commit_sha") or "").strip(),
            body_mode=str(raw.get("body_mode") or BODY_MODE_GENERATED).strip(),
            raw_body=str(raw.get("raw_body") or ""),
        )
        draft.validate(allowed_paths=allowed_paths)
        return draft

    def validate(self, *, allowed_paths: frozenset[str] | None = None) -> None:
        if not self.title:
            raise IssueDraftValidationError("issue title is required")
        if len(self.title) > 256:
            raise IssueDraftValidationError("issue title must be 256 characters or fewer")
        if self.body_mode not in {BODY_MODE_GENERATED, BODY_MODE_ORIGINAL}:
            raise IssueDraftValidationError("unsupported issue body mode")
        if self.body_mode == BODY_MODE_ORIGINAL:
            if not self.raw_body.strip():
                raise IssueDraftValidationError("original issue body is required")
            return
        if not self.summary:
            raise IssueDraftValidationError("issue summary is required")
        if not self.actual_behavior:
            raise IssueDraftValidationError("actual behavior is required")
        if not self.expected_behavior:
            raise IssueDraftValidationError("expected behavior is required")
        has_context = any(
            (
                self.codebase_context,
                self.relevant_files,
                self.possible_causes,
                self.context_branch,
                self.context_commit_sha,
            )
        )
        if has_context:
            if not self.codebase_context:
                raise IssueDraftValidationError("codebase context is required")
            if not self.context_branch or not self.context_commit_sha:
                raise IssueDraftValidationError("codebase context branch and commit are required")
            if not self.relevant_files:
                raise IssueDraftValidationError("at least one relevant file is required")
        if len(self.relevant_files) > 6:
            raise IssueDraftValidationError("at most 6 relevant files are allowed")
        if len(self.possible_causes) > 3:
            raise IssueDraftValidationError("at most 3 possible causes are allowed")
        for item in self.relevant_files:
            if not item.path or not item.reason:
                raise IssueDraftValidationError("relevant files require path and reason")
            if allowed_paths is not None and item.path not in allowed_paths:
                raise IssueDraftValidationError(f"relevant file was not supplied as evidence: {item.path}")
        for item in self.possible_causes:
            if not item.hypothesis or not item.evidence_paths:
                raise IssueDraftValidationError("possible causes require a hypothesis and evidence paths")
            if allowed_paths is not None:
                unknown = next((path for path in item.evidence_paths if path not in allowed_paths), None)
                if unknown:
                    raise IssueDraftValidationError(f"possible-cause evidence was not supplied: {unknown}")

    def to_json(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "actual_behavior": self.actual_behavior,
            "expected_behavior": self.expected_behavior,
            "codebase_context": self.codebase_context,
            "relevant_files": [
                {"path": item.path, "reason": item.reason} for item in self.relevant_files
            ],
            "possible_causes": [
                {"hypothesis": item.hypothesis, "evidence_paths": list(item.evidence_paths)}
                for item in self.possible_causes
            ],
            "context_branch": self.context_branch,
            "context_commit_sha": self.context_commit_sha,
            "body_mode": self.body_mode,
            "raw_body": self.raw_body,
        }

    def body(self, image_links: list[str], marker: str, repo: str = "") -> str:
        if self.body_mode == BODY_MODE_ORIGINAL:
            sections = [self.raw_body]
            if image_links:
                sections.extend(["", "## Images", "", *image_links])
            sections.extend(["", marker])
            return chr(10).join(sections)
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
        if self.codebase_context:
            sections.extend(
                [
                    "", "## Codebase context", "", self.codebase_context, "",
                    f"Analyzed `{self.context_branch}` at `{self.context_commit_sha}`.",
                ]
            )
        if self.relevant_files:
            sections.extend(["", "## Relevant files", ""])
            for item in self.relevant_files:
                label = item.path.replace("]", "\\]")
                if repo and self.context_commit_sha:
                    url = f"https://github.com/{repo}/blob/{self.context_commit_sha}/{quote(item.path, safe='/')}"
                    sections.append(f"- [`{label}`]({url}) — {item.reason}")
                else:
                    sections.append(f"- `{item.path}` — {item.reason}")
        if self.possible_causes:
            sections.extend(["", "## Possible causes", ""])
            for item in self.possible_causes:
                evidence = ", ".join(f"`{path}`" for path in item.evidence_paths)
                sections.extend([f"- **Hypothesis:** {item.hypothesis}", f"  - Evidence: {evidence}"])
        if image_links:
            sections.extend(["", "## Images", "", *image_links])
        sections.extend(["", marker])
        return chr(10).join(sections)
