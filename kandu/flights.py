"""Sessions the user opens from the dashboard: Didban, and a free flight on a task (DESIGN §13).

Each runs on the brain in its own tmux, started by systemd, so restarting the dashboard
leaves it running. Remote Control is on, and its link is read from the session's screen.
A session that has not been spoken to costs nothing. When one ends, its transcript is
summed up here, by code, and counted against no project's limits.
"""
import hashlib
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import didban, queen, sessions
from .config import DidbanBrain, DidbanConfig
from .runner import DEFAULT_LOGS_DIR, repo_path

ROLE_FILE = didban.REPO_DIR / "prompts" / "flight.md"
LANGUAGES = {"en": "English", "fa": "Persian"}
DIDBAN = "didban"
URL = re.compile(r"https://claude\.ai/code/session_\w+")
TRUST = "Yes, I trust this folder"  # Claude asks this in a folder it has not seen, and "no" is the default.
WAIT_SECONDS = 120
NO_LINK = "the session's link did not come in time"
COMMAND_TIMEOUT_SECONDS = 15
START_TIMEOUT_SECONDS = 30


def task_key(task):
    """A short name for the tmux socket, whose path has a length limit."""
    return "t-" + hashlib.sha256(task.encode()).hexdigest()[:12]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _in_thread(work):
    threading.Thread(target=work, daemon=True).start()


