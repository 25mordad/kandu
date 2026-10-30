"""Where agents run: the swappable layer of DESIGN §9.

Everything goes through a shell script on the project's machine, local or over
SSH, so both paths are the same code. To add isolation, replace this module's
`Runner`: the supervisor only uses its public methods.
"""
import json
import os
import posixpath
import shlex
import subprocess
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .didban import remote_path
from .state import usage_from_result

DEFAULT_LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
GIT_TIMEOUT_SECONDS = 300
PROBE_TIMEOUT_SECONDS = 20
POLL_SECONDS = 2
STOP_GRACE_SECONDS = 10
COMMAND_TIMEOUT_SECONDS = 30 * 60


class RunnerError(Exception):
    pass


@dataclass
class AgentResult:
    text: str = ""
    models: dict = field(default_factory=dict)
    duration_ms: int = 0
    is_error: bool = True
    stopped: bool = False


class Host:
    """Runs commands on one machine."""

    def __init__(self, machine):
        self.name = machine.name
        self.ssh_target = None if machine.reach == "local" else machine.reach.split()[1]
        self._home = None

    def argv(self, command, cwd=None):
        change_dir = f"cd {remote_path(cwd)} && " if cwd else ""
        script = f'export PATH="$HOME/.local/bin:$PATH"; {change_dir}exec {shlex.join(command)}'
        if self.ssh_target is None:
            return ["sh", "-c", script]
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", self.ssh_target, script]

    def run(self, command, cwd=None, timeout=GIT_TIMEOUT_SECONDS):
        try:
            return subprocess.run(self.argv(command, cwd), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise RunnerError(f"{command[0]} timed out on {self.name}") from e

    def check(self, command, cwd=None):
        result = self.run(command, cwd)
        if result.returncode != 0:
            raise RunnerError(f"{' '.join(command[:3])} failed on {self.name}: {result.stderr.strip()}")
        return result.stdout

    def succeeds(self, command, cwd=None):
        return self.run(command, cwd).returncode == 0

    def absolute(self, path):
        """Expands a leading `~` on this machine: arguments reach commands unexpanded."""
        if not path.startswith("~/"):
            return path
        if self._home is None:
            self._home = self.check(["sh", "-c", 'printf %s "$HOME"'])
        return posixpath.join(self._home, path[2:])


def repo_path(project, repo):
    """A project with several repos keeps each at `<path>/<repo name>`."""
    if len(project.repos) == 1:
        return project.path
    return posixpath.join(project.path, repo.split("/")[1])


def _worktree_dir(path, name):
    # Beside the copy, never inside it: a worktree in the user's copy would show up in their `git status`.
    return f"{path.rstrip('/')}.kandu-worktrees/{name}"


def _last_result(line, found):
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return found
    return event if isinstance(event, dict) and event.get("type") == "result" else found


class Runner:
    def __init__(self, config, logs_dir=DEFAULT_LOGS_DIR):
        self.hosts = {m.name: Host(m) for m in config.settings.machines}
        self.logs_dir = Path(logs_dir)
        self._repo_locks = defaultdict(threading.Lock)

    def reachable(self, machine):
        host = self.hosts[machine]
        try:
            return host.run(["true"], timeout=PROBE_TIMEOUT_SECONDS).returncode == 0
        except RunnerError:
            return False

    def _ensure_copy(self, host, project, repo):
        path = host.absolute(repo_path(project, repo))
        if host.succeeds(["test", "-e", ".git"], cwd=path):
            return path
        if project.run != "clone":
            raise RunnerError(f"{project.name}: no git copy at the registered path on {host.name}")
        host.check(["mkdir", "-p", posixpath.dirname(path)])
        host.check(["gh", "repo", "clone", repo, path])
        return path

    def prepare_worktree(self, project, repo, branch, base):
        """Returns the worker's worktree on `branch`, reusing it if an earlier run left one."""
        host = self.hosts[project.machine]
        with self._repo_locks[(host.name, repo)]:
            path = self._ensure_copy(host, project, repo)
            directory = _worktree_dir(path, branch.removeprefix("kandu/"))
            host.check(["git", "fetch", "origin"], cwd=path)
            pushed = host.succeeds(["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], cwd=path)
            if host.succeeds(["test", "-d", directory]):
                if pushed:
                    host.check(["git", "merge", "--ff-only", f"origin/{branch}"], cwd=directory)
                return directory
            host.check(["git", "worktree", "prune"], cwd=path)
            start = f"origin/{branch}" if pushed else f"origin/{base}"
            host.check(["git", "worktree", "add", "-B", branch, directory, start], cwd=path)
        return directory

    def prepare_review(self, project, repo, branch, head_sha):
        """Returns a detached checkout of exactly `head_sha`, apart from the worker's worktree."""
        host = self.hosts[project.machine]
        with self._repo_locks[(host.name, repo)]:
            path = self._ensure_copy(host, project, repo)
            directory = _worktree_dir(path, branch.removeprefix("kandu/") + "-review")
            host.check(["git", "fetch", "origin"], cwd=path)
            if host.succeeds(["test", "-d", directory]):
                host.check(["git", "checkout", "--detach", "--force", head_sha], cwd=directory)
            else:
                host.check(["git", "worktree", "prune"], cwd=path)
                host.check(["git", "worktree", "add", "--detach", directory, head_sha], cwd=path)
        return directory

    def run_command(self, project, cwd, command, log_name, secrets=False):
        """Runs a project's command in `cwd` and returns its exit code. Its output stays in a local log."""
        host = self.hosts[project.machine]
        script = command
        if secrets and project.secrets_from:
            script = f"set -a; . {shlex.quote(host.absolute(project.secrets_from))}; set +a; {command}"
        self.logs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.logs_dir / f"{log_name}.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as log:
            try:
                return subprocess.run(host.argv(["sh", "-c", script], cwd), stdout=log, stderr=subprocess.STDOUT,
                                      timeout=COMMAND_TIMEOUT_SECONDS).returncode
            except subprocess.TimeoutExpired as e:
                raise RunnerError(f"a project command timed out on {host.name}") from e

    def run_agent(self, machine, cwd, prompt, allowed, denied, run_id, should_stop, model=None):
        """Runs one `claude` process to its end; `should_stop` is polled while it runs.

        Over SSH, stopping ends the local `ssh`; the remote process then dies at
        its next write to the closed connection, not at once. With no `model`,
        `claude` picks its own default.
        """
        command = ["claude", "-p", "--output-format", "stream-json", "--verbose",
                   "--no-session-persistence", "--permission-mode", "dontAsk",
                   *(["--model", model] if model else []),
                   "--allowedTools", *allowed, "--disallowedTools", *denied]
        self.logs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.logs_dir / f"{run_id}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        process = subprocess.Popen(self.hosts[machine].argv(command, cwd), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        stopped = threading.Event()
        threading.Thread(target=self._feed, args=(process, prompt), daemon=True).start()
        threading.Thread(target=self._watch, args=(process, should_stop, stopped), daemon=True).start()
        result = None
        with os.fdopen(fd, "w", encoding="utf-8") as log:
            for line in process.stdout:
                log.write(line)
                log.flush()
                result = _last_result(line, result)
        process.wait()
        if result is None:
            return AgentResult(stopped=stopped.is_set())
        usage = usage_from_result(result)
        return AgentResult(text=result.get("result") or "", models=usage["models"], duration_ms=usage["duration_ms"],
                           is_error=usage["is_error"] or process.returncode != 0, stopped=stopped.is_set())

    @staticmethod
    def _feed(process, prompt):
        # In its own thread: a prompt larger than the pipe must not block reading the output.
        try:
            process.stdin.write(prompt)
            process.stdin.close()
        except (BrokenPipeError, ValueError):
            pass

    @staticmethod
    def _watch(process, should_stop, stopped):
        while process.poll() is None:
            if should_stop():
                stopped.set()
                process.terminate()
                time.sleep(STOP_GRACE_SECONDS)
                if process.poll() is None:
                    process.kill()
                return
            time.sleep(POLL_SECONDS)
