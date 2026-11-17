"""Kandu's dashboard and its public HTTP API (DESIGN §7). Documented in docs/API.md.

The page has two looks, Kandu Clean in dashboard.html and Classic in classic.html; each uses only this API, so
all it shows is open to any other system. Reads are open on the bound address; controls need a token.
"""
import argparse
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import (add, backup, bazras, clean, flights, ghlimit, ghmeter, queen, questions, remove, rename, review, sessions, split,
               tasks, timer, tries, upstream, waits)
from .config import (AGENT_CLIS, CLAUDE, CODEX, DEFAULT_LOCAL_DIR, NOTIFY_EVENTS, PACE_SETTINGS, REGISTRY_SETTINGS, TECHNICAL_DEBT,
                     ConfigError, Inspect, UsageLimits,
                     load_config, set_backup, set_clean_merged, set_drop_blockers, set_inspect, set_limits, set_pace,
                     set_default_look, set_notify_events, set_parallel, set_project_values, set_show_requests,
                     set_telegram_buttons)
from .github import AGENT_LABEL, GitHubError, task_blockers
from . import telegram_bot
from .mirror import FRESH_SECONDS, open_github
from .runner import DEFAULT_LOGS_DIR, Runner, RunnerError
from .state import PROJECT_SETTINGS, StateStore, is_project_setting
from .tasks import TaskError

PAGE = Path(__file__).with_name("dashboard.html")  # the Kandu Clean look
CLASSIC_PAGE = Path(__file__).with_name("classic.html")
CLASSIC_NEW_PAGE = Path(__file__).with_name("classic-new.html")
LOOK_COOKIE = "kandu-look"  # the look each browser chose; the page sets it
LOOKS = {"classic": CLASSIC_PAGE, "classic-new": CLASSIC_NEW_PAGE, "clean": PAGE}  # in the order the Look menu lists them
FONT = Path(__file__).with_name("vazirmatn.woff2")
TOKEN_ENV = "KANDU_DASHBOARD_TOKEN"
MAX_BODY = 16384  # a POST's form body; a longer one is refused whole, never cut, as a cut form would save half a command
BODY_ONLY = ("token", "chat_id")  # secrets a POST takes only from its body, so no URL or log ever holds one
API = "/api/v1"
MACHINE_SILENT = "the machine does not answer"  # why Codex cannot be asked about there
QUEUE_CACHE_SECONDS = 900  # GitHub's hourly limit is shared with all else on the account, however many pages are open.
QUEUE_FRESH_SECONDS = 60
QUEUE_RETRY_SECONDS = 300
STREAM_POLL_SECONDS = 2
EVENT_PAGE = 500
RUN_ID = re.compile(r"r-[\w-]+")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
MERGEABLE_COLUMNS = ("Ready", "In progress", "In review")  # the columns a cycle picks up
DONE = "Done"
SENT_BACK_FROM = ("Blocked", "Needs info")  # the columns the user can send a task back to work from
DONE_KEEP_DAYS = 5  # A finished task stays in the hive this long, so the recent work stays in sight.
QUEUE_COLUMNS = (DONE, "Backlog", "Ready", "In progress", "In review", "Needs info", "Needs split", "Blocked", TECHNICAL_DEBT)
TOOL_DETAIL_KEYS = ("file_path", "path", "pattern", "command")
MAX_DETAIL = 200
CARD_KEYS = ("title", "url", "status", "labels", "priority", "kind", "size", "deadline", "closed_at", "item_id")
USER_DECIDES = waits.USER_DECIDES
WAITS_FOR_BLOCKER = re.compile(r"blocker \S+#\d+ is not merged yet")  # as the merge gate words it
REASONS_APART = "; "  # how the merge gate joins its reasons in one decision
ONE_WAY_DOOR = "this is a one-way door"  # as `merge.merge_waits` words it: with the review's questions, all a yes answers


def held_by_blockers(reasons):
    """True when every reason a merge waited is a blocker: nothing the user could approve away."""
    return bool(reasons) and all(WAITS_FOR_BLOCKER.fullmatch(reason) for reason in reasons)


def recommendation(record):
    """The one-line advice of a review round, worked out in code."""
    if review.blockers(record["open"]):
        return "wait: a blocker is open"
    if record["door"] == "one-way":
        return "your decision: this is hard to undo"
    if record.get("wait_for_user"):
        return "your decision: the review has a question"
    if record["open"]:
        return "ready to merge, with notes"
    return "ready to merge"


MERGE_PROBLEMS, MERGE_PENDING = waits.MERGE_PROBLEMS, waits.MERGE_PENDING  # why a merge waits: see waits.py
MERGE_WAITS_TOO_OFTEN = 2  # a merge held this often in a row will not pass by itself
SETTLED = ("approved", "approved_with_notes")  # Kandu takes the PR as it is; a round that pushed nothing is over
STOPPED = ("blocked", "needs_info", "sizing_failed", "failed", "deploy_failed", "live_check_failed")


def _status(state, why=None, detail=None, tries_=None, wait=None):
    """`state` is fine, problem, error or stuck; `why` names the reason the page puts in a sentence, `detail` is the
    log's own words. `tries`: in Kandu's own error, which try failed and how many Kandu makes before it stops.
    `wait`: what the task waits on from the user, where and the next step (waits.py), or None if it waits on nobody.
    """
    return {"state": state, "why": why, "detail": detail, "tries": tries_, "wait": wait}


def task_statuses(events):
    """Whether each task is fine, has a problem, hit an error, or is stuck, and why. The local log alone.

    Stuck means Kandu will not move the task on by itself: only the user, the queen or a new head does. `cleaned`
    says what Kandu cleaned up in the task's folder before its last run, until the task moves on.
    """
    found = {}
    for event in events:
        task, kind = event.get("task"), event["type"]
        if task is None:
            continue
        seen = found.setdefault(task, {"status": _status("fine"), "stuck": False, "ended": False, "approved": False,
                                       "waits": 0, "no_commit": None, "failed": None, "tries": 0, "cleaned": None})

        def say(state, why=None, detail=None, stuck=False, tries_=None, wait=None):
            wait = waits.decision() if stuck and wait is None else wait
            seen.update(status=_status(state, why, detail, tries_, wait), stuck=stuck)

        def settled(reason):
            """Kandu settled the PR. Left to the user, it waits for their yes, unless a check or a blocker holds it."""
            if not waits.user_decides(reason):
                return say("fine")
            wait = waits.settled(reason)
            if wait is not None and wait["held"]:  # the check's name and link are in the wait; the detail keeps words
                holds = reason.split(REASONS_APART)[1:]
                return say("problem", "held_before_yes",
                           REASONS_APART.join(r for r in holds if not waits.CHECK_REASON.fullmatch(r)), wait=wait)
            say("fine", "waits_for_you", wait=wait)
        if kind in tries.PROGRESS:
            seen.update(tries=0, cleaned=None)
        if kind == "review_finished":
            seen.update(ended=False, approved=False, waits=0, no_commit=None)
            if event["blockers"] or not event["criteria_met"]:
                say("problem", "review_blockers", str(event["blockers"]))
            else:
                say("fine", "review_passed")
        elif kind in ("user_approved", "queen_approved"):
            seen.update(ended=False, approved=True, waits=0)
            say("fine", "approved")
        elif kind == "user_resumed" or (kind == "queen_decided" and event["action"] in queen.LIMITED):
            seen.update(ended=False, approved=False, waits=0, no_commit=None)
            say("fine")
        elif kind == "task_picked" and seen["ended"]:
            seen.update(ended=False, waits=0, no_commit=None)
            say("fine")
        elif kind == "loop_stopped":
            seen["ended"] = True
            say("stuck", "loop_stopped", event["reason"], stuck=True)
        elif kind == "task_finished" and event["outcome"] in STOPPED:
            outcome = event["outcome"]
            detail = seen["no_commit"] if outcome == "blocked" else seen["failed"] if outcome == "failed" else None
            seen["ended"] = True
            say("stuck", outcome, detail, stuck=True)
        elif kind == "decision":
            decision, reason = event["decision"], event["reason"]
            if decision == "worktree_cleaned":
                seen["cleaned"] = reason
            elif decision == "internal_error":
                seen["tries"] += tries.is_try(event)
                if not seen["stuck"]:
                    say("error", "internal_error", reason, tries_={"this": seen["tries"], "of": tries.INTERNAL_TRIES})
            elif decision == "round_no_commit":
                seen["no_commit"] = reason
                say("stuck", "round_no_commit", reason, stuck=True)
            elif decision in ("failed", "sizing_failed"):
                seen["failed"] = reason
                if not seen["stuck"]:
                    say("error", "failed", reason)
            elif seen["stuck"] and not (decision in SETTLED and seen["status"]["why"] == "round_no_commit"):
                continue  # nothing but the user, the queen, a new head or Kandu settling the PR moves it on
            elif decision in SETTLED and seen["stuck"]:
                seen["no_commit"] = None  # Kandu took the PR as it is, so the round that pushed nothing is behind it
                settled(reason)
            elif decision == "merge_waits":
                reasons = reason.split(REASONS_APART)
                problems = [r for r in reasons if r in MERGE_PROBLEMS]
                wait = waits.from_reasons(reasons)
                if problems:
                    seen["waits"] += 1
                    if seen["approved"] or seen["waits"] >= MERGE_WAITS_TOO_OFTEN:
                        say("stuck", "merge_held", REASONS_APART.join(problems), stuck=True, wait=wait)
                    else:
                        say("problem", "merge_waits", REASONS_APART.join(problems), wait=wait)
                elif all(MERGE_PENDING.fullmatch(r) for r in reasons):
                    say("fine", "merge_pending", reason, wait=wait)
                else:
                    say("fine", "waits_for_you", wait=wait)
            elif decision in SETTLED and waits.user_decides(reason):
                settled(reason)
    return {task: seen["status"] | {"cleaned": seen["cleaned"]} for task, seen in found.items()}