class Flights:
    def __init__(self, config, store, logs_dir=DEFAULT_LOGS_DIR, run=subprocess.run, clock=time.time,
                 spawn=_in_thread, projects_dir=None):
        self.config = config
        self.store = store
        self.logs_dir = Path(logs_dir)
        self.run = run
        self.clock = clock
        self.spawn = spawn
        self.projects_dir = projects_dir
        self._opening = set()
        self._lock = threading.Lock()

    def _tmux(self, key, *args):
        return self.run(["tmux", "-S", str(self.store.flight_socket(key)), *args], capture_output=True, text=True,
                        timeout=COMMAND_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)

    def alive(self, key):
        return self.store.flight_socket(key).exists() and self._tmux(key, "has-session", "-t", "main").returncode == 0

    def open_didban(self, language):
        """A session already open stays the one: the page shows its link."""
        self._open(DIDBAN, {"kind": "didban", "task": None, "project": None},
                   lambda session: self._didban(session, language))

    def open_task(self, project, repo, number, language):
        task = f"{repo}#{number}"
        self._open(task_key(task), {"kind": "task", "task": task, "project": project.name},
                   lambda session: self._task(session, project, repo, number, language))

    def _open(self, key, fields, prepare):
        with self._lock:
            if key in self._opening or self.alive(key):
                return
            self._opening.add(key)
            earlier = self.store.flight(key)
            if earlier is not None:
                self._land(earlier)
        self.spawn(lambda: self._launch(key, fields, prepare))

    def _launch(self, key, fields, prepare):
        session = str(uuid.uuid4())
        record = fields | {"key": key, "session": session, "started": now_iso(), "epoch": self.clock(), "url": "",
                           "error": ""}
        self.store.write_flight(record)
        try:
            args, prompt, cwd = prepare(session)
            path = self.store.write_flight_prompt(key, prompt)
            line = f'{shlex.join(args)} --append-system-prompt "$(cat {shlex.quote(str(path))})"'
            tmux = ["tmux", "-S", str(self.store.flight_socket(key)), "new-session", "-d", "-s", "main",
                    "-x", "200", "-y", "50", "-c", str(cwd), line]
            # Type=forking: tmux's client exits once its server is up, and the server keeps the unit alive.
            started = self.run(["systemd-run", "--user", "--quiet", "--collect", "--property=Type=forking",
                                f"--unit=kandu-flight-{key}-{int(self.clock())}",
                                f"--setenv=PATH={os.environ.get('PATH', '')}", *tmux],
                               capture_output=True, text=True, timeout=START_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
            if started.returncode != 0 or not self.alive(key):
                record["error"] = (started.stderr or "the session did not start").strip()[:300]
        except (OSError, subprocess.SubprocessError) as e:
            record["error"] = str(e)[:300]
        self.store.write_flight(record)
        self._opening.discard(key)

    def _didban(self, session, language):
        brain = DidbanBrain(reach="local", install_dir=str(didban.REPO_DIR))
        state, message = didban.check_brain(brain)
        prompt = didban.system_prompt(DidbanConfig(brain=brain), didban.STATUS[state].format(message),
                                      **didban.waiting(brain, state), language=LANGUAGES[language])
        args = ["claude", "--session-id", session, "--name", didban.SESSION_NAME,
                "--remote-control", didban.SESSION_NAME, "--disallowedTools", didban.BLOCKED_TOOLS]
        return args, prompt, didban.REPO_DIR

    def _task(self, session, project, repo, number, language):
        task = f"{repo}#{number}"
        name = f"Kandu {repo.split('/')[1]}#{number}"
        args = ["claude", "--dangerously-skip-permissions", "--session-id", session, "--name", name,
                "--remote-control", name]
        machine = self.config.machine(project.machine)
        local = machine.reach == "local"
        copy = repo_path(project, repo)
        worktree = self._worktree(copy, number) if local else None
        folder = worktree or (Path(copy).expanduser() if local else didban.REPO_DIR)
        cwd = folder if Path(folder).is_dir() else didban.REPO_DIR
        return args, self._flight_prompt(task, project, machine, copy, worktree, language), cwd

    def _worktree(self, copy, number):
        root = Path(f"{copy.rstrip('/')}.kandu-worktrees").expanduser()
        found = [p for p in sorted(root.glob(f"{number}-*")) if not p.name.endswith("-review")]
        return found[0] if found else None

    def _flight_prompt(self, task, project, machine, copy, worktree, language):
        """Facts and paths only: the issue's text stays out, as anyone may have written it."""
        repo, number = task.split("#")
        runs = [e["run_id"] for e in queen.all_events(self.store) if e["type"] == "run_finished" and e.get("task") == task]
        dashboard = self.config.settings.dashboard
        lines = [ROLE_FILE.read_text(encoding="utf-8"), "## This task",
                 f"- Task: `{task}`. Read it with `gh issue view {number} -R {repo} --comments`. A branch of Kandu's for it starts with `kandu/{number}-`.",
                 f"- Project: `{project.name}`, on machine `{machine.name}`, at `{copy}`.",
                 f"- Worktree: `{worktree}`." if worktree else "- Worktree: none on this machine.",
                 f"- Kandu is installed at `{didban.REPO_DIR}`. The project's memory is `local/memory/{project.name}.md` there.",
                 f"- Its run logs, in `{self.logs_dir}`: {', '.join(f'`{r}.jsonl`' for r in runs) or 'none'}.",
                 f"- Kandu's API: `http://{dashboard.bind}:{dashboard.port}/api/v1`.",
                 f"- Speak {LANGUAGES[language]} with the user."]
        if machine.reach != "local":
            lines.insert(4, f"- Reach that machine with: `{machine.reach}`.")
        return "\n".join(lines)

    def _look(self, record):
        """Returns what the page shows, or None once the session has ended."""
        key = record["key"]
        if key in self._opening:
            return record | {"status": "opening"}
        if record["error"]:
            return record | {"status": "failed"}
        if not self.alive(key):
            self._land(record)
            return None
        if record["url"]:
            return record | {"status": "open"}
        screen = self._tmux(key, "capture-pane", "-p", "-J", "-t", "main").stdout
        found = URL.search(screen)
        if TRUST in screen:
            self._tmux(key, "send-keys", "-t", "main", "Down", "Enter")
        elif found:
            record["url"] = found.group(0)
            self.store.write_flight(record)
            return record | {"status": "open"}
        elif self.clock() - record["epoch"] > WAIT_SECONDS:
            self._tmux(key, "kill-server")
            record["error"] = NO_LINK
            self.store.write_flight(record)
            return record | {"status": "failed"}
        return record | {"status": "opening"}

    def list(self):
        with self._lock:
            looked = [self._look(record) for record in self.store.flights()]
        return {"open": [record for record in looked if record], "flown": self.store.flown()[-sessions.KEPT:][::-1]}

    def close(self, key):
        """Returns False if there is no such session."""
        with self._lock:
            record = self.store.flight(key)
            if record is None or key in self._opening:
                return False
            if self.store.flight_socket(key).exists():
                self._tmux(key, "kill-server")
            self._land(record)
        return True

    def _land(self, record):
        """Keeps what the session spent. One nobody spoke to spent nothing, and leaves no row."""
        path = sessions.transcript(record["session"], self.projects_dir)
        try:
            summary = sessions.summarize(record["session"], path) if path else None
        except (OSError, KeyError, AttributeError, TypeError):
            summary = None
        if summary and any(summary["tokens"].values()):
            if record["kind"] == "didban":
                self.store.write_didban_session(summary)
            else:
                self.store.write_flown(summary | {"task": record["task"], "project": record["project"]})
        self.store.forget_flight(record["key"])
