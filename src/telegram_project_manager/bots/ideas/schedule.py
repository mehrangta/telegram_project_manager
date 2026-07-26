from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

INTERVAL_RE = re.compile(r"^([1-9][0-9]{0,2})d$", re.IGNORECASE)
TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
MAX_INTERVAL_DAYS = 365


def parse_interval(value: str) -> int:
    normalized = value.strip().lower()
    aliases = {"daily": 1, "weekly": 7}
    if normalized in aliases:
        return aliases[normalized]
    match = INTERVAL_RE.fullmatch(normalized)
    if not match:
        raise ValueError("Interval must be daily, weekly, or Nd (1-365 days).")
    days = int(match.group(1))
    if days > MAX_INTERVAL_DAYS:
        raise ValueError("Brainstorm interval cannot exceed 365 days.")
    return days


def parse_utc_time(value: str) -> int:
    match = TIME_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("Time must use zero-padded 24-hour HH:MM in UTC.")
    return int(match.group(1)) * 60 + int(match.group(2))


def next_run_at(now: int, interval_days: int, run_at_minute_utc: int) -> int:
    if not 1 <= interval_days <= MAX_INTERVAL_DAYS:
        raise ValueError("Brainstorm interval must be between 1 and 365 days.")
    if not 0 <= run_at_minute_utc < 24 * 60:
        raise ValueError("Brainstorm UTC time is invalid.")
    current = datetime.fromtimestamp(now, UTC)
    candidate = current.replace(
        hour=run_at_minute_utc // 60,
        minute=run_at_minute_utc % 60,
        second=0,
        microsecond=0,
    )
    if candidate <= current:
        candidate += timedelta(days=1)
    return int(candidate.timestamp())


def advance_run_at(scheduled_at: int, now: int, interval_days: int) -> int:
    step = interval_days * 24 * 60 * 60
    candidate = scheduled_at
    while candidate <= now:
        candidate += step
    return candidate


def format_interval(interval_days: int) -> str:
    if interval_days == 1:
        return "daily"
    if interval_days == 7:
        return "weekly"
    return f"{interval_days}d"


def format_utc_time(run_at_minute_utc: int) -> str:
    return f"{run_at_minute_utc // 60:02d}:{run_at_minute_utc % 60:02d} UTC"