def flower_marks(store):
    """What each task's flower shows beyond its column. The local log alone, so it costs GitHub nothing."""
    events = queen.all_events(store)
    marks = {}
    asked = {}  # the questions each task's reviews asked: a yes answers them
    for event in events:
        task, kind = event.get("task"), event["type"]
        if task is None:
            continue
        mark = marks.setdefault(task, {"size": None, "door": None, "sent_back": False, "slice": False, "pr": None,
                                       "questions": 0, "yours": False, "waited": []})
        if kind == "task_sized":
            mark |= {"size": event["size"], "door": event["door"]}
        elif kind == "slice_created":
            mark["slice"] = True
        elif kind == "review_finished":
            mark |= {"door": event["door"], "sent_back": bool(event["blockers"])}
            asked.setdefault(task, set()).update(event["wait_for_user"])
        elif kind == "decision" and event["decision"] == "merge_waits":
            reasons = event["reason"].split(REASONS_APART)
            mark |= {"pr": "held" if held_by_blockers(reasons) else "waits", "waited": reasons}
        elif kind == "decision" and waits.user_decides(event["reason"]):
            holds = event["reason"].split(REASONS_APART)[1:]  # what holds the merge whatever the user says
            mark |= {"pr": "held" if held_by_blockers(holds) else "waits", "yours": True, "waited": holds}
        elif kind == "loop_stopped":
            mark["yours"] = True
        elif kind in ("user_approved", "queen_approved"):
            answered = asked.get(task, set()) | {ONE_WAY_DOOR}  # a yes answers these and nothing else
            mark |= {"pr": "approved", "yours": False, "waited": [r for r in mark["waited"] if r not in answered]}
        elif kind in ("task_picked", "user_resumed"):
            mark |= {"pr": None, "yours": False, "waited": []}
        elif kind == "task_finished":
            if event["outcome"] in questions.ENDED:
                del marks[task]
            else:
                mark |= {"pr": None, "waited": []}
    statuses = task_statuses(events)
    for task, mark in marks.items():
        mark["status"] = statuses.get(task)
    for question in questions.open_questions(store, events):
        if question["task"] in marks:
            marks[question["task"]]["questions"] += 1
    for asked in (queen.last_report(store) or {}).get("questions", []):
        if asked["task"] in marks:
            marks[asked["task"]]["yours"] = True
            status = marks[asked["task"]]["status"]
            if status is not None and status["wait"] is None:  # the queen asks the user: their decision
                marks[asked["task"]]["status"] = status | {"wait": waits.decision()}
    for mark in marks.values():
        if mark["yours"] and mark["status"] is not None and mark["status"]["wait"] is None:
            mark["status"] = mark["status"] | {"wait": waits.unknown()}  # it waits for the user, and nothing says why
    return marks


def _codex_actions(item):
    """The steps in one item of a Codex run, each with the item's id: an item comes once started and again done.
    Its messages, its reasoning and every step's output stay out."""
    kind, key = item.get("type"), item.get("id")
    if kind == "command_execution":
        steps = [("Command", item.get("command"))]
    elif kind == "file_change":
        steps = [("Edit", change.get("path")) for change in item.get("changes") or [] if isinstance(change, dict)]
    elif kind == "web_search":
        steps = [("WebSearch", item.get("query"))]
    elif kind == "mcp_tool_call":
        steps = [(f"mcp__{item.get('server', '')}__{item.get('tool', '')}", "")]
    else:
        steps = []
    return [{"tool": tool, "detail": str(detail or "")[:MAX_DETAIL], "id": key} for tool, detail in steps]


def actions_in(line):
    """Takes one line of a run's log, from `claude` or `codex`; returns what the agent did in it. Tool results stay
    out. A Codex step carries its item's `id`, as it comes twice (`run_actions`)."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []
    if isinstance(event, dict) and event.get("type") in ("item.started", "item.completed"):
        return _codex_actions(event["item"]) if isinstance(event.get("item"), dict) else []
    message = event.get("message") if isinstance(event, dict) and event.get("type") == "assistant" else None
    content = message.get("content") if isinstance(message, dict) else None
    actions = []
    for part in content if isinstance(content, list) else []:
        if isinstance(part, dict) and part.get("type") == "tool_use":
            tool_input = part.get("input") if isinstance(part.get("input"), dict) else {}
            detail = next((str(tool_input[k]) for k in TOOL_DETAIL_KEYS if k in tool_input), "")
            actions.append({"tool": part.get("name", ""), "detail": detail[:MAX_DETAIL]})
    return actions


def run_actions(lines):
    """What a run did, from its log's lines, each step once."""
    seen, actions = set(), []
    for line in lines:
        for action in actions_in(line):
            key = action.pop("id", None)
            if key is not None:
                if (key, action["tool"], action["detail"]) in seen:
                    continue
                seen.add((key, action["tool"], action["detail"]))
            actions.append(action)
    return actions


def closed_lately(item):
    if not item.closed_at:
        return False
    closed = datetime.fromisoformat(item.closed_at.replace("Z", "+00:00"))
    return datetime.now(timezone.utc) - closed < timedelta(days=DONE_KEEP_DAYS)


def _holds(card, blocker):
    """True when a card's issue holds `blocker` under `## Blocked by` or in a GitHub "blocked by" relationship."""
    return queen.names_blocker(card.body, card.repo, blocker) or queen.in_relationships(card.blocked_by, blocker)


class ApiError(Exception):
    def __init__(self, status, message, extra=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}  # more fields of the answer, for a page that offers what to do next


