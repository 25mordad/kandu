"""Where agents run: the swappable layer of DESIGN §9.

Everything goes through a shell script on the project's machine, local or over
SSH, so both paths are the same code. To add isolation, replace this module's
`Runner`: the supervisor only uses its public methods.
"""
import json
import os
import posixpath
import re
import shlex
import subprocess
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import ghmeter
from .didban import remote_path
from .state import usage_from_result

DEFAULT_LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
GIT_TIMEOUT_SECONDS = 300
PROBE_TIMEOUT_SECONDS = 20
POLL_SECONDS = 2
STOP_GRACE_SECONDS = 10
# A run in a scope: if the machine has a user systemd, the command runs in its own scope, and when it ends
# Kandu stops that scope, which ends every process the run left behind (a dev server, a test browser).
SCOPE_PROBE = "command -v systemd-run >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1"
COMMAND_TIMEOUT_SECONDS = 30 * 60
SECRET_PREFIX = "KANDU_"
# What `Runner.clean_task` says of each folder and branch it finds: removed, or why it stays.
REMOVED = "removed"
KEPT_UNCOMMITTED = "it has changes that are not committed"
KEPT_UNPUSHED = "it has commits that are neither in the merged PR nor on GitHub"
KEPT_NOT_WORKTREE = "it is not a worktree of this copy"
KEPT_CHECKED_OUT = "it is checked out in a folder that stays"


class RunnerError(Exception):
    pass


def child_env(run_id=None):
    """What an agent or a project's command may see: a worker's code runs here, so none of Kandu's own secrets.

    Its `gh` is the meter, so each call is counted under this process and the run.
    """
    return ghmeter.env(run=run_id, base={name: value for name, value in os.environ.items()
                                        if not name.startswith(SECRET_PREFIX)})


@dataclass
class AgentResult:
    text: str = ""
    models: dict = field(default_factory=dict)
    duration_ms: int = 0
    is_error: bool = True
    stopped: bool = False
    session_id: str | None = None  # A broken reply is asked for again in this session.


class Host:
    """Runs commands on one machine."""

    def __init__(self, machine):
        self.name = machine.name
        self.ssh_target = None if machine.reach == "local" else machine.reach.split()[1]
        self._home = None

    def argv(self, command, cwd=None):
        change_dir = f"cd {remote_path(cwd)} && " if cwd else ""
        script = f'{ghmeter.path_line()}; {change_dir}exec {shlex.join(command)}'
        if self.ssh_target is None:
            return ["sh", "-c", script]
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", self.ssh_target, script]

    @staticmethod
    def scoped(command, unit):
        """`command` in its own systemd scope named `unit`; as it is where the machine has no user systemd."""
        script = (f'if {SCOPE_PROBE}; then exec systemd-run --user --scope --quiet --collect '
                  f'--property=TimeoutStopSec={STOP_GRACE_SECONDS} --unit={shlex.quote(unit)} -- "$@"; fi; exec "$@"')
        return ["sh", "-c", script, "sh", *command]

    def stop_scope(self, unit):
        """Ends whatever is left in the scope `unit`. A scope that is already gone, or no systemd, is fine."""
        try:
            self.run(["sh", "-c", f"if {SCOPE_PROBE}; then systemctl --user stop {shlex.quote(unit + '.scope')}; fi"],
                     timeout=PROBE_TIMEOUT_SECONDS)
        except RunnerError:
            pass

    def run(self, command, cwd=None, timeout=GIT_TIMEOUT_SECONDS):
        try:
            return subprocess.run(self.argv(command, cwd), capture_output=True, text=True, timeout=timeout, env=child_env())
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


def scope_name(name):
    """A unit name for one run: unique, and only characters systemd takes."""
    return f"kandu-{re.sub(r'[^A-Za-z0-9_.-]', '-', name)[:80]}-{uuid.uuid4().hex[:8]}"


def repo_path(project, repo):
    """A project with several repos keeps each at `<path>/<repo name>`."""
    if len(project.repos) == 1:
        return project.path
    return posixpath.join(project.path, repo.split("/")[1])


