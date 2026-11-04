"""Reads Claude usage at no token cost (DESIGN §6).

Fails closed: if usage cannot be read, the caller must treat it as over the limit.
"""
import json
import re
import shutil
import subprocess
from dataclasses import dataclass

SESSION = re.compile(r"Current session:\s*(\d+)% used")
WEEK = re.compile(r"Current week \(all models\):\s*(\d+)% used")
TIMEOUT_SECONDS = 60


class UsageError(Exception):
    pass


@dataclass
class Usage:
    session: int
    week: int


def parse_usage(text):
    """Takes the text of `/usage`; returns Usage, or raises UsageError.

    The weekly line must be there. Some plans have no 5-hour window, and then `/usage` shows no
    session line: that counts as 0%.
    """
    session, week = SESSION.search(text), WEEK.search(text)
    if week is None:
        raise UsageError("usage text has an unknown shape")
    return Usage(int(session.group(1)) if session else 0, int(week.group(1)))


def read_usage():
    # By its full path: Windows starts a program by name only if it ends in .exe, and claude may be claude.cmd.
    args = [shutil.which("claude") or "claude", "-p", "/usage", "--output-format", "json"]
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=TIMEOUT_SECONDS)
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
