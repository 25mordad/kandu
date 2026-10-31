"""Kandu's dashboard and its public HTTP API (DESIGN §7). Documented in docs/API.md.

The page in dashboard.html uses only this API, so all it shows is open to any
other system. Reads are open on the bound address; controls need a token.
"""
import argparse
import hmac
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import queen, questions, review, sessions, split, timer
from .config import DEFAULT_LOCAL_DIR, TECHNICAL_DEBT, ConfigError, load_config, set_limits
from .github import AGENT_LABEL, BoardCache, GitHub, GitHubError
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
DONE_KEEP_DAYS = 5  # A finished task stays in the hive this long, so the recent work stays in sight.
QUEUE_COLUMNS = (DONE, "Backlog", "Ready", "In progress", "In review", "Needs info", "Needs split", "Blocked", TECHNICAL_DEBT)
TOOL_DETAIL_KEYS = ("file_path", "path", "pattern", "command")
MAX_DETAIL = 200
USER_DECIDES = "the user decides next"


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
    for event in events:
        task, kind = event.get("task"), event["type"]
        if task is None:
            continue
        mark = marks.setdefault(task, {"size": None, "door": None, "sent_back": False, "slice": False, "pr": None,
                                       "questions": 0})
        if kind == "task_sized":
            mark |= {"size": event["size"], "door": event["door"]}
        elif kind == "slice_created":
            mark["slice"] = True
        elif kind == "review_finished":
            mark |= {"door": event["door"], "sent_back": bool(event["blockers"])}
        elif kind == "decision" and (event["decision"] == "merge_waits" or event["reason"] == USER_DECIDES):
            mark["pr"] = "waits"
        elif kind == "user_approved":
            mark["pr"] = "approved"
        elif kind == "task_picked":
            mark["pr"] = None
        elif kind == "task_finished":
            if event["outcome"] in questions.ENDED:
                del marks[task]
            else:
                mark["pr"] = None
    for question in questions.open_questions(store, events):
        if question["task"] in marks:
            marks[question["task"]]["questions"] += 1
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


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Api:
    def __init__(self, config, store, github, logs_dir, token=None, clock=time.monotonic, start_cycle=timer.start_cycle,
                 local_dir=DEFAULT_LOCAL_DIR, timer_on=timer.timer_on):
        self.config = config
        self.store = store
        self.github = github
        self.logs_dir = Path(logs_dir)
        self.token = token
        self.clock = clock
        self.start_cycle = start_cycle
        self.timer_on = timer_on
        self.local_dir = local_dir
        self._queue = None
        self._queue_time = None
        self._queue_failed = False
        self._queue_finished = None
        self._queue_lock = threading.Lock()

    def state(self, query):
        limits = self.config.settings.claude.limits
        return self.store.read_current() | {
            "timer_on": self.timer_on(),
            "stop_requested": self.store.stop_file.exists(),
            "paused_projects": sorted(self._paused()),
            "paused_in_config": sorted(p.name for p in self.config.projects if p.paused),
            "language": self.config.settings.language,
            "limits": {"session": limits.session_5h_percent, "week": limits.weekly_percent},
            "queen": self._queen_report(),
            "flowers": flower_marks(self.store),
        }

    def _paused(self):
        return self.store.paused_projects() | {p.name for p in self.config.projects if p.paused}

    def _queen_report(self):
        report = queen.last_report(self.store)
        if report is None:
            return None
        return report | {"today": self.store.tokens_counted(self.store.today(), role="queen"),
                         "limit": self.config.settings.supervisor.queen_daily_token_limit}

    def queue(self, query):
        """Each project's columns. All pages share one read, and a failed read keeps the last good answer.

        A task that finished moved on the board, so it counts as asking for a fresh read.
        """
        with self._queue_lock:  # A page that arrives during a read gets its result.
            finished = self.store.last_event("task_finished")
            fresh = query.get("fresh") == ["1"] or finished != self._queue_finished
            wait = QUEUE_RETRY_SECONDS if self._queue_failed else QUEUE_FRESH_SECONDS if fresh else QUEUE_CACHE_SECONDS
            if self._queue_time is None or self.clock() - self._queue_time >= wait:
                self._queue_time = self.clock()
                self._queue_finished = finished
                try:
                    self._queue = self._read_queue()
                    self._queue_failed = False
                except GitHubError:
                    self._queue_failed = True
            if self._queue is None:
                raise ApiError(502, "GitHub could not be read")
            return self._queue | {"stale": self._queue_failed}

    def _read_queue(self):
        projects = {p.name: {c: [] for c in QUEUE_COLUMNS} for p in self.config.projects}
        boards = {}
        for project in self.config.projects:
            if project.board is not None:
                boards.setdefault((project.board.owner, project.board.number), project.board)
        by_repo = {repo: p.name for p in self.config.projects for repo in p.repos}
        for board in boards.values():
            for item in self.github.board_items(board):
                name = by_repo.get(item.repo)
                if name is None or AGENT_LABEL not in item.labels or item.status not in QUEUE_COLUMNS:
                    continue
                if item.status == DONE and not closed_lately(item):
                    continue
                task = f"{item.repo}#{item.number}"
                if item.status == split.NEEDS_SPLIT and self.store.last_event("task_split", task=task) is not None:
                    continue  # Accepted: it waits for nobody, and its slices stand for it until the last one closes it.
                projects[name][item.status].append({"task": task, "title": item.title, "url": item.url})
        return {"projects": projects, "done_days": DONE_KEEP_DAYS}

    def days(self, query):
        return {"days": self.store.days()}

    def didban(self, query):
        return {"sessions": sessions.recent(self.store)}

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
        rounds = review.reviews_on(pr, self.config.settings.github.account)
        return {"task": task, "pr": {"number": pr["number"], "title": pr["title"], "url": pr["url"],
                                     "state": pr["state"], "head_sha": head,
                                     "approved": self.store.last_event("user_approved", task=task,
                                                                       head_sha=head) is not None,
                                     "can_approve": why_not is None, "why_not": why_not},
                "rounds": [r | {"recommendation": recommendation(r), "user_brief": self._user_brief(full, number, r)}
                           for r in rounds]}

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
        status = next((i.status for i in self.github.board_items(project.board)
                       if i.repo == repo and i.number == int(number)), None)
        if status not in MERGEABLE_COLUMNS:
            return f"its task is in {status or 'no column'}, so no cycle would pick it up"
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

    def stop(self, query):
        self.store.request_stop()
        return {"stop_requested": True}

    def resume(self, query):
        self.store.stop_file.unlink(missing_ok=True)
        return {"stop_requested": False}

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
    ("GET", "/days", Api.days),
    ("GET", "/days/{day}", Api.day),
    ("GET", "/didban", Api.didban),
    ("GET", "/events", Api.events),
    ("GET", "/stream", None),
    ("GET", "/runs/{run_id}", Api.run),
    ("GET", "/tasks/{owner}/{repo}/{number}", Api.task),
    ("GET", "/reviews/{owner}/{repo}/{number}", Api.reviews),
    ("POST", "/reviews/{owner}/{repo}/{number}/approve", Api.approve),
    ("GET", "/splits/{owner}/{repo}/{number}", Api.split),
    ("POST", "/splits/{owner}/{repo}/{number}/approve", Api.approve_split),
    ("POST", "/stop", Api.stop),
    ("POST", "/resume", Api.resume),
    ("POST", "/limits", Api.change_limits),
    ("POST", "/workers/{run_id}/stop", Api.stop_worker),
    ("POST", "/projects/{name}/pause", Api.pause_project),
    ("POST", "/projects/{name}/resume", Api.resume_project),
]


def _pattern(template):
    return re.compile(re.sub(r"\{\w+\}", r"([^/]+)", re.escape(template).replace(r"\{", "{").replace(r"\}", "}")))


COMPILED = [(method, _pattern(API + template), handler) for method, template, handler in ROUTES if handler]


def dispatch(api, method, target, supplied_token=None):
    """Answers one request; returns (status, payload)."""
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
    api = Api(config, StateStore(), GitHub(cache=BoardCache()), DEFAULT_LOGS_DIR, os.environ.get(TOKEN_ENV), local_dir=args.local)
    bind, port = config.settings.dashboard.bind, config.settings.dashboard.port
    server = ThreadingHTTPServer((bind, port), make_handler(api))
    print(f"dashboard: http://{bind}:{port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