class Api:
    def __init__(self, config, store, github, logs_dir, token=None, clock=time.monotonic, start_cycle=timer.start_cycle,
                 local_dir=DEFAULT_LOCAL_DIR, timer_on=timer.timer_on, flights_=None, restart=timer.restart,
                 backup_timer=timer.enable_backup, install_dir=upstream.INSTALL_DIR, gate=None, runner=None):
        self.config = config
        self.runner = runner  # asks a machine whether Codex can run there; a new Runner each time when not given
        self.gate = gate or ghlimit.GATE
        self.store = store
        self.github = github
        self.logs_dir = Path(logs_dir)
        self.token = token
        self.clock = clock
        self.start_cycle = start_cycle
        self.timer_on = timer_on
        self.local_dir = local_dir
        self.flights = flights_ or flights.Flights(config, store, logs_dir, github=github)
        self.restart_kandu = restart
        self.backup_timer = backup_timer
        self.upstream = upstream.Upstream(install_dir)
        self.bot = telegram_bot.Bot(self, self._approve_from_telegram)  # it takes presses only once main() starts it
        self._config_stamp = config_stamp(local_dir)
        self._config_lock = threading.Lock()
        self._add_lock = threading.Lock()  # one change to the config files at a time
        self._queue = None
        self._queue_time = None
        self._queue_failed = False
        self._queue_finished = None
        self._queue_versions = None
        self._queue_lock = threading.Lock()

    def refresh_config(self):
        """Takes a changed config without a restart, so a new project shows in its own patch.

        A config with a problem leaves the last good one in place; `python -m kandu.config` names the problem. The
        agent CLI the dashboard chose for a project's workers or reviewers is put in its `agent_cli` on each request.
        """
        with self._config_lock:
            stamp = config_stamp(self.local_dir)
            if stamp != self._config_stamp:
                self._config_stamp = stamp
                try:
                    config = load_config(self.local_dir)
                except ConfigError:
                    config = None
                if config is not None:
                    self.config = config
                    self.flights.config = config
                    self._queue_time = None  # a new project's tasks need a new read
            self.store.choose_agent_clis(self.config.projects)  # the dashboard's choice wins over the registry's

    def state(self, query):
        limits = self.config.settings.claude.limits
        marks = self._open_marks(flower_marks(self.store))
        tokens = self._project_tokens()
        current = self.store.read_current()
        codex_usage = current.pop("codex_usage", None)
        return current | self._codex(codex_usage) | {
            "timer_on": self.timer_on(),
            "stop_requested": self.store.stop_file.exists(),
            "stopping": self.store.stopping(),  # bees still finishing their step after a stop
            "paused_projects": sorted(self._paused()),
            "paused_in_config": sorted(p.name for p in self.config.projects if p.paused),
            "project_order": [p.name for p in sorted(self.config.projects, key=lambda p: p.priority)],
            "project_settings": self._project_settings(),
            "language": self.config.settings.language,
            "machines": self._machines(),
            "limits": {"session": limits.session_5h_percent, "week": limits.weekly_percent},
            "parallel": self._parallel(),
            "pace": self._pace(),
            "over_limit": {name: spent for name, spent in tokens.items() if spent["today"] >= spent["limit"]},
            "project_tokens": tokens,
            "queen": self._queen_report(),
            "flowers": marks,
            "waits_for_you": self._waits_for_you(marks),
            "bazras": {"running": self.store.read_bazras(),
                       "last": {p.name: bazras.last_look(p.name, self.local_dir) for p in bazras.inspected(self.config)}},
            "backup": self._backup(),
            "drop_blockers": self.config.settings.supervisor.queen_drop_blockers,
            "notify": self._notify(),
            "show_requests": self.config.settings.dashboard.show_requests,
            "default_look": self.config.settings.dashboard.look,
            "looks": list(LOOKS),
            "clean_merged": self.config.settings.supervisor.clean_merged,
            "upstream": self._upstream(),
            "github_refused": self._github_refused(),
        }

    def _github_refused(self):
        """While GitHub refuses Kandu for a rate limit: when Kandu calls it again, and what GitHub said."""
        held = self.gate.blocked()
        if held is None:
            return None
        at = datetime.fromtimestamp(held["until"], timezone.utc).isoformat(timespec="seconds")
        return {"at": at, "why": held.get("why", "")}

    def _upstream(self):
        """The install's `main` against its upstream, and the open task that brings the updates in; None while off.

        From the local log and Kandu's copy, so a page costs neither git nor GitHub.
        """
        settings = self.config.settings.upstream
        if not settings.enabled:
            return None
        try:
            made = upstream.open_task(self.store, self.github)
        except Exception:  # Whatever the copy does, the rest of the state still answers.
            made = None
        return upstream.status(self.store, settings) | {"task": made}

    def _upstream_on(self):
        if not self.config.settings.upstream.enabled:
            raise ApiError(409, "the upstream check is off: turn it on in kandu.yaml")
        return self.config.settings.upstream

    def check_upstream(self, query):
        """Fetches the upstream branch and counts, now. A failure is shown, not raised: the page keeps working."""
        self.upstream.check(self.store, self._upstream_on())
        return self._upstream()

    def upstream_tick(self, now=None):
        """The dashboard's own clock: checks when `upstream.every_hours` passed since the last try."""
        self.refresh_config()
        return self.upstream.tick(self.store, self.config.settings.upstream, now)

    def bring_in_updates(self, query):
        """Creates one task on the board of Kandu's own project to bring the commits in through a PR.

        While that task is open, a press answers it again and creates nothing. Nothing here merges or pushes.
        """
        self._upstream_on()
        try:
            task, url, created = self.upstream.bring_in(self.config, self.github, self.store, self.local_dir)
        except ValueError as e:
            raise ApiError(409, str(e)) from None
        except TaskError as e:
            raise ApiError(409, str(e)) from None
        self._queue_time = None
        return {"task": task, "url": url, "created": created}

    def _open_marks(self, marks):
        """The log's marks, with «waits for you» kept only on a task that is still open: its card on a board, not
        in `Done`, its issue not closed. Read from the copy, so it costs GitHub nothing; a reopened task gets its
        mark back from the log alone. An unreadable copy leaves the log's marks as they are.
        """
        if not any(mark["yours"] for mark in marks.values()):
            return marks
        try:
            open_tasks = {f"{i.repo}#{i.number}" for b in self._boards() for i in self.github.board_items(b)
                          if i.status != DONE and not i.closed_at}
        except GitHubError:
            return marks
        return {task: mark | {"yours": False} if mark["yours"] and task not in open_tasks else mark
                for task, mark in marks.items()}

    def _waits_for_you(self, marks):
        """How many open tasks wait for the user: those the log marks as theirs, and each open by-hand task, once.

        The by-hand ones come from the last queue read, so this costs GitHub nothing.
        """
        tasks = {task for task, mark in marks.items() if mark["yours"]}
        for columns in (self._queue or {}).get("projects", {}).values():
            tasks |= {entry["task"] for column in columns.values() for entry in column if entry["yours"]}
        return len(tasks)

    def _backup(self):
        """The backup's settings, its copies newest first, and how its last try ended."""
        settings = self.config.settings.backup
        made, failed = self.store.last_event("backup_made"), self.store.last_event("backup_failed")
        last = max((e for e in (made, failed) if e is not None), key=lambda e: e["ts"], default=None)
        return {"enabled": settings.enabled, "at": settings.at, "destination": settings.destination,
                "keep": settings.keep, "copies": [p.name for p in reversed(backup.copies(backup.destination_of(settings)))],
                "last": last}

    def change_backup(self, query):
        """Turns the backup on or off, and sets its hour, destination and copies kept, in `kandu.yaml`.

        Turning it on also starts its timer, so an install whose timer was never enabled backs up too.
        """
        enabled = query.get("enabled", [""])[0]
        if enabled not in ("true", "false"):
            raise ApiError(400, "give enabled=true or enabled=false")
        at = query.get("at", [None])[0] or None
        destination = query.get("destination", [None])[0] or None
        try:
            keep = int(query["keep"][0]) if query.get("keep", [""])[0] else None
        except ValueError:
            raise ApiError(400, "give keep as a whole number") from None
        try:
            set_backup(enabled == "true", at, destination, keep, self.local_dir)
        except ConfigError as e:
            raise ApiError(400, e.problems[0]) from None
        settings = self.config.settings.backup
        settings.enabled = enabled == "true"
        settings.at, settings.destination = at or settings.at, destination or settings.destination
        settings.keep = keep or settings.keep
        self.store.record("backup_changed", enabled=settings.enabled, at=settings.at,
                          destination=settings.destination, keep=settings.keep)
        if settings.enabled:
            try:
                self.backup_timer(self.config)
            except (OSError, subprocess.CalledProcessError):
                raise ApiError(500, "saved, but systemd could not start the backup's timer") from None
        return self._backup()

    def bazras_reports(self, query):
        """Every project: whether Bazras looks at it, at what hour, and its newest reports."""
        settings = self.config.settings.bazras
        return {"default_at": settings.at, "projects": [
            {"name": p.name, "enabled": bool(p.inspect and p.inspect.enabled), "paused_in_config": p.paused,
             "at": (p.inspect.at if p.inspect else None) or settings.at,
             "reports": bazras.recent_looks(p.name, self.local_dir)}
            for p in sorted(self.config.projects, key=lambda p: p.priority)]}

    def change_inspect(self, query, name):
        """Turns Bazras on or off for one project, and sets its hour. The next wake of its timer uses it."""
        project = self._project(name)
        enabled = query.get("enabled", [""])[0]
        if enabled not in ("true", "false"):
            raise ApiError(400, "give enabled=true or enabled=false")
        at = query.get("at", [None])[0] or None
        try:
            set_inspect(project.name, enabled == "true", at, self.local_dir)
        except ConfigError as e:
            raise ApiError(400, e.problems[0]) from None
        project.inspect = project.inspect or Inspect()
        project.inspect.enabled = enabled == "true"
        project.inspect.at = at or project.inspect.at
        self.store.record("inspect_changed", project=project.name, enabled=project.inspect.enabled, at=project.inspect.at)
        return {"enabled": project.inspect.enabled, "at": project.inspect.at or self.config.settings.bazras.at}

    def _paused(self):
        return self.store.paused_projects() | {p.name for p in self.config.projects if p.paused}

    def _queen_report(self):
        report = queen.last_report(self.store)
        if report is None:
            return None
        return report | {"today": self.store.tokens_counted(self.store.today(), role="queen"),
                         "limit": self.config.settings.supervisor.queen_daily_token_limit, "drops": self._drop_asks()}

    def _drop_asks(self):
        """Her asks to drop a blocker that wait for the user's yes. One whose blocker is already gone, from both
        `## Blocked by` and the relationships, is not shown.

        Read from the copy, so it costs GitHub nothing. An unreadable copy shows them all: a yes is checked again.
        """
        asks = queen.drop_asks(queen.all_events(self.store))
        if not asks:
            return []
        try:
            cards = {f"{i.repo}#{i.number}": i for b in self._boards() for i in self.github.board_items(b)}
        except GitHubError:
            return asks
        return [a for a in asks if a["task"] in cards and _holds(cards[a["task"]], a["blocker"])]

    def queue(self, query):
        """Each project's columns. All pages share one read, and a failed read keeps the last good answer.

        A task that finished moved on the board, so it counts as asking for a fresh read.
        So does a change to the shared copy: the supervisor writes each move there.
        """
        with self._queue_lock:  # A page that arrives during a read gets its result.
            finished = self.store.last_event("task_finished")
            moved = self._board_versions() != self._queue_versions
            fresh = query.get("fresh") == ["1"] or finished != self._queue_finished or moved
            wait = QUEUE_RETRY_SECONDS if self._queue_failed else QUEUE_FRESH_SECONDS if fresh else QUEUE_CACHE_SECONDS
            if self._queue_time is None or self.clock() - self._queue_time >= wait:
                self._queue_time = self.clock()
                self._queue_finished = finished
                try:
                    if query.get("fresh") == ["1"] and hasattr(self.github, "refresh"):
                        # The user asked: read GitHub into the copy, then send what waits, in the cycle's order.
                        self.github.refresh(self.config)
                        self.github.send()
                    self._queue = self._read_queue()
                    self._queue_failed = False
                    self._queue_versions = self._board_versions()  # After the read, which may write the copy itself.
                except GitHubError:
                    self._queue_failed = True
            if self._queue is None:
                raise ApiError(502, "GitHub could not be read")
            return self._queue | {"stale": self._queue_failed}

    def boards(self, query):
        """Every board with its columns, fields and cards, from the copy: a session reads GitHub only for the rest.

        `?fresh=1` reads each board into the copy first, at most once a minute, as after a card changed on GitHub.
        """
        projects = {}
        for project in self.config.projects:
            if project.board is not None:
                projects.setdefault((project.board.owner, project.board.number), []).append(project.name)
        answer = []
        try:
            for board in self._boards():
                if query.get("fresh") == ["1"] and hasattr(self.github, "refresh_board"):
                    self.github.refresh_board(board, max_age=FRESH_SECONDS)
                layout = self.github.board_layout(board)
                cards = [{"task": f"{item.repo}#{item.number}"} | {key: getattr(item, key) for key in CARD_KEYS}
                         for item in self.github.board_items(board)]
                answer.append({"owner": board.owner, "number": board.number,
                               "projects": projects[(board.owner, board.number)], **layout, "cards": cards})
        except GitHubError:
            raise ApiError(502, "GitHub could not be read") from None
        return {"boards": answer}

    def _boards(self):
        boards = {}
        for project in self.config.projects:
            if project.board is not None:
                boards.setdefault((project.board.owner, project.board.number), project.board)
        return list(boards.values())

    def _board_versions(self):
        return [self.github.board_version(board) for board in self._boards()]

    def _read_queue(self):
        projects = {p.name: {c: [] for c in QUEUE_COLUMNS} for p in self.config.projects}
        by_repo = {repo: p.name for p in self.config.projects for repo in p.repos}
        items = [item for board in self._boards() for item in self.github.board_items(board)]
        columns = {f"{item.repo}#{item.number}": item.status for item in items}
        for item in items:
            name = by_repo.get(item.repo)
            if name is None or item.status not in QUEUE_COLUMNS:
                continue
            if item.status == DONE and not closed_lately(item):
                continue
            task = f"{item.repo}#{item.number}"
            if item.status == split.NEEDS_SPLIT and self.store.last_event("task_split", task=task) is not None:
                continue  # Accepted: it waits for nobody, and its slices stand for it until the last one closes it.
            # A task without `agent` is the user's to do by hand: shown for a free flight, never worked by Kandu.
            entry = {"task": task, "title": item.title, "url": item.url, "by_hand": item.by_hand,
                     "yours": item.waits_by_hand}
            if item.by_hand:  # Its dialog shows what the user is to do, read from the copy: no GitHub call.
                entry |= {"goal": (tasks.section_text(item.body or "", "Goal") or "").strip(),
                          "criteria": (tasks.section_text(item.body or "", "Acceptance criteria") or "").strip()}
            if item.status in MERGEABLE_COLUMNS:
                entry["blockers"] = self._blockers(item, columns)
            if item.status == DONE:
                entry["closed_at"] = item.closed_at  # when it closed, mostly by its PR's merge
            projects[name][item.status].append(entry)
        return {"projects": projects, "done_days": DONE_KEEP_DAYS}

    def _blockers(self, item, columns):
        """The open blockers of a task, each with its column; a split one with its slices. They are the merge gate's:
        its `## Blocked by` joined with its GitHub "blocked by" relationships, each once.

        From the copy and the local log only. A split blocker is open until its last slice closes, as at the
        merge gate. `columns` maps each task on a board to its column.
        """
        held = []
        for repo, number in task_blockers(item.body, item.repo, item.blocked_by):
            key = f"{repo}#{number}"
            last = tasks.last_slice(self.store, key) or key
            last_repo, _, last_number = last.rpartition("#")
            try:
                if not self.github.issue_is_open(last_repo, int(last_number)):
                    continue
            except GitHubError:
                pass  # the copy does not know it: the merge gate cannot pass it either
            split_ = self.store.last_event("task_split", task=key)
            slices = [{"task": child, "column": columns.get(child)} for child in (split_ or {}).get("children", [])]
            held.append({"task": key, "column": columns.get(key), "slices": slices})
        return held

    def days(self, query):
        return {"days": self.store.days()}

    def didban(self, query):
        return {"sessions": sessions.recent(self.store)}

    def requests(self, query):
        """Open requests by project, each with its decision map or null. Read from local state only: no GitHub call."""
        shown = self.config.settings.dashboard.show_requests
        if not shown:
            return {"show_requests": False, "projects": []}
        order = [p.name for p in sorted(self.config.projects, key=lambda p: p.priority)]
        groups = {}
        for request in self.store.requests():
            groups.setdefault(request.get("project"), []).append(
                {"id": request["id"], "text": request["text"], "map": request.get("map")})
        names = [n for n in order if n in groups] + sorted(n for n in groups if n is not None and n not in order)
        names += [None] if None in groups else []
        return {"show_requests": True, "projects": [{"project": n, "requests": groups[n]} for n in names]}

    def change_show_requests(self, query):
        """Shows open requests on the dashboard, or hides them, through the config."""
        enabled = query.get("enabled", [""])[0]
        if enabled not in ("true", "false"):
            raise ApiError(400, "give enabled=true or enabled=false")
        try:
            set_show_requests(enabled == "true", self.local_dir)
        except ConfigError as e:
            raise ApiError(400, e.problems[0]) from None
        self.config.settings.dashboard.show_requests = enabled == "true"
        self.store.record("requests_shown_changed", enabled=enabled == "true")
        return {"show_requests": enabled == "true"}

    def change_default_look(self, query):
        """The look a browser opens in until it chooses its own, in the config."""
        look = query.get("look", [""])[0]
        try:
            set_default_look(look, self.local_dir)
        except ConfigError as e:
            raise ApiError(400, e.problems[0]) from None
        self.config.settings.dashboard.look = look
        return {"default_look": look}

    def list_flights(self, query):
        return self.flights.list()

    def _language(self, query):
        language = query.get("language", [self.config.settings.language])[0]
        if language not in flights.LANGUAGES:
            raise ApiError(400, "no such language")
        return language

    def open_didban(self, query):
        """Answers at once: the session's link appears in `GET /flights`. `project` opens it on that project."""
        project = query.get("project", [None])[0]
        if project is not None and project not in {p.name for p in self.config.projects}:
            raise ApiError(400, "no such project")
        self.flights.open_didban(self._language(query), project)
        return {"opening": flights.DIDBAN}

    def open_zanbourdar(self, query):
        """Answers at once: the session's link appears in `GET /flights`."""
        self.flights.open_zanbourdar(self._language(query))
        return {"opening": flights.ZANBOURDAR}

    def open_kanduban(self, query, name):
        """Asks the project's machine first, then answers: the session's link appears in `GET /flights`."""
        project = next((p for p in self.config.projects if p.name == name), None)
        if project is None:
            raise ApiError(404, "no such project")
        try:
            return {"opening": self.flights.open_kanduban(project, self._language(query))}
        except flights.KandubanError as e:
            raise ApiError(503, str(e)) from None

    def open_flight(self, query, owner, repo, number):
        task = self._split_task(owner, repo, number)
        full = f"{owner}/{repo}"
        project = next(p for p in self.config.projects if full in p.repos)
        self.flights.open_task(project, full, int(number), self._language(query))
        return {"opening": flights.task_key(task)}

    def close_flight(self, query, key):
        if not self.flights.close(key):
            raise ApiError(404, "no such session")
        return {"closed": key}

    def day(self, query, day):
        if not DAY.fullmatch(day):
            raise ApiError(404, "no such day")
        finished = [{k: e[k] for k in ("ts", "task", "project", "outcome")}
                    for e in self.store.events(day) if e["type"] == "task_finished"]
        return self.store.tokens_for_day(day) | {"finished": finished}

    def _range(self, query):
        """The first and last day of the asked range: a named one counted from today, or `from` and `to`."""
        today = date.fromisoformat(self.store.today())
        if "from" in query or "to" in query:
            try:
                first, last = (date.fromisoformat(self._field(query, name)) for name in ("from", "to"))
            except ValueError:
                raise ApiError(400, "from and to must both be YYYY-MM-DD") from None
            if first > last:
                raise ApiError(400, "from must not be after to")
            return first, last
        name = self._field(query, "range", "today")
        if name == "today":
            return today, today
        if name == "week":  # Monday to Sunday
            monday = today - timedelta(days=today.weekday())
            return monday, monday + timedelta(days=6)
        if name == "last7":
            return today - timedelta(days=6), today
        if name == "month":
            following = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
            return today.replace(day=1), following - timedelta(days=1)
        raise ApiError(400, "range must be today, week, last7 or month")

    def usage(self, query):
        """Tokens by project over a range, each project's tasks under it, most first, with Kandu's own runs and the
        sessions apart, so the rows add up to the total."""
        first, last = self._range(query)
        report = self.store.usage_between(first.isoformat(), last.isoformat())
        today = first == last == date.fromisoformat(self.store.today())
        limits = {p.name: p.daily_token_limit for p in self.config.projects}
        tasks = sorted(({"task": task, "project": e["project"], "total": e["tokens"], "outcome": e["outcome"]}
                        for task, e in report["tasks"].items()), key=lambda t: (-t["total"], t["task"]))
        projects = {}
        for task in tasks:
            projects.setdefault(task["project"], []).append({k: task[k] for k in ("task", "total", "outcome")})
        # The limit counts all of a project's runs today, its sizing too, though sizing shows under Kandu itself here.
        grouped = sorted(({"project": name, "total": sum(t["total"] for t in rows),
                           "limit": limits.get(name) if today else None,
                           "counted": self.store.tokens_counted(first.isoformat(), project=name) if today else None,
                           "tasks": rows}
                          for name, rows in projects.items()), key=lambda p: (-p["total"], p["project"] or ""))
        return {"from": first.isoformat(), "to": last.isoformat(), "today": self.store.today(),
                "total": report["total"], "roles": report["roles"], "models": report["models"],
                "projects": grouped, "kandu": report["kandu"], "sessions": report["sessions"], "tasks": tasks}

    def events(self, query):
        try:
            after = int(query.get("after", ["0"])[0])
        except ValueError:
            raise ApiError(400, "after must be a number") from None
        day = query.get("day", [None])[0] or (self.store.days() or [None])[-1]
        if day is None:
            return {"day": None, "events": [], "next": 0}
        if not DAY.fullmatch(day):
            raise ApiError(400, "day must be YYYY-MM-DD")
        events = self.store.events(day)
        return {"day": day, "events": events[after:after + EVENT_PAGE],
                "next": min(len(events), after + EVENT_PAGE)}

    def run(self, query, run_id):
        path = self.logs_dir / f"{run_id}.jsonl"
        if not RUN_ID.fullmatch(run_id) or not path.exists():
            raise ApiError(404, "no such run")
        with open(path, encoding="utf-8", errors="replace") as log:
            return {"run_id": run_id, "actions": run_actions(log)}

    def reviews(self, query, owner, repo, number):
        full = f"{owner}/{repo}"
        if not number.isdigit() or not any(full in p.repos for p in self.config.projects):
            raise ApiError(404, "no such task")
        pr = self.github.find_pr(full, int(number))
        if pr is None:
            return {"task": f"{full}#{number}", "pr": None, "rounds": [], "kept": clean.kept(self.store, f"{full}#{number}")}
        task, head = f"{full}#{number}", pr["headRefOid"]
        why_not = self._why_no_approval(task, pr)
        item, columns = self._card(full, int(number))
        blockers = self._blockers(item, columns) if item is not None and pr["state"] == "OPEN" else []
        waited = (flower_marks(self.store).get(task) or {}).get("waited", [])
        gate = [tasks.last_slice(self.store, b["task"]) or b["task"] for b in blockers]  # named as the gate names them
        holds = list(dict.fromkeys([*(f"blocker {key} is not merged yet" for key in gate), *waited]))
        rounds = review.reviews_on(pr, self.config.settings.github.account)
        return {"task": task, "pr": {"number": pr["number"], "title": pr["title"], "url": pr["url"],
                                     "state": pr["state"], "head_sha": head,
                                     "approved": self.store.last_event("user_approved", task=task,
                                                                       head_sha=head) is not None,
                                     "can_approve": why_not is None, "why_not": why_not},
                "blockers": blockers, "waited": waited, "holds": holds, "kept": clean.kept(self.store, task),
                "rounds": [r | {"recommendation": recommendation(r), "user_brief": self._user_brief(full, number, r)}
                           for r in rounds]}

    def _card(self, repo, number):
        """A task's card on any board, or None, and the column of every card: all from the copy."""
        items = [item for board in self._boards() for item in self.github.board_items(board)]
        card = next((i for i in items if i.repo == repo and i.number == number), None)
        return card, {f"{i.repo}#{i.number}": i.status for i in items}

    def _user_brief(self, repo, number, round_):
        """It stays in the local log: a PR comment holds only English."""
        seen = self.store.last_event("review_finished", task=f"{repo}#{number}", round=round_["round"])
        return (seen or {}).get("user_brief")

    def _why_no_approval(self, task, pr):
        """The local log decides, as at the merge gate. These would stop the merge anyway, so the button says so now."""
        if pr["state"] != "OPEN":
            return "this PR is not open"
        seen = self.store.last_event("review_finished", task=task, head_sha=pr["headRefOid"])
        if seen is None:
            return "this head has no review"
        if seen["blockers"]:
            return "a blocker is open"
        if not seen["criteria_met"]:
            return "an acceptance criterion is not met"
        repo, number = task.split("#")
        project = next(p for p in self.config.projects if repo in p.repos)
        if project.name in self._paused():
            return "this project is paused: resume it first"
        if project.board is None:
            return "this project has no board"
        item, columns = self._card(repo, int(number))
        status = item and item.status
        if status not in MERGEABLE_COLUMNS:
            return f"its task is in {status or 'no column'}, so no cycle would pick it up"
        blockers = self._blockers(item, columns)
        if blockers:  # The merge gate holds it until they merge, whatever the user says.
            return f"it waits for {', '.join(b['task'] for b in blockers)} to merge first"
        return None

    def approve(self, query, owner, repo, number):
        """The user's yes to one head. The supervisor merges it and still checks everything else."""
        full = f"{owner}/{repo}"
        project = next((p for p in self.config.projects if full in p.repos), None)
        if not number.isdigit() or project is None:
            raise ApiError(404, "no such task")
        pr = self.github.find_pr(full, int(number))
        if pr is None:
            raise ApiError(404, "this task has no PR")
        if query.get("head", [None])[0] != pr["headRefOid"]:
            raise ApiError(409, "the PR has changed since you saw it")
        task = f"{full}#{number}"
        why = self._why_no_approval(task, pr)
        if why is not None:
            raise ApiError(409, why)
        self.store.record("user_approved", task=task, project=project.name, head_sha=pr["headRefOid"])
        return {"approved": pr["headRefOid"], "cycle_started": self.start_cycle()}

    def send_back(self, query, owner, repo, number):
        """The user's call on a stopped task: it goes back to work, past the queen's limits.

        With an open PR, the next cycle reviews its head, or fixes what the last review found.
        Nothing here merges: the merge gate still decides.
        """
        key = self._split_task(owner, repo, number)
        try:
            project, item = split.find_item(self.config, self.github, key)
        except TaskError as e:
            raise ApiError(404, str(e))
        if item.status not in SENT_BACK_FROM:
            raise ApiError(409, f"its task is in {item.status}, not Blocked or Needs info")
        if AGENT_LABEL not in item.labels:
            raise ApiError(409, "it has no agent label: it is yours to do by hand")
        if project.name in self._paused():
            raise ApiError(409, "this project is paused: resume it first")
        # GitHub as it is now, not the copy, which may be minutes old: a PR merged a minute ago must count.
        try:
            self.github.refresh_repo(item.repo)
        except GitHubError as e:
            raise ApiError(503, f"GitHub could not be read: {e}")
        if not self.github.issue_is_open(item.repo, item.number):
            raise ApiError(409, "its issue is closed")
        pr = self.github.find_pr(item.repo, item.number)
        if pr is not None and pr["state"] == "MERGED":
            raise ApiError(409, "its PR is already merged")
        events = queen.all_events(self.store)
        to = split.BACKLOG if queen.last_outcome(events, key) == "sizing_failed" else "Ready"
        self.github.set_status(project.board, item.url, to)
        self.store.record("user_resumed", task=key, project=project.name, to=to)
        self._queue_time = None
        return {"task": key, "to": to, "cycle_started": self.start_cycle()}

    def to_queen(self, query, owner, repo, number):
        """The user stops Kandu's tries on a task in its own error, as if the last one had failed, and a cycle starts
        so the queen looks at it now and can ask the user. Refused for a task in any other state, or while a try runs.
        """
        key = self._split_task(owner, repo, number)
        events = queen.all_events(self.store)
        if (task_statuses(events).get(key) or {}).get("why") != "internal_error":
            raise ApiError(409, "it is not in Kandu's own error")
        if any(w.get("task") == key for w in self.store.read_current().get("workers") or []):
            raise ApiError(409, "Kandu is trying it again right now")
        try:
            project, item = split.find_item(self.config, self.github, key)
        except TaskError as e:
            raise ApiError(404, str(e))
        to = tries.stop(self.github, self.store, project, item, key, events)
        self._queue_time = None
        started = None if self.store.stop_file.exists() else self.start_cycle()
        return {"task": key, "to": to, "cycle_started": started}

    def task(self, query, owner, repo, number):
        """What a task was, what it cost and its steps. An unreadable issue costs only its text, not the numbers."""
        key = self._split_task(owner, repo, number)
        try:
            text = self.github.issue_text(f"{owner}/{repo}", int(number))
        except GitHubError:
            text = None
        finished = self.store.last_event("task_finished", task=key)
        return {"task": key, "issue": text, "outcome": finished and finished["outcome"],
                "cost": self.store.task_cost(key), "steps": self._steps(key)}

    def _steps(self, key):
        """The task's events so far, oldest first, as its sheet tells them; the tokens stay in the cost."""
        return [{k: v for k, v in event.items() if k not in ("v", "task", "models")}
                for day in self.store.days() for event in self.store.events(day) if event.get("task") == key]

    def _split_task(self, owner, repo, number):
        full = f"{owner}/{repo}"
        if not number.isdigit() or not any(full in p.repos for p in self.config.projects):
            raise ApiError(404, "no such task")
        return f"{full}#{number}"

    def split(self, query, owner, repo, number):
        """The proposal that waits for the user, or null. It stays in the local state, as the issue may be public."""
        task = self._split_task(owner, repo, number)
        proposal = next((s for s in split.pending(self.store) if s["task"] == task), None)
        return {"task": task, "split": proposal and proposal | {"fingerprint": split.fingerprint(proposal)}}

    def approve_split(self, query, owner, repo, number):
        """The user's yes creates the slices. Declining or changing a proposal happens in Didban."""
        task = self._split_task(owner, repo, number)
        try:
            children = split.approve(self.config, self.github, self.store, task, self.local_dir,
                                     seen=query.get("proposal", [""])[0])
        except (TaskError, split.ReportError) as e:
            raise ApiError(409, str(e))
        self._queue = None
        return {"children": children}

    def drop_blocker(self, query, owner, repo, number):
        """The user's yes to her ask: the blocker leaves the issue's `## Blocked by` and its GitHub "blocked by"
        relationship, wherever it is held, and its other blockers stay. Both writes are queued, as every edit is.

        Only an ask she made counts, and only while the issue still holds that blocker in one of the two places. The next cycle checks the
        merge again.
        """
        task = self._split_task(owner, repo, number)
        blocker = query.get("blocker", [""])[0]
        ask = next((a for a in queen.drop_asks(queen.all_events(self.store))
                    if a["task"] == task and a["blocker"] == blocker), None)
        if ask is None:
            raise ApiError(409, f"the queen has not asked to drop {blocker or 'a blocker'} from {task}")
        item, _ = self._card(f"{owner}/{repo}", int(number))
        if item is None:
            raise ApiError(404, "its task is on no board")
        if not _holds(item, blocker):
            raise ApiError(409, f"its issue no longer names {blocker}")
        if queen.names_blocker(item.body, item.repo, blocker):
            self.github.edit_issue(item.repo, item.number, item.title, queen.without_blocker(item.body, item.repo, blocker))
        if queen.in_relationships(item.blocked_by, blocker):
            other, _, other_number = blocker.partition("#")
            self.github.remove_blocked_by(item.repo, item.number, other, int(other_number))
        self.store.record("blocker_dropped", task=task, project=ask["project"], blocker=blocker)
        self._queue_time = None
        return {"task": task, "dropped": blocker, "cycle_started": self.start_cycle()}

    def _notify(self):
        """What Kandu tells the user and where: never the bot's token or the chat, which stay in local/.env."""
        notify = self.config.settings.notify
        return {"adapter": notify.adapter, "events": list(notify.events), "kinds": list(NOTIFY_EVENTS),
                "language": notify.language or self.config.settings.language, "telegram": self.bot.status()}

    def _approve_from_telegram(self, task, head):
        """The dashboard's approval, for a press in Telegram that names the head it approves."""
        repo, number = task.split("#")
        owner, name = repo.split("/")
        try:
            self.approve({"head": [head]}, owner, name, number)
        except ApiError as e:
            raise telegram_bot.Refused(e.message) from None

    def _telegram(self, step):
        try:
            return step()
        except telegram_bot.TelegramError as e:
            raise ApiError(400, str(e)) from None

    def telegram_token(self, query):
        """The bot's token, sent in the request's body, checked with Telegram and kept in local/.env only."""
        self._telegram(lambda: self.bot.set_token(query.get("token", [""])[0]))
        self.store.record("telegram_changed", what="token")
        return {"notify": self._notify()}

    def telegram_chat(self, query):
        """The chat Kandu writes to and takes presses from, kept in local/.env only."""
        self._telegram(lambda: self.bot.set_chat(query.get("chat_id", [""])[0]))
        self.store.record("telegram_changed", what="chat")
        return {"notify": self._notify()}

    def telegram_chats(self, query):
        """The chats that wrote to the bot lately, so the user picks theirs."""
        return {"chats": self.bot.found_chats(), "bot": self.bot.username}

    def telegram_test(self, query):
        self._telegram(self.bot.test)
        return {"sent": True}

    def telegram_buttons(self, query):
        """Turns the approve button in Telegram on or off, through the config. The next cycle's messages carry it."""
        enabled = query.get("enabled", [""])[0]
        if enabled not in ("true", "false"):
            raise ApiError(400, "give enabled=true or enabled=false")
        if self.config.settings.notify.telegram is None:
            raise ApiError(400, "notify.telegram is not in the config")
        try:
            set_telegram_buttons(enabled == "true", self.local_dir)
        except ConfigError as e:
            raise ApiError(400, e.problems[0]) from None
        self.config.settings.notify.telegram.buttons = enabled == "true"
        self.store.record("telegram_changed", what="buttons")
        return {"notify": self._notify()}

    def change_notify(self, query):
        """Sets what Kandu tells the user, as a comma list of NOTIFY_EVENTS or `none`, through the config. The next
        cycle uses it."""
        given = query.get("events")
        if given is None:
            raise ApiError(400, "give events=, a comma list of " + ", ".join(NOTIFY_EVENTS) + ", or none")
        events = [] if given[0] == "none" else [each for each in given[0].split(",") if each]
        try:
            events = set_notify_events(events, self.local_dir)
        except ConfigError as e:
            raise ApiError(400, e.problems[0]) from None
        self.config.settings.notify.events = events
        self.store.record("notify_changed", events=events)
        return {"notify": self._notify()}

    def change_drop_blockers(self, query):
        """Lets the queen ask to drop a blocker, or stops her. Her next look uses it, through the config."""
        enabled = query.get("enabled", [""])[0]
        if enabled not in ("true", "false"):
            raise ApiError(400, "give enabled=true or enabled=false")
        try:
            set_drop_blockers(enabled == "true", self.local_dir)
        except ConfigError as e:
            raise ApiError(400, e.problems[0]) from None
        self.config.settings.supervisor.queen_drop_blockers = enabled == "true"
        self.store.record("drop_blockers_changed", enabled=enabled == "true")
        return {"drop_blockers": enabled == "true"}

    def change_clean_merged(self, query):
        """Turns the cleanup after a merge on or off, through the config. The next cycle uses it."""
        enabled = query.get("enabled", [""])[0]
        if enabled not in ("true", "false"):
            raise ApiError(400, "give enabled=true or enabled=false")
        try:
            set_clean_merged(enabled == "true", self.local_dir)
        except ConfigError as e:
            raise ApiError(400, e.problems[0]) from None
        self.config.settings.supervisor.clean_merged = enabled == "true"
        self.store.record("clean_merged_changed", enabled=enabled == "true")
        return {"clean_merged": enabled == "true"}

    def stop(self, query):
        self.store.request_stop()
        return {"stop_requested": True}

    def resume(self, query):
        self.store.stop_file.unlink(missing_ok=True)
        return {"stop_requested": False}

    @staticmethod
    def _field(query, name, default=""):
        return query.get(name, [default])[0]

    def add_machine(self, query):
        """A machine the brain reaches by an SSH alias; answers whether it answered."""
        try:
            with self._add_lock:
                result = add.add_machine(self._field(query, "name"), self._field(query, "ssh"),
                                         self._field(query, "always_on") == "true", self.local_dir)
        except (add.AddError, ConfigError) as e:
            raise ApiError(400, str(e)) from None
        self.refresh_config()
        self.store.record("machine_added", machine=result["machine"])
        return result

    def detect_project(self, query):
        """What the copy at a path says about itself, as a proposed entry. Changes nothing."""
        try:
            return add.detect(self.config, self._field(query, "machine"), self._field(query, "path"),
                              self._field(query, "repo"))
        except add.AddError as e:
            raise ApiError(400, str(e)) from None

    def add_project(self, query):
        """Writes the project, cloning its copy and creating its board if needed, then checks it."""
        fields = {name: values[0] for name, values in query.items()}
        try:
            with self._add_lock:
                result = add.add_project(fields, self.github, self.local_dir,
                                         lambda config, project: add.doctor_checks(config, project, self.github))
        except (add.AddError, ConfigError) as e:
            raise ApiError(400, str(e)) from None
        self.refresh_config()
        self.store.record("project_added", project=result["project"])
        return result

    def remove_project(self, query, name):
        """Takes the project out of Kandu: offers its memory as a PR to its repo, stops its work, removes Kandu's
        worktrees for it and its entry in the config, then archives its memory. Its copy, repo, board and issues stay.

        `without_memory_pr=true` is the user's choice to go on when that PR cannot be made. Anything that fails
        leaves the config as it was.
        """
        project = self._project(name)
        memory_pr = self._field(query, "without_memory_pr", "false") != "true"
        try:
            with self._add_lock:
                result = remove.remove_project(project.name, self.store, self.flights, self.local_dir,
                                               github=self.github, memory_pr=memory_pr)
        except remove.MemoryPrError as e:
            raise ApiError(409, str(e), {"memory_pr_failed": True}) from None
        except (remove.RemoveError, ConfigError) as e:
            raise ApiError(409, e.problems[0] if isinstance(e, ConfigError) else str(e)) from None
        finally:
            self.refresh_config()
        return result

    def restart(self, query):
        """Takes the config again, re-arms the timer and restarts the dashboard. A running cycle finishes."""
        try:
            config = load_config(self.local_dir)
        except ConfigError as e:
            raise ApiError(409, f"the config has a problem: {e.problems[0]}") from None
        self.restart_kandu(config)
        self.store.record("kandu_restarted")
        return {"restarting": True}

    def wake(self, query):
        """Starts a cycle now, as the timer would: the queen looks and the bees go out."""
        if self.store.stop_file.exists():
            raise ApiError(409, "Kandu is stopped: resume it first")
        started = self.start_cycle()
        if started is None:
            raise ApiError(409, "a cycle is already running")
        if not started:
            raise ApiError(500, "systemd could not start a cycle")
        self.store.record("wake_requested")
        return {"cycle_started": True}

    def _codex(self, usage):
        """Codex's usage, as the supervisor last read it, and its limits: only while a project uses Codex."""
        if not self.config.uses_codex():
            return {}
        limits = self.config.codex_limits()
        return {"codex": {"usage": usage, "limits": {"session": limits.session_5h_percent, "week": limits.weekly_percent}}}

    def change_limits(self, query):
        """Reaches the next cycle through `kandu.yaml`; a cycle already running keeps the limits it read. `cli=codex`
        sets Codex's own, while a project uses Codex."""
        cli = query.get("cli", [CLAUDE])[0]
        if cli not in AGENT_CLIS or (cli == CODEX and not self.config.uses_codex()):
            raise ApiError(400, "cli: claude, or codex while a project uses it")
        try:
            session, week = int(query["session"][0]), int(query["week"][0])
            set_limits(session, week, self.local_dir, cli)
        except (KeyError, ValueError):
            raise ApiError(400, "give both limits, as whole percents") from None
        except ConfigError as e:
            raise ApiError(400, e.problems[0]) from None
        if cli == CODEX:
            self.config.settings.codex.limits = UsageLimits(session, week)
            self.store.record("codex_limits_changed", session=session, week=week)
            return {"codex": {"limits": {"session": session, "week": week}}}
        limits = self.config.settings.claude.limits
        limits.session_5h_percent, limits.weekly_percent = session, week
        self.store.record("limits_changed", session=session, week=week)
        return {"limits": {"session": session, "week": week}}

    def _parallel(self):
        ordered = sorted(self.config.projects, key=lambda p: p.priority)
        return {"total": self.config.settings.supervisor.max_parallel_workers,
                "projects": {p.name: p.max_parallel_workers for p in ordered}}

    def change_parallel(self, query):
        """How many workers build at once, in all and per project. The next cycle uses it, through the config."""
        try:
            total = int(query["total"][0]) if "total" in query else None
            projects = {}
            for item in query.get("project", []):
                name, _, value = item.rpartition(":")
                projects[self._project(name).name] = int(value)
        except ValueError:
            raise ApiError(400, "give each number as a whole number") from None
        if total is None and not projects:
            raise ApiError(400, "give total, or a project as name:number")
        try:
            set_parallel(total, projects, self.local_dir)
        except ConfigError as e:
            raise ApiError(400, e.problems[0]) from None
        if total is not None:
            self.config.settings.supervisor.max_parallel_workers = total
        for project in self.config.projects:
            project.max_parallel_workers = projects.get(project.name, project.max_parallel_workers)
        self.store.record("parallel_changed", total=total, projects=projects)
        return {"parallel": self._parallel()}

    def _project(self, name):
        project = next((p for p in self.config.projects if p.name == name), None)
        if project is None:
            raise ApiError(404, "no such project")
        return project

    def pause_project(self, query, name):
        """No new work for this project, across cycles. Its board and its tasks stay as they are."""
        project = self._project(name)
        if project.name not in self._paused():
            self.store.pause_project(project.name)
            self.store.record("project_paused", project=project.name)
        return {"paused": True}

    def resume_project(self, query, name):
        project = self._project(name)
        if project.paused:
            raise ApiError(409, "this project is paused in its config")
        if project.name in self._paused():
            self.store.resume_project(project.name)
            self.store.record("project_resumed", project=project.name)
        return {"paused": False}

    def _registry_value(self, project, setting):
        return self.config.auto_merge_for(project) if setting == "auto_merge" else getattr(project, setting)

    def _project_settings(self):
        """One row per project, in the registry's order: its pause, how many workers it may have, and each setting
        the dashboard may change, from Kandu's state or the registry."""
        paused = self._paused()
        return [{"name": p.name, "paused": p.name in paused, "paused_in_config": p.paused,
                 "max_parallel_workers": p.max_parallel_workers}
                | {setting: self.store.project_setting(p, setting) for setting in PROJECT_SETTINGS}
                | {setting: self._registry_value(p, setting) for setting in REGISTRY_SETTINGS}
                for p in self.config.projects]

    def codex_on(self, query, name):
        """Whether Codex can run this project's workers or reviewers on its machine, asked as the doctor asks it: `why` is the
        doctor's reason when it cannot, or null. It costs no tokens and changes nothing."""
        project = self._project(name)
        runner = self.runner or Runner(self.config)
        try:
            why = runner.codex_login(project.machine)
        except RunnerError:
            why = MACHINE_SILENT
        return {"machine": project.machine, "why": why}

    @staticmethod
    def _read_value(kind, setting, text):
        if kind is bool:
            if text not in ("true", "false"):
                raise ApiError(400, f"{setting} is true or false")
            return text == "true"
        try:
            return int(text)
        except ValueError:
            raise ApiError(400, f"{setting}: give a whole number") from None

    def change_project_settings(self, query, name):
        """Changes a project's settings. Each change is logged.

        `split_approval`, `queen_answers_one_way`, `worker_cli` and `reviewer_cli` are kept in Kandu's state, where they
        win over the registry; proposals already accepted, splits already made and workers or reviews already running
        stay as they are.
        `priority`, `daily_token_limit`, `auto_merge` and `frozen` are written in its entry in `projects.yaml`, and
        the next cycle uses them. Nothing is changed if any value is wrong.

        `name` renames the project, in a call of its own (`kandu.rename`): Kandu then knows it only by the new name.
        """
        project = self._project(name)
        given = {setting: values[0] for setting, values in query.items()}
        if not given:
            raise ApiError(400, "give a setting, such as split_approval=user")
        if "name" in given:
            return self._rename(project, given)
        changes, registry = {}, {}
        for setting, text in given.items():
            if setting in REGISTRY_SETTINGS:
                registry[setting] = self._read_value(REGISTRY_SETTINGS[setting], setting, text)
                continue
            value = {"true": True, "false": False}.get(text, text) if True in PROJECT_SETTINGS.get(setting, ()) else text
            if not is_project_setting(setting, value):
                raise ApiError(400, f"not a project setting: {setting}={text}")
            changes[setting] = value
        if registry:
            old = {setting: self._registry_value(project, setting) for setting in registry}
            try:
                with self._add_lock:
                    set_project_values(project.name, registry, self.local_dir)
            except ConfigError as e:
                raise ApiError(400, e.problems[0]) from None
            self.refresh_config()
            for setting, value in registry.items():
                self.store.record("project_setting_changed", project=project.name, setting=setting,
                                  old=old[setting], new=value)
        for setting, value in changes.items():
            old = self.store.project_setting(project, setting)
            self.store.set_project_setting(project.name, setting, value)
            self.store.record("project_setting_changed", project=project.name, setting=setting, old=old, new=value)
        return changes | registry

    def _rename(self, project, given):
        if len(given) > 1:
            raise ApiError(400, "change the name in a call of its own")
        try:
            with self._add_lock:
                result = rename.rename_project(project.name, given["name"], self.store, self.local_dir)
        except rename.RenameError as e:
            running = {"running": e.running} if e.running else None  # what to wait for or close, for the page to list
            raise ApiError(409 if str(e) == rename.RUNNING else 400, str(e), running) from None
        except ConfigError as e:
            raise ApiError(400, e.problems[0]) from None
        finally:
            self.refresh_config()
        return {"name": result["name"]}

    def _machines(self):
        """Each machine, and since when it has not answered, as the supervisor last saw it, or null while it does."""
        down = {m["machine"]: m["since"] for m in self.store.read_current().get("unreachable_machines") or []}
        return [{"name": m.name, "always_on": m.always_on, "down_since": down.get(m.name)}
                for m in self.config.settings.machines]

    def _project_tokens(self):
        """Each configured project's tokens today and its daily limit, counted as the supervisor counts them to start
        nothing new there; at or past its limit a project is over it. A new day counts from zero, from the local log."""
        today = self.store.today()
        return {p.name: {"today": self.store.tokens_counted(today, project=p.name), "limit": p.daily_token_limit}
                for p in self.config.projects}

    def _pace(self):
        supervisor = self.config.settings.supervisor
        return {setting: getattr(supervisor, setting) for setting in PACE_SETTINGS}

    def change_pace(self, query):
        """How often the supervisor wakes and how long a cycle may run, in `kandu.yaml`. The next cycle uses the
        cycle length; a new wake interval reaches the timer on "Restart Kandu"."""
        values = {setting: self._read_value(int, setting, values[0]) for setting, values in query.items()}
        old = self._pace()
        try:
            with self._add_lock:
                set_pace(values, self.local_dir)
        except ConfigError as e:
            raise ApiError(400, e.problems[0]) from None
        self.refresh_config()
        self.store.record("pace_changed", old=old, new=self._pace())
        return {"pace": self._pace()}

    def stop_worker(self, query, run_id):
        workers = self.store.read_current().get("workers", [])
        if not any(w.get("run_id") == run_id for w in workers):
            raise ApiError(404, "no such running worker")
        self.store.request_worker_stop(run_id)
        return {"stopping": run_id}


