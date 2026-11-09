"""Kandu's dashboard and its public HTTP API (DESIGN §7). Documented in docs/API.md.

The page in dashboard.html uses only this API, so all it shows is open to any
other system. Reads are open on the bound address; controls need a token.
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
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import backup, bazras, flights, ghmeter, queen, questions, review, sessions, split, tasks, timer
from .config import (DEFAULT_LOCAL_DIR, TECHNICAL_DEBT, ConfigError, Inspect, load_config, set_backup,
                     set_drop_blockers, set_inspect, set_limits, set_parallel, set_show_requests)
from .github import AGENT_LABEL, GitHubError, task_blockers
from .mirror import FRESH_SECONDS, open_github
from .runner import DEFAULT_LOGS_DIR
from .state import StateStore
from .tasks import TaskError

PAGE = Path(__file__).with_name("dashboard.html")
FONT = Path(__file__).with_name("vazirmatn.woff2")
TOKEN_ENV = "KANDU_DASHBOARD_TOKEN"
API = "/api/v1"
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
USER_DECIDES = "the user decides next"
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
        elif kind == "decision" and event["reason"] == USER_DECIDES:
            mark |= {"pr": "waits", "yours": True}
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
    for question in questions.open_questions(store, events):
        if question["task"] in marks:
            marks[question["task"]]["questions"] += 1
    for asked in (queen.last_report(store) or {}).get("questions", []):
        if asked["task"] in marks:
            marks[asked["task"]]["yours"] = True
    return marks


def actions_in(line):
    """Takes one `stream-json` line; returns what the agent did in it. Tool results stay out."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []
    message = event.get("message") if isinstance(event, dict) and event.get("type") == "assistant" else None
    content = message.get("content") if isinstance(message, dict) else None
    actions = []
    for part in content if isinstance(content, list) else []:
        if isinstance(part, dict) and part.get("type") == "tool_use":
            tool_input = part.get("input") if isinstance(part.get("input"), dict) else {}
            detail = next((str(tool_input[k]) for k in TOOL_DETAIL_KEYS if k in tool_input), "")
            actions.append({"tool": part.get("name", ""), "detail": detail[:MAX_DETAIL]})
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
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Api:
    def __init__(self, config, store, github, logs_dir, token=None, clock=time.monotonic, start_cycle=timer.start_cycle,
                 local_dir=DEFAULT_LOCAL_DIR, timer_on=timer.timer_on, flights_=None, restart=timer.restart,
                 backup_timer=timer.enable_backup):
        self.config = config
        self.store = store
        self.github = github
        self.logs_dir = Path(logs_dir)
        self.token = token
        self.clock = clock
        self.start_cycle = start_cycle
        self.timer_on = timer_on
        self.local_dir = local_dir
        self.flights = flights_ or flights.Flights(config, store, logs_dir)
        self.restart_kandu = restart
        self.backup_timer = backup_timer
        self._config_stamp = config_stamp(local_dir)
        self._config_lock = threading.Lock()
        self._queue = None
        self._queue_time = None
        self._queue_failed = False
        self._queue_finished = None
        self._queue_versions = None
        self._queue_lock = threading.Lock()

    def refresh_config(self):
        """Takes a changed config without a restart, so a new project shows in its own patch.

        A config with a problem leaves the last good one in place; `python -m kandu.config` names the problem.
        """
        with self._config_lock:
            stamp = config_stamp(self.local_dir)
            if stamp == self._config_stamp:
                return
            self._config_stamp = stamp
            try:
                config = load_config(self.local_dir)
            except ConfigError:
                return
            self.config = config
            self.flights.config = config
            self._queue_time = None  # a new project's tasks need a new read

    def state(self, query):
        limits = self.config.settings.claude.limits
        marks = flower_marks(self.store)
        return self.store.read_current() | {
            "timer_on": self.timer_on(),
            "stop_requested": self.store.stop_file.exists(),
            "paused_projects": sorted(self._paused()),
            "paused_in_config": sorted(p.name for p in self.config.projects if p.paused),
            "project_order": [p.name for p in sorted(self.config.projects, key=lambda p: p.priority)],
            "language": self.config.settings.language,
            "limits": {"session": limits.session_5h_percent, "week": limits.weekly_percent},
            "parallel": self._parallel(),
            "queen": self._queen_report(),
            "flowers": marks,
            "waits_for_you": self._waits_for_you(marks),
            "bazras": {"running": self.store.read_bazras(),
                       "last": {p.name: bazras.last_look(p.name, self.local_dir) for p in bazras.inspected(self.config)}},
            "backup": self._backup(),
            "drop_blockers": self.config.settings.supervisor.queen_drop_blockers,
            "show_requests": self.config.settings.dashboard.show_requests,
        }

    def _waits_for_you(self, marks):
        """How many tasks wait for the user: those the log marks as theirs, and each open by-hand task, once.

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
            by_hand = AGENT_LABEL not in item.labels
            # One in an open column waits for the user; one done or closed waits for nobody.
            yours = by_hand and item.status != DONE and not item.closed_at
            entry = {"task": task, "title": item.title, "url": item.url, "by_hand": by_hand, "yours": yours}
            if item.status in MERGEABLE_COLUMNS:
                entry["blockers"] = self._blockers(item, columns)
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
            return {"run_id": run_id, "actions": [a for line in log for a in actions_in(line)]}

    def reviews(self, query, owner, repo, number):
        full = f"{owner}/{repo}"
        if not number.isdigit() or not any(full in p.repos for p in self.config.projects):
            raise ApiError(404, "no such task")
        pr = self.github.find_pr(full, int(number))
        if pr is None:
            return {"task": f"{full}#{number}", "pr": None, "rounds": []}
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
                "blockers": blockers, "waited": waited, "holds": holds,
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
        pr = self.github.find_pr(item.repo, item.number)
        if pr is not None and pr["state"] == "MERGED":
            raise ApiError(409, "its PR is already merged")
        events = queen.all_events(self.store)
        to = split.BACKLOG if queen.last_outcome(events, key) == "sizing_failed" else "Ready"
        self.github.set_status(project.board, item.url, to)
        self.store.record("user_resumed", task=key, project=project.name, to=to)
        self._queue_time = None
        return {"task": key, "to": to, "cycle_started": self.start_cycle()}

    def task(self, query, owner, repo, number):
        """What a task was and what it cost. An unreadable issue costs only its text, not the numbers."""
        key = self._split_task(owner, repo, number)
        try:
            text = self.github.issue_text(f"{owner}/{repo}", int(number))
        except GitHubError:
            text = None
        finished = self.store.last_event("task_finished", task=key)
        return {"task": key, "issue": text, "outcome": finished and finished["outcome"],
                "cost": self.store.task_cost(key)}

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

    def stop(self, query):
        self.store.request_stop()
        return {"stop_requested": True}

    def resume(self, query):
        self.store.stop_file.unlink(missing_ok=True)
        return {"stop_requested": False}

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

    def change_limits(self, query):
        """Reaches the next cycle through `kandu.yaml`; a cycle already running keeps the limits it read."""
        try:
            session, week = int(query["session"][0]), int(query["week"][0])
            set_limits(session, week, self.local_dir)
        except (KeyError, ValueError):
            raise ApiError(400, "give both limits, as whole percents") from None
        except ConfigError as e:
            raise ApiError(400, e.problems[0]) from None
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
    ("GET", "/didban", Api.didban),
    ("GET", "/requests", Api.requests),
    ("POST", "/requests", Api.change_show_requests),
    ("GET", "/bazras", Api.bazras_reports),
    ("GET", "/flights", Api.list_flights),
    ("POST", "/flights/didban", Api.open_didban),
    ("POST", "/flights/zanbourdar", Api.open_zanbourdar),
    ("POST", "/flights/tasks/{owner}/{repo}/{number}", Api.open_flight),
    ("POST", "/flights/{key}/close", Api.close_flight),
    ("GET", "/events", Api.events),
    ("GET", "/stream", None),
    ("GET", "/runs/{run_id}", Api.run),
    ("GET", "/tasks/{owner}/{repo}/{number}", Api.task),
    ("POST", "/tasks/{owner}/{repo}/{number}/send-back", Api.send_back),
    ("GET", "/reviews/{owner}/{repo}/{number}", Api.reviews),
    ("POST", "/reviews/{owner}/{repo}/{number}/approve", Api.approve),
    ("GET", "/splits/{owner}/{repo}/{number}", Api.split),
    ("POST", "/splits/{owner}/{repo}/{number}/approve", Api.approve_split),
    ("POST", "/drop-blockers", Api.change_drop_blockers),
    ("POST", "/drop-blockers/{owner}/{repo}/{number}", Api.drop_blocker),
    ("POST", "/stop", Api.stop),
    ("POST", "/resume", Api.resume),
    ("POST", "/limits", Api.change_limits),
    ("POST", "/parallel", Api.change_parallel),
    ("POST", "/restart", Api.restart),
    ("POST", "/wake", Api.wake),
    ("POST", "/workers/{run_id}/stop", Api.stop_worker),
    ("POST", "/projects/{name}/pause", Api.pause_project),
    ("POST", "/projects/{name}/resume", Api.resume_project),
    ("POST", "/projects/{name}/inspect", Api.change_inspect),
    ("POST", "/backup", Api.change_backup),
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


def dispatch(api, method, target, supplied_token=None):
    """Answers one request; returns (status, payload)."""
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
            return 200, handler(api, parse_qs(url.query), *map(unquote, match.groups()))
        except ApiError as e:
            return e.status, {"error": e.message}
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

        def _send(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _answer(self, method):
            auth = self.headers.get("Authorization", "")
            status, payload = dispatch(api, method, self.path, auth.removeprefix("Bearer ") or None)
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
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path == f"/{FONT.name}":
                self._send(200, FONT.read_bytes(), "font/woff2")
            elif path == f"{API}/stream":
                self._stream()
            else:
                self._answer("GET")

        def do_POST(self):
            self._answer("POST")

    return Handler


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
    server = ThreadingHTTPServer((bind, port), make_handler(api))
    print(f"dashboard: http://{bind}:{port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
