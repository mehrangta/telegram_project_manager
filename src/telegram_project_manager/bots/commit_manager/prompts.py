SYSTEM_PROMPT = """You are a GitHub commit planning assistant.
Return only valid JSON matching the requested schema.
Do not execute actions.
Do not include secrets.
Ask clarifying questions when the request is ambiguous.
Prefer small, reviewable commits.
Every file change must include full file content, not a description.
"""


def build_user_prompt(
    *,
    request_text: str,
    repo: str,
    base_branch: str,
    target_branch: str,
    max_files: int,
    max_bytes: int,
) -> str:
    return f"""Create a commit plan for this Telegram request.

Request:
{request_text}

Context:
- active_repo: {repo}
- base_branch: {base_branch}
- target_branch: {target_branch}
- max_files: {max_files}
- max_bytes: {max_bytes}

Return JSON:
{{
  "intent": "create_commit",
  "repo": "{repo}",
  "base_branch": "{base_branch}",
  "target_branch": "{target_branch}",
  "commit_message": "Short imperative commit message",
  "changes": [
    {{
      "path": "README.md",
      "operation": "create_or_update",
      "content": "Full proposed file content"
    }}
  ],
  "github_comment": "Short comment explaining why this commit was created.",
  "requires_confirmation": true,
  "questions": []
}}
"""