# The whole public surface; docs/API.md is tested against this table.
ROUTES = [
    ("GET", "/state", Api.state),
    ("GET", "/queue", Api.queue),
    ("GET", "/boards", Api.boards),
    ("GET", "/days", Api.days),
    ("GET", "/days/{day}", Api.day),
    ("GET", "/usage", Api.usage),
    ("GET", "/didban", Api.didban),
    ("GET", "/requests", Api.requests),
    ("POST", "/requests", Api.change_show_requests),
    ("POST", "/default-look", Api.change_default_look),
    ("GET", "/bazras", Api.bazras_reports),
    ("GET", "/flights", Api.list_flights),
    ("POST", "/flights/didban", Api.open_didban),
    ("POST", "/flights/zanbourdar", Api.open_zanbourdar),
    ("POST", "/flights/kanduban/{project}", Api.open_kanduban),
    ("POST", "/flights/tasks/{owner}/{repo}/{number}", Api.open_flight),
    ("POST", "/flights/{key}/close", Api.close_flight),
    ("GET", "/events", Api.events),
    ("GET", "/stream", None),
    ("GET", "/runs/{run_id}", Api.run),
    ("GET", "/tasks/{owner}/{repo}/{number}", Api.task),
    ("POST", "/tasks/{owner}/{repo}/{number}/send-back", Api.send_back),
    ("POST", "/tasks/{owner}/{repo}/{number}/to-queen", Api.to_queen),
    ("GET", "/reviews/{owner}/{repo}/{number}", Api.reviews),
    ("POST", "/reviews/{owner}/{repo}/{number}/approve", Api.approve),
    ("GET", "/splits/{owner}/{repo}/{number}", Api.split),
    ("POST", "/splits/{owner}/{repo}/{number}/approve", Api.approve_split),
    ("POST", "/drop-blockers", Api.change_drop_blockers),
    ("POST", "/notify", Api.change_notify),
    ("POST", "/telegram/token", Api.telegram_token),
    ("POST", "/telegram/chat", Api.telegram_chat),
    ("POST", "/telegram/chats", Api.telegram_chats),
    ("POST", "/telegram/test", Api.telegram_test),
    ("POST", "/telegram/buttons", Api.telegram_buttons),
    ("POST", "/clean-merged", Api.change_clean_merged),
    ("POST", "/drop-blockers/{owner}/{repo}/{number}", Api.drop_blocker),
    ("POST", "/stop", Api.stop),
    ("POST", "/resume", Api.resume),
    ("POST", "/limits", Api.change_limits),
    ("POST", "/parallel", Api.change_parallel),
    ("POST", "/pace", Api.change_pace),
    ("POST", "/restart", Api.restart),
    ("POST", "/machines", Api.add_machine),
    ("POST", "/projects/detect", Api.detect_project),
    ("POST", "/projects", Api.add_project),
    ("POST", "/wake", Api.wake),
    ("POST", "/workers/{run_id}/stop", Api.stop_worker),
    ("POST", "/projects/{name}/pause", Api.pause_project),
    ("POST", "/projects/{name}/resume", Api.resume_project),
    ("POST", "/projects/{name}/inspect", Api.change_inspect),
    ("POST", "/projects/{name}/settings", Api.change_project_settings),
    ("GET", "/projects/{name}/codex", Api.codex_on),
    ("POST", "/projects/{name}/remove", Api.remove_project),
    ("POST", "/backup", Api.change_backup),
    ("POST", "/upstream/check", Api.check_upstream),
    ("POST", "/upstream/bring-in", Api.bring_in_updates),
]


