"""State files and the event log (DESIGN §7). The format is documented in docs/STATE.md.

The event log is append-only and is the single record of what happened; token
reports are always computed from it. Each event type has a fixed set of fields,
so nothing lands in the log through an extra field. Values are not inspected:
a caller must never pass a prompt, command output or a secret in one.
"""
import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DEFAULT_STATE_DIR = Path(__file__).resolve().parent.parent / "state"
FORMAT_VERSION = 1

TASK_FIELDS = ("task", "project")
EVENT_FIELDS = {
    "supervisor_woke": (),
    "supervisor_slept": ("reason", "next_wake"),
    "task_picked": TASK_FIELDS,
    "phase_changed": TASK_FIELDS + ("phase",),
    "decision": TASK_FIELDS + ("decision", "reason"),
    "run_finished": TASK_FIELDS + ("run_id", "role", "models", "duration_ms", "is_error"),
    "review_finished": TASK_FIELDS + ("run_id", "round", "head_sha", "findings", "blockers", "door", "criteria_met",
                                      "wait_for_user", "user_brief"),
    "notes_sized": TASK_FIELDS + ("run_id", "head_sha", "notes"),
    "debt_reported": TASK_FIELDS + ("finding", "title", "body", "size", "reason"),
    "debt_parked": TASK_FIELDS + ("finding", "url", "note"),
    "user_approved": TASK_FIELDS + ("head_sha",),
    "priority_raised": TASK_FIELDS,
    "task_sized": TASK_FIELDS + ("run_id", "size", "files", "door", "criteria", "slices"),
    "slice_created": TASK_FIELDS + ("parent",),
    "task_split": TASK_FIELDS + ("children",),
    "split_declined": TASK_FIELDS,
    "question_answered": TASK_FIELDS + ("head_sha", "question", "answer"),
    "group_closed": TASK_FIELDS,
    "deployed": TASK_FIELDS + ("run_id",),
    "task_finished": TASK_FIELDS + ("outcome",),
    "project_checked": ("project", "passed", "detail"),
    "project_paused": ("project",),
    "project_resumed": ("project",),
    "limits_changed": ("session", "week"),
    "queen_decided": TASK_FIELDS + ("action", "note"),
    "queen_finished": ("run_id", "brief", "backlog"),
    "notification_sent": ("kind", "key"),
    "notification_failed": ("kind", "key", "reason"),
}
NO_TASK_ROLES = ("supervisor", "queen")
TOKEN_KINDS = ("input", "output", "cache_read", "cache_creation")


class StateError(Exception):
    pass


def usage_from_result(result):
    """Takes the `result` event of a `stream-json` run; returns its run_finished fields."""
    models = {}
    for model, usage in result.get("modelUsage", {}).items():
        models[model] = {
            "input": usage.get("inputTokens", 0),
            "output": usage.get("outputTokens", 0),
            "cache_read": usage.get("cacheReadInputTokens", 0),
            "cache_creation": usage.get("cacheCreationInputTokens", 0),
        }
    return {
        "models": models,
        "duration_ms": result.get("duration_ms", 0),
        "is_error": bool(result.get("is_error", False)),
    }


def _empty_tokens():
    return {kind: 0 for kind in TOKEN_KINDS} | {"total": 0}


def _add_tokens(target, usage):
    for kind in TOKEN_KINDS:
        target[kind] += usage.get(kind, 0)
        target["total"] += usage.get(kind, 0)


