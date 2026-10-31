"""What each Didban session spent and did, for the dashboard (DESIGN §13).

The transcript stays on the user's machine. Didban reads it when a session ends and
sends the brain only this summary, which no model writes, so a report costs no tokens.
It is kept apart from the event log, so it counts against no project's limits.
"""
import argparse
import json
import re
import sys
from pathlib import Path

from .state import TOKEN_KINDS, StateStore

KEPT = 30
SESSION_ID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")
FIELDS = {"input": "input_tokens", "output": "output_tokens", "cache_read": "cache_read_input_tokens",
          "cache_creation": "cache_creation_input_tokens"}
# What the session did, as the commands of the Didban role show it.
ACTIONS = {"requests": ("kandu.intake add",), "tasks": ("kandu.tasks", "kandu.debt promote"),
           "answers": ("kandu.questions answer",)}


class SessionError(Exception):
    pass


def transcript(session_id, projects_dir=None):
    """Claude Code keeps one file per session, in a folder named after the working directory."""
    root = Path(projects_dir) if projects_dir else Path.home() / ".claude" / "projects"
    return next(root.glob(f"*/{session_id}.jsonl"), None)


def summarize(session_id, path):
    tokens, actions = {}, dict.fromkeys(ACTIONS, 0)
    started = ended = title = None
    with open(path, encoding="utf-8") as file:
        for line in file:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # The session may have been cut off in mid-line.
            started = started or entry.get("timestamp")
            ended = entry.get("timestamp") or ended
            if entry.get("type") == "ai-title":
                title = entry.get("aiTitle")
            if entry.get("type") != "assistant":
                continue
            message = entry["message"]
            tokens[message["id"]] = message.get("usage", {})  # One message spans several lines, each with its total.
            for block in message.get("content", []):
                if block.get("type") == "tool_use" and block.get("name") == "Bash":
                    command = block["input"].get("command", "")
                    for action, modules in ACTIONS.items():
                        actions[action] += sum(command.count(module) for module in modules)
    return {"id": session_id, "started": started, "ended": ended, "title": title, "actions": actions,
            "tokens": {kind: sum(usage.get(FIELDS[kind], 0) for usage in tokens.values()) for kind in TOKEN_KINDS}}


def save(store, summary):
    if not isinstance(summary, dict) or not SESSION_ID.fullmatch(str(summary.get("id"))):
        raise SessionError("a summary needs a session id")
    store.write_didban_session(summary)


def recent(store):
    return store.didban_sessions()[-KEPT:][::-1]


def main():
    parser = argparse.ArgumentParser(description="Keep a summary of each Didban session.")
    parser.add_argument("command", choices=("save",))
    parser.parse_args()
    try:
        save(StateStore(), json.loads(sys.stdin.read()))
    except (SessionError, json.JSONDecodeError) as e:
        print(f"sessions: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