def _pattern(template):
    return re.compile(re.sub(r"\{\w+\}", r"([^/]+)", re.escape(template).replace(r"\{", "{").replace(r"\}", "}")))


COMPILED = [(method, _pattern(API + template), handler) for method, template, handler in ROUTES if handler]


def config_stamp(local_dir):
    """When the two config files last changed, so a change is noticed without reading them."""
    stamp = []
    for name in ("kandu.yaml", "projects.yaml"):
        try:
            stamp.append((Path(local_dir) / name).stat().st_mtime_ns)
        except OSError:
            stamp.append(None)
    return tuple(stamp)


def dispatch(api, method, target, supplied_token=None, body=""):
    """Answers one request; returns (status, payload). A POST's form body adds to its query: a secret goes there, so
    it is in no URL and no log."""
    api.refresh_config()
    url = urlparse(target)
    allowed = False
    for route_method, pattern, handler in COMPILED:
        match = pattern.fullmatch(url.path)
        if match is None:
            continue
        allowed = True
        if route_method != method:
            continue
        if method != "GET" and not _token_ok(api.token, supplied_token):
            return 403, {"error": "a valid token is required"}
        try:
            query = parse_qs(url.query)
            leaked = [name for name in BODY_ONLY if name in query]
            if leaked:
                return 400, {"error": f"send {leaked[0]} in the request's body, not in the URL"}
            for key, values in parse_qs(body or "").items():
                query.setdefault(key, values)
            return 200, handler(api, query, *map(unquote, match.groups()))
        except ApiError as e:
            return e.status, {"error": e.message, **e.extra}
        except GitHubError:
            return 502, {"error": "GitHub could not be read"}
    return (405, {"error": "method not allowed"}) if allowed else (404, {"error": "not found"})


