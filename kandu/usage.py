"""Reads Claude usage at no token cost (DESIGN §6).

Fails closed: if usage cannot be read, the caller must treat it as over the limit.
"""
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SESSION = re.compile(r"Current session:\s*(\d+)% used(.*)")
WEEK = re.compile(r"Current week \(all models\):\s*(\d+)% used(.*)")
# "resets Oct 30, 6:20pm", "resets 8pm" or "resets Oct 30, 8pm (Europe/Berlin)": a day is left out when it is the next.
RESETS = re.compile(r"resets\s+(?:([A-Z][a-z]{2})\s+(\d{1,2}),\s*)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)(?:\s*\(([^)]+)\))?", re.I)
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
TIMEOUT_SECONDS = 60


class UsageError(Exception):
    pass


@dataclass
class Usage:
    session: int
    week: int
    session_resets: str | None = None  # When Claude resets each limit, as an ISO time; None when `/usage` does not say.
    week_resets: str | None = None


def parse_resets(rest, now):
    """Takes what follows a line's percent; returns when that limit resets as an ISO time, or None.

    `/usage` names the time on this machine's clock unless it names a zone, and leaves out the year: the reset is the
    next time it comes, so a day already past by more than a day is next year's.
    """
    found = RESETS.search(rest)
    if found is None:
        return None
    month, day, hour, minute, half, zone = found.groups()
    try:
        here = now.astimezone(ZoneInfo(zone)) if zone else now
    except (ZoneInfoNotFoundError, ValueError):
        return None
    if month and month.lower() not in MONTHS:
        return None
    hour = int(hour) % 12 + (12 if half.lower() == "pm" else 0)
    try:
        at = here.replace(month=MONTHS.index(month.lower()) + 1 if month else here.month, day=int(day) if day else here.day,
                          hour=hour, minute=int(minute or 0), second=0, microsecond=0)
    except ValueError:
        return None
    if not month and at < here:
        at += timedelta(days=1)
    elif month and at < here - timedelta(days=1):
        at = at.replace(year=at.year + 1)
    return at.isoformat(timespec="seconds")


def parse_usage(text, now=None):
    """Takes the text of `/usage`; returns Usage, or raises UsageError.

    The weekly line must be there. Some plans have no 5-hour window, and then `/usage` shows no
    session line: that counts as 0%. A reset time it cannot read is None; the percents still count.
    """
    session, week = SESSION.search(text), WEEK.search(text)
    if week is None:
        raise UsageError("usage text has an unknown shape")
    now = now or datetime.now().astimezone()
    return Usage(int(session.group(1)) if session else 0, int(week.group(1)),
                 parse_resets(session.group(2), now) if session else None, parse_resets(week.group(2), now))


def read_usage():
    args = ["claude", "-p", "/usage", "--output-format", "json"]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise UsageError(f"could not run claude: {type(e).__name__}") from e
    if result.returncode != 0:
        raise UsageError(f"claude exited with {result.returncode}")
    try:
        return parse_usage(json.loads(result.stdout).get("result", ""))
    except (json.JSONDecodeError, AttributeError, TypeError) as e:
        raise UsageError("claude gave no JSON result") from e


def over_limit(usage, limits):
    """Returns why usage is over a limit, or None if it is under both."""
    if usage.session >= limits.session_5h_percent:
        return f"5-hour usage at {usage.session}%, limit {limits.session_5h_percent}%"
    if usage.week >= limits.weekly_percent:
        return f"weekly usage at {usage.week}%, limit {limits.weekly_percent}%"
    return None
