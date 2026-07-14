from __future__ import annotations

import html
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


TELEGRAM_TEXT_LIMIT = 4096
COPY_TEXT_LIMIT = 256
URL_RE = re.compile(r"https://[^\s<>]+")
PRIMARY_ID_RE = re.compile(r"(?m)^(Code Job ID|Draft ID|Plan ID):\s*([^\s]+)\s*$")
COMMAND_RE = re.compile(r"(/[a-z][a-z0-9_]*(?:\s+[^\n]+)?)", re.IGNORECASE)
FIELD_RE = re.compile(r"^([^:\n]{1,40}):\s*(.*)$")


@dataclass(frozen=True)
class InlineButton:
    label: str
    copy_text: str | None = None
    url: str | None = None

    def __post_init__(self) -> None:
        if (self.copy_text is None) == (self.url is None):
            raise ValueError("Inline buttons require exactly one action")
        if self.copy_text is not None and not 1 <= len(self.copy_text) <= COPY_TEXT_LIMIT:
            raise ValueError("Telegram copy text must be between 1 and 256 characters")

    def to_api(self) -> dict[str, object]:
        payload: dict[str, object] = {"text": self.label}
        if self.copy_text is not None:
            payload["copy_text"] = {"text": self.copy_text}
        else:
            payload["url"] = self.url
        return payload


@dataclass(frozen=True)
class OutgoingMessage:
    text: str
    parse_mode: str | None = "HTML"
    keyboard: tuple[tuple[InlineButton, ...], ...] = ()
    disable_link_preview: bool = True

    def reply_markup(self, *, include_empty: bool = False) -> dict[str, object] | None:
        if not self.keyboard and not include_empty:
            return None
        return {
            "inline_keyboard": [
                [button.to_api() for button in row]
                for row in self.keyboard
            ]
        }