def _token_ok(expected, supplied):
    return bool(expected) and bool(supplied) and hmac.compare_digest(expected.encode(), supplied.encode())


def new_events(store, position):
    """Returns the events after `position`, a (day, count) pair, and the new position."""
    days = store.days()
    if not days:
        return [], position
    day, count = position
    if day not in days:
        day, count = days[-1], 0
    events = store.events(day)[count:]
    count += len(events)
    if day != days[-1]:
        return events, (days[days.index(day) + 1], 0)
    return events, (day, count)


def _start_position(store):
    days = store.days()
    return (days[-1], len(store.events(days[-1]))) if days else (None, 0)


def make_handler(api):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _send(self, status, body, content_type, cache=True):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            if not cache:  # a page kept by the browser or a proxy would hide an update of Kandu
                self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _answer(self, method):
            auth = self.headers.get("Authorization", "")
            length = int(self.headers.get("Content-Length") or 0) if method == "POST" else 0
            if length > MAX_BODY:
                self.close_connection = True  # its body is left unread
                self._send(413, json.dumps({"error": "the request is too long"}).encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            body = self.rfile.read(length).decode("utf-8", "replace") if length > 0 else ""
            status, payload = dispatch(api, method, self.path, auth.removeprefix("Bearer ") or None, body)
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _stream(self):
            position = _start_position(api.store)  # before the headers, so no event can fall between
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    events, position = new_events(api.store, position)
                    for event in events:
                        self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.write(b": alive\n\n")
                    self.wfile.flush()
                    time.sleep(STREAM_POLL_SECONDS)
            except (BrokenPipeError, ConnectionResetError):
                return

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                api.refresh_config()  # a default look changed by hand counts at once
                page = page_for(self.headers.get("Cookie"), api.config.settings.dashboard.look)
                self._send(200, page.read_bytes(), "text/html; charset=utf-8", cache=False)
            elif path == f"/{FONT.name}":
                self._send(200, FONT.read_bytes(), "font/woff2")
            elif path == f"{API}/stream":
                self._stream()
            else:
                self._answer("GET")

        def do_POST(self):
            self._answer("POST")

    return Handler


def page_for(cookie_header, default_look="classic"):
    """The page of the look a browser chose, from its Cookie header. A browser that chose nothing, or a look that is
    gone such as the honeycomb, opens in the install's default look. The header is split by hand: the cookies of
    127.0.0.1 are shared by every local app, and one malformed cookie would stop a strict parser before it reaches ours."""
    default = LOOKS.get(default_look, CLASSIC_PAGE)
    for pair in (cookie_header or "").split(";"):
        name, _, value = pair.partition("=")
        if name.strip() == LOOK_COOKIE:
            return LOOKS.get(value.strip(), default)
    return default


def main():
    parser = argparse.ArgumentParser(description="Kandu's dashboard.")
    parser.add_argument("--local", default=DEFAULT_LOCAL_DIR, help="config directory (default: local/)")
    args = parser.parse_args()
    try:
        config = load_config(args.local)
    except ConfigError as e:
        print(f"dashboard: {len(e.problems)} problem(s) in config; run: python -m kandu.config", file=sys.stderr)
        return 1
    api = Api(config, StateStore(), open_github(config), DEFAULT_LOGS_DIR, os.environ.get(TOKEN_ENV), local_dir=args.local)
    bind, port = config.settings.dashboard.bind, config.settings.dashboard.port
    ghmeter.sample_forever()  # the limit as GitHub sees it, beside the calls the meter counts
    upstream.tick_forever(api.upstream_tick)  # does nothing while `upstream.enabled` is off
    threading.Thread(target=api.bot.run_forever, daemon=True).start()  # Telegram's buttons; idle without a token
    server = ThreadingHTTPServer((bind, port), make_handler(api))
    print(f"dashboard: http://{bind}:{port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
