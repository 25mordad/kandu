"""Reads Claude's and Codex's usage at no token cost (DESIGN §6).

Fails closed: if usage cannot be read, the caller must treat it as over the limit.
"""
import json
import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SESSION = re.compile(r"Current session:\s*(\d+)% used(.*)")
WEEK = re.compile(r"Current week \(all models\):\s*(\d+)% used(.*)")
# "resets Oct 30, 6:20pm", "resets 8pm" or "resets Oct 30, 8pm (Europe/Berlin)": a day is left out when it is the next.
RESETS = re.compile(r"resets\s+(?:([A-Z][a-z]{2})\s+(\d{1,2}),\s*)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)(?:\s*\(([^)]+)\))?", re.I)
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
TIMEOUT_SECONDS = 60
# What Kandu asks `codex app-server` on its stdin; the answer to id 2 holds the limits. It answers only while its
# stdin is open, so the read keeps it open until that answer comes.
CODEX_USAGE_ID = 2
CODEX_REQUESTS = "".join(json.dumps(request) + "\n" for request in (
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "kandu", "version": "1"}}},
    {"jsonrpc": "2.0", "method": "initialized"},
    {"jsonrpc": "2.0", "id": CODEX_USAGE_ID, "method": "account/rateLimits/read", "params": None}))
# Why Codex cannot run on a machine, in the same words wherever Kandu says it.
CODEX_NOT_INSTALLED = "not installed"
CODEX_LOGGED_OUT = "not logged in; run: codex login"
CODEX_NO_ANSWER = "codex login status did not answer"
DAY_MINUTES = 24 * 60  # a Codex window this long or shorter is its 5-hour one; a longer one is its week


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


def _codex_resets(window):
    stamp = window.get("resetsAt")
    if not isinstance(stamp, int):
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_codex_usage(result):
    """Takes the result of `account/rateLimits/read`; returns Usage, or raises UsageError.

    Each window is known by its length, the first and second when Codex does not say it. As for Claude, the weekly
    window must be there, and a plan with no 5-hour window counts it as 0%.
    """
    try:
        limits = (result.get("rateLimitsByLimitId") or {}).get("codex") or result["rateLimits"]
        windows = [w for w in (limits.get("primary"), limits.get("secondary")) if w is not None]
        session = week = None
        for place, window in enumerate(windows):
            length = window.get("windowDurationMins")
            short = length <= DAY_MINUTES if isinstance(length, int) else place == 0 and len(windows) == 2
            if short and session is None:
                session = window
            elif not short and week is None:
                week = window
        if week is None or not isinstance(week["usedPercent"], int):
            raise UsageError("codex usage has an unknown shape")
        used = session["usedPercent"] if session is not None else 0
        if not isinstance(used, int):
            raise UsageError("codex usage has an unknown shape")
    except (AttributeError, KeyError, TypeError) as e:
        raise UsageError("codex usage has an unknown shape") from e
    return Usage(used, week["usedPercent"], _codex_resets(session) if session is not None else None, _codex_resets(week))


def _start_codex(args, **pipes):
    """Starts `codex` as the leader of its own process group: `codex` may be a wrapper, as the npm package's node
    launcher is, and the program it starts holds the pipes too, so stopping it must stop the whole group."""
    return subprocess.Popen(["codex", *args], start_new_session=True, text=True, **pipes)


def _stop_codex(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def read_codex_usage():
    """Codex's usage, from `codex app-server` on this machine. Only the error's code is kept: its message may name
    the account."""
    try:
        server = _start_codex(["app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as e:
        raise UsageError(f"could not run codex: {type(e).__name__}") from e
    late = threading.Timer(TIMEOUT_SECONDS, _stop_codex, (server,))
    late.start()
    try:
        server.stdin.write(CODEX_REQUESTS)
        server.stdin.flush()
        for line in server.stdout:
            try:
                answer = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(answer, dict) or answer.get("id") != CODEX_USAGE_ID:
                continue
            if "error" in answer:
                code = answer["error"].get("code") if isinstance(answer["error"], dict) else None
                raise UsageError(f"codex refused the usage read: error {code}")
            return parse_codex_usage(answer.get("result"))
        raise UsageError("codex timed out" if late.finished.is_set() else "codex gave no usage answer")
    except OSError as e:
        raise UsageError(f"codex could not be asked: {type(e).__name__}") from e
    finally:
        late.cancel()
        late.join()  # the group is stopped while the server is not yet reaped, so its id is still its own
        _stop_codex(server)
        for pipe in (server.stdin, server.stdout):
            try:
                pipe.close()
            except OSError:
                pass
        server.wait()


def codex_login():
    """None when Codex is logged in on this machine; otherwise why not, in fixed words."""
    try:
        status = _start_codex(["login", "status"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        return CODEX_NOT_INSTALLED
    try:
        code = status.wait(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _stop_codex(status)
        status.wait()
        return CODEX_NO_ANSWER
    return None if code == 0 else CODEX_LOGGED_OUT


def over_limit(usage, limits, who=""):
    """Returns why usage is over a limit, or None if it is under both. `who` names the agent CLI, as `Codex `."""
    if usage.session >= limits.session_5h_percent:
        return f"{who}5-hour usage at {usage.session}%, limit {limits.session_5h_percent}%"
    if usage.week >= limits.weekly_percent:
        return f"{who}weekly usage at {usage.week}%, limit {limits.weekly_percent}%"
    return None