def bullet_list(items: Iterable[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def truncate(value: str, limit: int = 3500) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 20].rstrip() + "\n... truncated ..."


def outgoing_message(
    value: str | OutgoingMessage,
    *,
    keyboard: Sequence[Sequence[InlineButton]] | None = None,
    expandable_prefixes: Sequence[str] = (),
) -> OutgoingMessage:
    if isinstance(value, OutgoingMessage):
        return value
    plain = truncate(value, TELEGRAM_TEXT_LIMIT)
    resolved_keyboard = (
        tuple(tuple(row) for row in keyboard)
        if keyboard is not None
        else keyboard_for_text(plain)
    )
    return OutgoingMessage(
        text=_render_html(plain, expandable_prefixes=tuple(expandable_prefixes)),
        keyboard=resolved_keyboard,
    )


def copy_button(label: str, text: str) -> InlineButton:
    return InlineButton(label=label, copy_text=text)


def url_button(label: str, url: str) -> InlineButton:
    return InlineButton(label=label, url=url)


def rows(buttons: Sequence[InlineButton], width: int = 2) -> tuple[tuple[InlineButton, ...], ...]:
    return tuple(tuple(buttons[index : index + width]) for index in range(0, len(buttons), width))


def keyboard_for_text(text: str) -> tuple[tuple[InlineButton, ...], ...]:
    primary = PRIMARY_ID_RE.search(text)
    if not primary:
        return rows(_url_buttons(text))
    identifier = primary.group(2)
    noun = {"Code Job ID": "Job ID", "Draft ID": "Draft ID", "Plan ID": "Plan ID"}[
        primary.group(1)
    ]
    buttons = [copy_button(f"📋 {noun}", identifier)]
    seen = {identifier}
    for line in text.splitlines():
        match = COMMAND_RE.search(line)
        if not match:
            continue
        command = match.group(1).strip()
        command = re.sub(r"\s+<[^>]+>$", "", command).rstrip()
        if identifier not in command or command in seen or len(command) > COPY_TEXT_LIMIT:
            continue
        seen.add(command)
        buttons.append(copy_button(f"📋 {_command_action(command)}", command))
    buttons.extend(_url_buttons(text))
    return rows(buttons)


def _url_buttons(text: str) -> list[InlineButton]:
    buttons: list[InlineButton] = []
    seen: set[str] = set()
    for line in text.splitlines():
        match = URL_RE.search(line)
        if not match:
            continue
        url = match.group(0).rstrip(".,)")
        if url in seen:
            continue
        seen.add(url)
        lower = line.lower()
        if "pull request" in lower or "/pull/" in url:
            label = "↗ Pull request"
        elif "deployment" in lower or "/actions/" in url:
            label = "↗ Deployment"
        elif "/issues/" in url or "issue" in lower:
            label = "↗ Issue"
        elif "commit" in lower:
            label = "↗ Commit"
        else:
            label = "↗ Open link"
        buttons.append(url_button(label, url))
    return buttons[:3]


def _command_action(command: str) -> str:
    parts = command.split()
    if parts[0].lower() == "/code" and len(parts) > 1:
        action = parts[1]
    else:
        action = parts[0].lstrip("/")
    return action.replace("_", " ").title()


def _render_html(text: str, *, expandable_prefixes: tuple[str, ...]) -> str:
    lines = text.splitlines()
    rendered: list[str] = []
    first_content = True
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line:
            rendered.append("")
            index += 1
            continue
        if expandable_prefixes and line.startswith(expandable_prefixes):
            section = [line]
            index += 1
            while index < len(lines) and not _is_detail_boundary(lines[index]):
                section.append(lines[index])
                index += 1
            content = "\n".join(html.escape(item) for item in section).strip()
            rendered.append(f"<blockquote expandable>{content}</blockquote>")
            first_content = False
            continue
        if first_content:
            rendered.append(_heading(line))
            first_content = False
        else:
            rendered.append(_render_line(line))
        index += 1
    return "\n".join(rendered).strip()


def _heading(line: str) -> str:
    escaped = html.escape(line)
    if line and line[0] in "✅❌⚠️ℹ️🧭⏸⚙🧪📝📦🔐":
        emoji, _, rest = escaped.partition(" ")
        return f"{emoji} <b>{rest or emoji}</b>"
    lower = line.lower()
    if any(word in lower for word in ("failed", "not created", "unauthorized")):
        icon = "❌"
    elif any(word in lower for word in ("created", "succeeded", "ready", "set", "cleared")):
        icon = "✅"
    elif any(word in lower for word in ("usage", "need clarification", "expired")):
        icon = "⚠️"
    elif "draft" in lower or "plan" in lower:
        icon = "📝"
    else:
        icon = "ℹ️"
    return f"{icon} <b>{escaped}</b>"


def _render_line(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("/"):
        return f"<code>{html.escape(stripped)}</code>"
    if stripped.startswith("- "):
        return f"• {html.escape(stripped[2:])}"
    if re.match(r"^\d+\.\s", stripped):
        return html.escape(stripped)
    field = FIELD_RE.match(stripped)
    if not field:
        return html.escape(line)
    label, value = field.groups()
    escaped_label = html.escape(label)
    if not value:
        return f"<b>{escaped_label}:</b>"
    url = URL_RE.fullmatch(value)
    if url:
        escaped_url = html.escape(value, quote=True)
        return f'<b>{escaped_label}:</b> <a href="{escaped_url}">Open</a>'
    if label == "Activity":
        return f"<b>Latest activity</b>\n<blockquote>{html.escape(value)}</blockquote>"
    if label == "Status":
        rendered_value = f"<b>{html.escape(value.upper())}</b>"
    elif label in {"Code Job ID", "Draft ID", "Plan ID", "Commit", "Context commit", "Merge commit"}:
        rendered_value = f"<code>{html.escape(value)}</code>"
    elif value.startswith("/"):
        rendered_value = f"<code>{html.escape(value)}</code>"
    else:
        rendered_value = html.escape(value)
    return f"<b>{escaped_label}:</b> {rendered_value}"


def _is_detail_boundary(line: str) -> bool:
    if not line:
        return False
    return line.startswith(
        (
            "Pull request:", "CI checks:", "CI repair attempts:", "Deployment:",
            "Merge commit:", "Deployment run:", "Deployment error:", "Error:",
            "Reply with", "Retry:", "Discard:", "Rebase onto", "Deploy:",
        )
    )
