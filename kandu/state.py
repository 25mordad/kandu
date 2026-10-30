"""State files and the event log (DESIGN §7). The format is documented in docs/STATE.md.

The event log is append-only and is the single record of what happened; token
reports are always computed from it. Each event type has a fixed set of fields,
so nothing lands in the log through an extra field. Values are not inspected:
a caller must never pass a prompt, command output or a secret in one.
"""
import json
import os
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
    "task_finished": TASK_FIELDS + ("outcome",),
}
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
        if fields.get("role") != "supervisor" and None in (fields.get(name, "") for name in TASK_FIELDS):
            raise StateError(f"{event_type}: only a supervisor run may have no task")
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