def tokens_script(path, names):
    """Shell that exports only `names` from the file at `path`, which is read, never run."""
    unquote = shlex.quote(r"""s/^"(.*)"$/\1/; s/^'(.*)'$/\1/""")
    lines = []
    for name in names:
        pick = shlex.quote(rf"s/^(export +)?{name} *= *//p")
        lines.append(f"{name}=$(sed -n -E {pick} {shlex.quote(path)} | tail -n 1 | tr -d '\r' | sed -E {unquote}); "
                     f'[ -n "${name}" ] || {{ echo "token {name} is missing" >&2; exit 1; }}; export {name}')
    return "; ".join(lines)


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

    def clean_task(self, project, repo, branch, folders, merged_sha):
        """Removes a merged task's folders beside the copy and its local branch. Returns (what, target, outcome)
        for each that was there: `what` is "folder" or "branch", `outcome` REMOVED or why it stays.

        Only `<copy>.kandu-worktrees/<name>` for each name in `folders`, and only the `kandu/` branch `branch`:
        the copy itself and every other folder and branch stay. A folder or branch stays while it holds work found
        neither in the merged PR (`merged_sha`) nor on GitHub; files git ignores do not count.
        """
        if not branch.startswith("kandu/") or any("/" in n or n.startswith(".") or not n for n in folders):
            raise RunnerError(f"not a task's folder or branch: {branch} {folders}")
        host = self.hosts[project.machine]
        found = []
        with self._repo_locks[(host.name, repo)]:
            path = host.absolute(repo_path(project, repo))
            if not host.succeeds(["test", "-e", ".git"], cwd=path):
                return found
            host.check(["git", "fetch", "origin"], cwd=path)
            for name in folders:
                directory = _worktree_dir(path, name)
                if host.succeeds(["test", "-d", directory]):
                    found.append(("folder", name, self._remove_folder(host, path, directory, merged_sha)))
            ref = f"refs/heads/{branch}"
            if host.succeeds(["git", "show-ref", "--verify", "--quiet", ref], cwd=path):
                found.append(("branch", branch, self._remove_branch(host, path, branch, merged_sha)))
        return found

    @staticmethod
    def _worktrees(host, path):
        """The copy's worktrees: (folder, branch ref or None) for each."""
        found, folder = [], None
        for line in host.check(["git", "worktree", "list", "--porcelain"], cwd=path).splitlines():
            if line.startswith("worktree "):
                folder = line.removeprefix("worktree ")
                found.append((folder, None))
            elif line.startswith("branch ") and folder is not None:
                found[-1] = (folder, line.removeprefix("branch "))
        return found

    @staticmethod
    def _merged_or_pushed(host, cwd, rev, merged_sha):
        """True when every commit of `rev` is in the merged PR or on one of GitHub's branches."""
        if host.succeeds(["git", "merge-base", "--is-ancestor", rev, merged_sha], cwd=cwd):
            return True
        # A tracking branch counts only while GitHub still has it: fetch leaves one whose branch is gone.
        live = {line.split("\t")[-1].removeprefix("refs/heads/")
                for line in host.check(["git", "ls-remote", "--heads", "origin"], cwd=cwd).splitlines() if line}
        holding = host.check(["git", "for-each-ref", "--format=%(refname:lstrip=3)", "--contains", rev,
                              "refs/remotes/origin"], cwd=cwd).split()
        return any(name in live for name in holding)

    def _remove_folder(self, host, path, directory, merged_sha):
        if directory not in {folder for folder, _ in self._worktrees(host, path)}:
            return KEPT_NOT_WORKTREE
        if host.check(["git", "status", "--porcelain"], cwd=directory).strip():
            return KEPT_UNCOMMITTED  # untracked files count; ignored ones do not show here
        if not self._merged_or_pushed(host, directory, "HEAD", merged_sha):
            return KEPT_UNPUSHED
        # Forced only so that ignored files go with it: everything that counts was checked above.
        host.check(["git", "worktree", "remove", "--force", directory], cwd=path)
        return REMOVED

    def _remove_branch(self, host, path, branch, merged_sha):
        if any(ref == f"refs/heads/{branch}" for _, ref in self._worktrees(host, path)):
            return KEPT_CHECKED_OUT
        if not self._merged_or_pushed(host, path, f"refs/heads/{branch}", merged_sha):
            return KEPT_UNPUSHED
        host.check(["git", "branch", "-D", branch], cwd=path)
        return REMOVED

    def run_command(self, project, cwd, command, log_name, secrets=False, scoped=True):
        """Runs a project's command in `cwd` and returns its exit code. Its output stays in a local log.

        With `scoped`, what the command leaves running is ended with it. A deploy passes False: it may start
        what must keep running.
        """
        host = self.hosts[project.machine]
        script = command
        if secrets and project.secrets_from:
            script = f"set -a; . {shlex.quote(host.absolute(project.secrets_from))}; set +a; {command}"
        unit = scope_name(log_name) if scoped else None
        argv = Host.scoped(["sh", "-c", script], unit) if unit else ["sh", "-c", script]
        self.logs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.logs_dir / f"{log_name}.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as log:
            try:
                return subprocess.run(host.argv(argv, cwd), stdout=log, stderr=subprocess.STDOUT,
                                      timeout=COMMAND_TIMEOUT_SECONDS, env=child_env()).returncode
            except subprocess.TimeoutExpired as e:
                raise RunnerError(f"a project command timed out on {host.name}") from e
            finally:
                if unit:
                    host.stop_scope(unit)

    def run_agent(self, machine, cwd, prompt, allowed, denied, run_id, should_stop, model=None, tokens=None,
                  schema=None, resume=None):
        """Runs one `claude` process to its end; `should_stop` is polled while it runs.

        The run is in its own scope (`Host.scoped`): when it ends, or is
        stopped, every process it started on the machine ends with it, over
        SSH too. With no `model`,
        `claude` picks its own default. `tokens` is (file, names): those
        values alone reach this process, in its environment. If one is
        missing the run ends with an error. With a `schema`, `claude` holds the
        reply to that JSON Schema, and the text is the object it returns.
        With `resume`, the prompt goes to that earlier session in `cwd`.
        """
        # With no allow list the run may do everything; the deny list still holds.
        mode = ["--permission-mode", "bypassPermissions"] if allowed is None else \
            ["--permission-mode", "dontAsk", "--allowedTools", *allowed]
        command = ["claude", "-p", "--output-format", "stream-json", "--verbose", *mode,
                   # A repo's own settings must not change what this run may do.
                   "--setting-sources", "user",
                   *(["--model", model] if model else []),
                   *(["--resume", resume] if resume else []),
                   *(["--json-schema", json.dumps(schema)] if schema else []),
                   *(["--disallowedTools", *denied] if denied else [])]
        if tokens:
            path, names = tokens
            script = tokens_script(self.hosts[machine].absolute(path), names)
            command = ["sh", "-c", f'{script}; exec "$@"', "sh", *command]
        self.logs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.logs_dir / f"{run_id}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        host = self.hosts[machine]
        unit = scope_name(run_id)
        process = subprocess.Popen(host.argv(Host.scoped(command, unit), cwd), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=child_env(run_id))
        stopped = threading.Event()
        threading.Thread(target=self._feed, args=(process, prompt), daemon=True).start()
        threading.Thread(target=self._watch, args=(process, should_stop, stopped), daemon=True).start()
        result = None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as log:
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    result = _last_result(line, result)
            process.wait()
        finally:
            # Ends what the run left behind, and over SSH a stopped run's `claude` at once.
            host.stop_scope(unit)
        if result is None:
            return AgentResult(stopped=stopped.is_set())
        usage = usage_from_result(result)
        structured = result.get("structured_output")
        text = json.dumps(structured, ensure_ascii=False) if structured is not None else result.get("result") or ""
        return AgentResult(text=text, models=usage["models"], duration_ms=usage["duration_ms"],
                           is_error=usage["is_error"] or process.returncode != 0, stopped=stopped.is_set(),
                           session_id=result.get("session_id"))

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