class StateStore:
    def __init__(self, root=DEFAULT_STATE_DIR):
        self.root = Path(root)
        self.events_dir = self.root / "events"
        self.current_file = self.root / "current.json"
        self.stop_file = self.root / "stopped"
        self.worker_stops_dir = self.root / "stop-workers"
        self.paused_dir = self.root / "paused"
        self.splits_dir = self.root / "splits"
        self.requests_dir = self.root / "requests"
        self.didban_dir = self.root / "didban"
        self.listeners = []  # Called with each event once it is in the log.

    def _make_dirs(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.events_dir.mkdir(mode=0o700, exist_ok=True)

    def record(self, event_type, now=None, **fields):
        """Appends one event to today's log and returns it."""
        if event_type not in EVENT_FIELDS:
            raise StateError(f"unknown event type: {event_type!r}")
        expected = set(EVENT_FIELDS[event_type])
        if set(fields) != expected:
            raise StateError(f"{event_type}: fields must be {sorted(expected)}, got {sorted(fields)}")
        if fields.get("role") not in NO_TASK_ROLES and None in (fields.get(name, "") for name in TASK_FIELDS):
            raise StateError(f"{event_type}: only a supervisor or queen run may have no task")
        now = now or datetime.now().astimezone()
        event = {"v": FORMAT_VERSION, "ts": now.isoformat(timespec="seconds"), "type": event_type} | fields
        self._make_dirs()
        path = self.events_dir / f"{now.date().isoformat()}.jsonl"
        line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        # One O_APPEND write, so parallel workers never interleave lines.
        fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                line = b"\n" + line  # A crash cut the last line off; keep this one apart from it.
            os.write(fd, line)
        finally:
            os.close(fd)
        for listen in self.listeners:
            listen(event)
        return event

    def days(self):
        """Dates that have events, oldest first."""
        return sorted(p.stem for p in self.events_dir.glob("*.jsonl"))

    def events(self, day):
        """Returns one day's events; a line cut off by a crash is skipped.

        Lines are split on newlines only: JSON text may hold other line separators.
        """
        path = self.events_dir / f"{day}.jsonl"
        if not path.exists():
            return []
        events = []
        for line in path.read_bytes().split(b"\n"):
            try:
                events.append(json.loads(line.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        return events

    def _split_file(self, task):
        return self.splits_dir / f"{task.replace('/', '__').replace('#', '-')}.json"

    def write_split(self, task, proposal):
        """A proposal stays here, not on the issue: it is the splitter's text and the repo may be public."""
        self._make_dirs()
        self.splits_dir.mkdir(mode=0o700, exist_ok=True)
        path = self._split_file(task)
        path.write_text(json.dumps({"task": task} | proposal, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)

    def read_split(self, task):
        path = self._split_file(task)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def drop_split(self, task):
        self._split_file(task).unlink(missing_ok=True)

    def splits(self):
        if not self.splits_dir.exists():
            return []
        paths = sorted(self.splits_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        return [json.loads(path.read_text(encoding="utf-8")) for path in paths]

    def last_event(self, event_type, **match):
        """Returns the newest event of this type with these field values, or None."""
        for day in reversed(self.days()):
            for event in reversed(self.events(day)):
                if event["type"] == event_type and all(event.get(k) == v for k, v in match.items()):
                    return event
        return None

    def tokens_for_day(self, day):
        """One day's tokens, split by task, model and role. Runs with no task go under `no_task`."""
        report = {"day": day, "total": _empty_tokens(), "tasks": {}, "no_task": _empty_tokens(),
                  "models": {}, "roles": {}}
        for event in self.events(day):
            if event["type"] != "run_finished":
                continue
            for model, usage in event["models"].items():
                task = event["task"]
                _add_tokens(report["total"], usage)
                _add_tokens(report["no_task"] if task is None else report["tasks"].setdefault(task, _empty_tokens()), usage)
                _add_tokens(report["models"].setdefault(model, _empty_tokens()), usage)
                _add_tokens(report["roles"].setdefault(event["role"], _empty_tokens()), usage)
        return report

    def task_cost(self, task):
        """What one task spent over its whole life, in all and by role."""
        report = {"total": _empty_tokens(), "runs": 0, "duration_ms": 0, "roles": {}}
        for day in self.days():
            for event in self.events(day):
                if event["type"] != "run_finished" or event["task"] != task:
                    continue
                role = report["roles"].setdefault(event["role"], {"tokens": _empty_tokens(), "runs": 0, "duration_ms": 0})
                for side, tokens in ((report, report["total"]), (role, role["tokens"])):
                    side["runs"] += 1
                    side["duration_ms"] += event["duration_ms"]
                    for usage in event["models"].values():
                        _add_tokens(tokens, usage)
        return report

    def today(self):
        return datetime.now().astimezone().date().isoformat()

    def tokens_counted(self, day, *, project=None, role=None):
        """The tokens that count against a daily limit. A cache read is far cheaper, so it does not count."""
        runs = [e for e in self.events(day) if e["type"] == "run_finished"
                and (project is None or e["project"] == project) and (role is None or e["role"] == role)]
        return sum(usage.get(kind, 0) for run in runs for usage in run["models"].values()
                   for kind in TOKEN_KINDS if kind != "cache_read")

    def request_stop(self):
        self._make_dirs()
        self.stop_file.touch(mode=0o600)

    def pause_project(self, name):
        self._make_dirs()
        self.paused_dir.mkdir(mode=0o700, exist_ok=True)
        (self.paused_dir / name).touch(mode=0o600)

    def resume_project(self, name):
        (self.paused_dir / name).unlink(missing_ok=True)

    def paused_projects(self):
        return {path.name for path in self.paused_dir.iterdir()} if self.paused_dir.exists() else set()

    @contextmanager
    def split_lock(self):
        """A split is accepted from the dashboard or from Didban, two processes that must not both make the group."""
        self._make_dirs()
        with open(self.root / "split.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def write_request(self, request):
        """A request is the user's own words, so it stays out of the event log."""
        self._make_dirs()
        self.requests_dir.mkdir(mode=0o700, exist_ok=True)
        path = self.requests_dir / f"{request['id']}.json"
        path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)

    def drop_request(self, request_id):
        (self.requests_dir / f"{request_id}.json").unlink(missing_ok=True)

    def requests(self):
        if not self.requests_dir.exists():
            return []
        paths = sorted(self.requests_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        return [json.loads(path.read_text(encoding="utf-8")) for path in paths]

    def write_didban_session(self, summary):
        """A later report of the same session replaces the earlier one."""
        self._make_dirs()
        self.didban_dir.mkdir(mode=0o700, exist_ok=True)
        path = self.didban_dir / f"{summary['id']}.json"
        path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)

    def didban_sessions(self):
        if not self.didban_dir.exists():
            return []
        sessions = [json.loads(path.read_text(encoding="utf-8")) for path in self.didban_dir.glob("*.json")]
        return sorted(sessions, key=lambda s: s.get("started") or "")

    def worker_stop_file(self, run_id):
        return self.worker_stops_dir / run_id

    def request_worker_stop(self, run_id):
        self._make_dirs()
        self.worker_stops_dir.mkdir(mode=0o700, exist_ok=True)
        self.worker_stop_file(run_id).touch(mode=0o600)

    def read_current(self):
        """Returns the current snapshot, or {} if there is none yet."""
        if not self.current_file.exists():
            return {}
        return json.loads(self.current_file.read_text(encoding="utf-8"))

    def write_current(self, snapshot):
        """Replaces the snapshot atomically, so a reader never sees half a file."""
        self._make_dirs()
        tmp = self.current_file.with_suffix(f".{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"v": FORMAT_VERSION} | snapshot, f, ensure_ascii=False, indent=2)
        tmp.replace(self.current_file)
