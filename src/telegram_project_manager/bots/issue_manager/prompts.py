SYSTEM_PROMPT = """You improve rough Telegram messages into concise GitHub issue drafts.
Return only JSON matching the requested schema.
Preserve the user's intent and facts.
Do not invent logs, reproduction steps, versions, or technical causes.
Actual behavior describes the current state.
Expected behavior describes the desired state after resolution.
"""


def build_user_prompt(request_text: str, repo: str) -> str:
    return f"""Improve this request into a GitHub issue for {repo}.

Request:
{request_text}

Return a concise title, summary, actual behavior, and expected behavior.
"""
