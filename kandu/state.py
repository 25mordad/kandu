"""State files and the event log (DESIGN §7). The format is documented in docs/STATE.md.

The event log is append-only and is the single record of what happened; token
reports are always computed from it. Each event type has a fixed set of fields,
so prompts, command output or secrets cannot end up in the log by accident.
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

    def record(self, event_type, now=None, **fields):
        """Appends one event to today's log and returns it."""
        if event_type not in EVENT_FIELDS:
            raise StateError(f"unknown event type: {event_type!r}")
        expected = set(EVENT_FIELDS[event_type])
        if set(fields) != expected:
            raise StateError(f"{event_type}: fields must be {sorted(expected)}, got {sorted(fields)}")
        now = now or datetime.now().astimezone()
        event = {"v": FORMAT_VERSION, "ts": now.isoformat(timespec="seconds"), "type": event_type} | fields
        self.events_dir.mkdir(parents=True, exist_ok=True)
        path = self.events_dir / f"{now.date().isoformat()}.jsonl"
        line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        # One O_APPEND write, so parallel workers never interleave lines.
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        return event

    def days(self):
        """Dates that have events, oldest first."""
        return sorted(p.stem for p in self.events_dir.glob("*.jsonl"))

    def events(self, day):
        """Returns one day's events; a line cut off by a crash is skipped."""
        path = self.events_dir / f"{day}.jsonl"
        if not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def tokens_for_day(self, day):
        """One day's tokens, split by task, model and role."""
        report = {"day": day, "total": _empty_tokens(), "tasks": {}, "models": {}, "roles": {}}
        for event in self.events(day):
            if event["type"] != "run_finished":
                continue
            for model, usage in event["models"].items():
                _add_tokens(report["total"], usage)
                _add_tokens(report["tasks"].setdefault(event["task"], _empty_tokens()), usage)
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
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.current_file.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"v": FORMAT_VERSION} | snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.current_file)
