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
from .config import AGENT_CLIS, CLAUDE, CODEX
from .didban import remote_path
from .state import usage_from_result
from .usage import CODEX_LOGGED_OUT, CODEX_NO_ANSWER, CODEX_NOT_INSTALLED, TIMEOUT_SECONDS as CODEX_LOGIN_TIMEOUT

DEFAULT_LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
GIT_TIMEOUT_SECONDS = 300
PROBE_TIMEOUT_SECONDS = 20
SSH_FAILED = 255  # ssh's own exit code: it could not connect, or the connection dropped
POLL_SECONDS = 2
STOP_GRACE_SECONDS = 10
# A run in a scope: if the machine has a user systemd, the command runs in its own scope, and when it ends
# Kandu stops that scope, which ends every process the run left behind (a dev server, a test browser).
SCOPE_PROBE = "command -v systemd-run >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1"
COMMAND_TIMEOUT_SECONDS = 30 * 60
SECRET_PREFIX = "KANDU_"
SETUP_MARK = "kandu-setup"  # in a checkout's git folder: what its last setup was for (`Runner.run_test`)
# What `Runner.clean_task` says of each folder and branch it finds: removed, or why it stays.
REMOVED = "removed"
KEPT_UNCOMMITTED = "it has changes that are not committed"
KEPT_UNPUSHED = "it has commits that are neither in the merged PR nor on GitHub"
KEPT_NOT_WORKTREE = "it is not a worktree of this copy"
KEPT_CHECKED_OUT = "it is checked out in a folder that stays"


class RunnerError(Exception):
    pass


class Unreachable(RunnerError):
    """The machine did not answer over SSH, or the connection dropped: nothing was learnt about the work itself."""


class LocalWork(RunnerError):
    """A task's folder holds commits that are not on GitHub, so Kandu will not clean it."""


# What a git operation left unfinished leaves in the worktree's git folder, and the command that cancels it.
UNFINISHED = (("MERGE_HEAD", "merge"), ("rebase-merge", "rebase"), ("rebase-apply", "rebase"),
              ("CHERRY_PICK_HEAD", "cherry-pick"))
# A conflict marker git writes at the start of a line, which must never reach a review.
CONFLICT_MARKER = "^(<<<<<<<|>>>>>>>)( |$)"


def child_env(run_id=None):
    """What an agent or a project's command may see: a worker's code runs here, so none of Kandu's own secrets.

    Its `gh` is the meter, so each call is counted under this process and the run.
    """
    return ghmeter.env(run=run_id, base={name: value for name, value in os.environ.items()
                                        if not name.startswith(SECRET_PREFIX)})


# The program that runs an agent (GLOSSARY "Agent CLI"), from config: a caller that names none gets Claude Code.
# Rules that let a run change files: with one, a Codex run may write in its checkout.
EDIT_TOOLS = ("Edit", "Write", "NotebookEdit", "MultiEdit")


@dataclass
class AgentResult:
    text: str = ""
    models: dict = field(default_factory=dict)
    duration_ms: int = 0
    is_error: bool = True
    stopped: bool = False
    session_id: str | None = None  # A broken reply is asked for again in this session.
    cli: str = CLAUDE  # ... and with this agent CLI.


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
            if self.ssh_target is not None:  # a connection that hangs without closing is a drop too
                raise Unreachable(f"{command[0]} timed out over SSH on {self.name}") from e
            raise RunnerError(f"{command[0]} timed out on {self.name}") from e

    def _answered(self, result):
        """`result`, unless ssh itself failed. Git and the shell's tools never exit with 255 themselves."""
        if self.ssh_target is not None and result.returncode == SSH_FAILED:
            raise Unreachable(f"{self.name} did not answer over SSH")
        return result

    def check(self, command, cwd=None):
        result = self._answered(self.run(command, cwd))
        if result.returncode != 0:
            raise RunnerError(f"{' '.join(command[:3])} failed on {self.name}: {result.stderr.strip()}")
        return result.stdout

    def succeeds(self, command, cwd=None):
        return self._answered(self.run(command, cwd)).returncode == 0

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


def _event(line):
    """One line of an agent CLI's output as a JSON object, or None: a CLI also writes plain lines of its own."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def _claude_command(allowed, denied, model, schema, resume):
    # With no allow list the run may do everything; the deny list still holds.
    mode = ["--permission-mode", "bypassPermissions"] if allowed is None else \
        ["--permission-mode", "dontAsk", "--allowedTools", *allowed]
    return ["claude", "-p", "--output-format", "stream-json", "--verbose", *mode,
            # A repo's own settings must not change what this run may do.
            "--setting-sources", "user",
            *(["--model", model] if model else []),
            *(["--resume", resume] if resume else []),
            *(["--json-schema", json.dumps(schema)] if schema else []),
            *(["--disallowedTools", *denied] if denied else [])]


class _ClaudeReply:
    """Reads `claude`'s `stream-json` output: its last `result` event is the run's."""

    def __init__(self):
        self.result = None

    def read(self, line):
        event = _event(line)
        if event is not None and event.get("type") == "result":
            self.result = event

    def finish(self, returncode, stopped, duration_ms):
        result = self.result
        if result is None:
            return AgentResult(stopped=stopped)
        usage = usage_from_result(result)
        structured = result.get("structured_output")
        text = json.dumps(structured, ensure_ascii=False) if structured is not None else result.get("result") or ""
        return AgentResult(text=text, models=usage["models"], duration_ms=usage["duration_ms"],
                           is_error=usage["is_error"] or returncode != 0, stopped=stopped,
                           session_id=result.get("session_id"))


# A run that may change no file but writes temp files, such as a reviewer's tests: it writes its checkout and the
# temp folders alone, with the network as read-only has it, off, whatever Codex's own config says. Code checks after
# the run that the checkout is unchanged (`Runner.checkout_changed`).
TEMP_FILES_LIMITS = ["--sandbox", "workspace-write", "-c", "sandbox_workspace_write.network_access=false",
                     "-c", "sandbox_workspace_write.writable_roots=[]",
                     "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=false",
                     "-c", "sandbox_workspace_write.exclude_slash_tmp=false"]


def _codex_limits(host, cwd, allowed, temp_files=False):
    """Codex's sandbox for a run's allow list. Codex cannot deny single rules: code checks what a run changed."""
    if allowed is None:
        return ["--dangerously-bypass-approvals-and-sandbox"]
    if not any(rule.split("(")[0] in EDIT_TOOLS for rule in allowed):
        return list(TEMP_FILES_LIMITS) if temp_files else ["--sandbox", "read-only"]
    limits = ["--sandbox", "workspace-write", "-c", "sandbox_workspace_write.network_access=true"]
    # A worktree's commits go to its copy's git folder, outside the checkout.
    found = host.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd)
    return limits + (["--add-dir", found.stdout.strip()] if found.returncode == 0 and found.stdout.strip() else [])


# A Codex run's commands, as Claude's `Bash(...)` rules name them: the program, then what its arguments start with.
BASH_RULE = re.compile(r"Bash\(([A-Za-z0-9._+-]+)(?: (.*))?\)")
PROGRAM_NAME = re.compile(r"[A-Za-z0-9._+-]+")
# Stands in for one program on a Codex run's PATH: refuses what the run's deny list names, runs the rest as it is.
# It comes after the meter, which counts a `gh` call first, and passes it by when it looks for the real program.
HELD_PROGRAM = """#!/bin/sh
case "$*" in
  {patterns}) echo "kandu: denied in this run: {program} $*" >&2; exit 1 ;;
esac
here=$(dirname "$0")
IFS=:
set -f
for dir in $PATH; do
  [ -n "$dir" ] && [ "$dir" != "$here" ] && [ "$dir" != {meter} ] && [ -x "$dir/{program}" ] && exec "$dir/{program}" "$@"
done
echo "{program}: not found" >&2
exit 127
"""
# Codex's answer for this machine: 127 when there is no `codex` on its PATH, else `codex login status`'s own.
CODEX_LOGIN = 'command -v codex >/dev/null 2>&1 || exit 127; exec codex login status >/dev/null 2>&1'


def _case_pattern(rest):
    """A shell `case` pattern for a rule's arguments: `*` stays a wildcard, the rest is literal. A rule that ends in
    ` *` or `:*` holds the words before it alone too, as Claude Code does."""
    if rest.endswith(":*"):
        rest = rest[:-2] + " *"
    glob = lambda text: "*".join(shlex.quote(piece) for piece in text.split("*"))
    return f"{glob(rest[:-2])}|{glob(rest)}" if rest.endswith(" *") else glob(rest)


def held_programs(denied):
    """The stand-ins for a Codex run's deny list: {program: script}. Codex cannot deny a single command, so each
    program a `Bash(...)` rule names is found first on the run's PATH, and refuses what the rules name."""
    patterns = defaultdict(list)
    for rule in denied or []:
        found = BASH_RULE.fullmatch(rule.strip())
        if found and PROGRAM_NAME.fullmatch(found.group(1)):
            patterns[found.group(1)].append(_case_pattern(found.group(2) or ""))
    return {program: HELD_PROGRAM.format(program=program, patterns="|".join(rules),
                                         meter=shlex.quote(str(ghmeter.METER_DIR)))
            for program, rules in patterns.items()}


def _put_held(host, programs):
    """Writes the stand-ins to a new folder on the host's machine and returns its path."""
    script = ('d=$(mktemp -d "${TMPDIR:-/tmp}/kandu-held.XXXXXX") || exit 1; '
              'while [ $# -gt 0 ]; do printf %s "$2" > "$d/$1" && chmod 755 "$d/$1" || exit 1; shift 2; done; '
              'printf %s "$d"')
    return host.check(["sh", "-c", script, "sh", *[part for item in programs.items() for part in item]])


def _held(folder, command):
    """`command` with `folder` on its PATH right after the meter, and so on the PATH of every command it starts."""
    return ["sh", "-c", 'PATH="$1:$2:$PATH"; export PATH; shift 2; exec "$@"', "sh", str(ghmeter.METER_DIR), folder,
            *command]


def _codex_command(limits, model, schema_file, resume, held=False):
    """With `held`, Codex runs its commands in plain shells, never login ones: a login profile may put another
    folder ahead of the held programs on PATH, and so run the real program."""
    return ["codex", "exec", "--json", "--skip-git-repo-check",
            # Codex gets Kandu's instructions alone, never the repo's own agent files.
            "-c", "project_doc_max_bytes=0", *limits,
            *(["-c", "allow_login_shell=false"] if held else []),
            *(["--model", model] if model else []),
            *(["--output-schema", schema_file] if schema_file else []),
            *(["resume", resume] if resume else []),
            "-"]  # the prompt, on stdin


def strict_schema(schema):
    """`schema` as OpenAI's structured outputs hold a reply to it: every object names all its keys as required and
    allows no other, so a key that was optional may be null instead (`without_unset` takes those nulls out)."""
    if not isinstance(schema, dict):
        return schema
    strict = dict(schema)
    if isinstance(schema.get("items"), dict):
        strict["items"] = strict_schema(schema["items"])
    if isinstance(schema.get("properties"), dict):
        required = set(schema.get("required", []))
        strict["properties"] = {name: strict_schema(rule) if name in required else
                                {"anyOf": [strict_schema(rule), {"type": "null"}]}
                                for name, rule in schema["properties"].items()}
        strict["required"] = list(schema["properties"])
        strict["additionalProperties"] = False
    return strict


def without_unset(value, schema):
    """A reply to `strict_schema(schema)` as a reply to `schema`: an optional key that is null is left out."""
    if not isinstance(schema, dict):
        return value
    if isinstance(value, dict) and isinstance(schema.get("properties"), dict):
        rules, required = schema["properties"], set(schema.get("required", []))
        return {key: without_unset(item, rules.get(key)) for key, item in value.items()
                if not (item is None and key in rules and key not in required)}
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [without_unset(item, schema["items"]) for item in value]
    return value


class _CodexReply:
    """Reads `codex exec --json` output: its thread is the session, its last message the reply."""

    def __init__(self, model, schema):
        self.model = model or CODEX  # Codex does not say which model its default is.
        self.schema = schema
        self.session_id = None
        self.text = ""
        self.tokens = None
        self.failed = False

    def read(self, line):
        event = _event(line)
        kind = event and event.get("type")
        item = event.get("item") if kind == "item.completed" else None
        if kind == "thread.started":
            self.session_id = event.get("thread_id")
        elif isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            self.text = item["text"]
        elif kind == "turn.completed":
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            cached = usage.get("cached_input_tokens", 0)
            tokens = self.tokens or {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
            # Codex's input counts its cached input too; Kandu counts that as cache reads alone.
            tokens["input"] += max(usage.get("input_tokens", 0) - cached, 0)
            tokens["output"] += usage.get("output_tokens", 0)
            tokens["cache_read"] += cached
            tokens["cache_creation"] += usage.get("cache_write_input_tokens", 0)
            self.tokens = tokens
        elif kind == "turn.failed":
            self.failed = True

    def finish(self, returncode, stopped, duration_ms):
        text = self.text
        if self.schema is not None:
            try:
                text = json.dumps(without_unset(json.loads(text), self.schema), ensure_ascii=False)
            except json.JSONDecodeError:
                pass  # a reply that cannot be read stays as it is, to be asked for again
        return AgentResult(text=text, models={self.model: self.tokens} if self.tokens else {},
                           duration_ms=duration_ms, stopped=stopped, session_id=self.session_id, cli=CODEX,
                           is_error=self.failed or self.tokens is None or returncode != 0)


def _put_schema(host, schema):
    """Writes `schema` to a new file on the host's machine and returns its path: Codex reads a schema from a file."""
    script = 'f=$(mktemp "${TMPDIR:-/tmp}/kandu-schema.XXXXXX") && printf %s "$1" > "$f" && printf %s "$f"'
    return host.check(["sh", "-c", script, "sh", json.dumps(schema)])


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

    def prepare_worktree(self, project, repo, branch, base, cleaned=None):
        """Returns the worker's worktree on `branch`, reusing it if an earlier run left one.

        A merge, rebase or cherry-pick an earlier run left unfinished there is cancelled first, and `cleaned` is
        called with what was cancelled. If the folder holds commits that are not on GitHub, it is left as it is
        and `LocalWork` is raised.
        """
        host = self.hosts[project.machine]
        with self._repo_locks[(host.name, repo)]:
            path = self._ensure_copy(host, project, repo)
            directory = _worktree_dir(path, branch.removeprefix("kandu/"))
            host.check(["git", "fetch", "origin"], cwd=path)
            pushed = host.succeeds(["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], cwd=path)
            if host.succeeds(["test", "-d", directory]):
                self._cancel_unfinished(host, directory, branch, cleaned)
                if pushed:
                    host.check(["git", "merge", "--ff-only", f"origin/{branch}"], cwd=directory)
                return directory
            host.check(["git", "worktree", "prune"], cwd=path)
            start = f"origin/{branch}" if pushed else f"origin/{base}"
            host.check(["git", "worktree", "add", "-B", branch, directory, start], cwd=path)
        return directory

    @staticmethod
    def _cancel_unfinished(host, directory, branch, cleaned):
        operation = next((name for marker, name in UNFINISHED if host.succeeds(
            ["test", "-e", host.check(["git", "rev-parse", "--git-path", marker], cwd=directory).strip()],
            cwd=directory)), None)
        if operation is None:
            return
        local = host.check(["git", "rev-list", "--count", "HEAD", f"refs/heads/{branch}", "--not", "--remotes=origin"],
                           cwd=directory).strip()
        if local != "0":
            raise LocalWork(f"The task's folder on {host.name} has a {operation} left unfinished and {local} commit(s) "
                            f"that are not on GitHub. Kandu left the folder as it is, so nothing is lost: finish or "
                            f"cancel the {operation} there, push the commits, then send the task back.")
        host.check(["git", operation, "--abort"], cwd=directory)
        if cleaned is not None:
            cleaned(f"cancelled a {operation} an earlier run left unfinished in the task's folder")

    def merge_base(self, project, repo, directory, branch, base, message, kept=None):
        """Merges `origin/<base>` into the pushed `branch`, in Kandu's code, so no worker runs `git merge`.

        Work an earlier run left uncommitted or unpushed is dropped first: it was never reviewed, and it would
        stop the merge. Commits it made but did not push, such as a run cut off by a dropped connection, are kept
        first under `refs/kandu/kept/`, and `kept` is called with where. Returns the commit of `base` merged and
        the files in conflict. With none the merge is committed with `message`; with some it is left open, for a
        worker to resolve and commit."""
        host = self.hosts[project.machine]
        with self._repo_locks[(host.name, repo)]:
            host.check(["git", "fetch", "origin"], cwd=directory)
            local = host.check(["git", "rev-list", "--count", "HEAD", "--not", "--remotes=origin"], cwd=directory).strip()
            if local != "0":
                head = host.check(["git", "rev-parse", "HEAD"], cwd=directory).strip()
                ref = f"refs/kandu/kept/{branch.removeprefix('kandu/')}/{head[:12]}"
                host.check(["git", "update-ref", ref, head], cwd=directory)
                if kept is not None:
                    kept(f"{local} commit(s) a run made but did not push are kept in {ref} on {host.name}")
            host.check(["git", "reset", "--hard", f"origin/{branch}"], cwd=directory)
            host.check(["git", "clean", "-fd"], cwd=directory)
            sha = host.check(["git", "rev-parse", f"origin/{base}"], cwd=directory).strip()
            if host.succeeds(["git", "merge", "--no-ff", "-m", message, sha], cwd=directory):
                return sha, []
            conflicts = host.check(["git", "diff", "--name-only", "--diff-filter=U"], cwd=directory).splitlines()
            if not conflicts:
                host.run(["git", "merge", "--abort"], cwd=directory)
                raise RunnerError(f"merging origin/{base} failed on {host.name}, with no conflict to resolve")
            return sha, conflicts

    def push_branch(self, project, directory, branch):
        """Pushes a merge Kandu made itself. Never forced: a branch that moved meanwhile refuses it."""
        self.hosts[project.machine].check(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], cwd=directory)

    def branch_pushed(self, project, repo, branch):
        """Whether `branch` is on GitHub, such as a worker's that pushed but whose last message was lost."""
        host = self.hosts[project.machine]
        with self._repo_locks[(host.name, repo)]:
            path = self._ensure_copy(host, project, repo)
            host.check(["git", "fetch", "origin"], cwd=path)
            return host.succeeds(["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
                                 cwd=path)

    def has_commit(self, project, repo, directory, branch, sha):
        """Whether the pushed `branch` contains `sha`, such as the base commit an update round merges."""
        host = self.hosts[project.machine]
        with self._repo_locks[(host.name, repo)]:
            host.check(["git", "fetch", "origin"], cwd=directory)
        return host.succeeds(["git", "merge-base", "--is-ancestor", sha, f"origin/{branch}"], cwd=directory)

    def conflict_markers(self, project, repo, directory, branch, paths):
        """Returns which of `paths` still hold a conflict marker on the pushed branch."""
        host = self.hosts[project.machine]
        with self._repo_locks[(host.name, repo)]:
            host.check(["git", "fetch", "origin"], cwd=directory)
        ref = f"origin/{branch}"
        found = host.run(["git", "grep", "-l", "-E", CONFLICT_MARKER, ref, "--", *paths], cwd=directory)
        if found.returncode not in (0, 1):
            raise RunnerError(f"git grep failed on {host.name}: {found.stderr.strip()}")
        return [line.removeprefix(f"{ref}:") for line in found.stdout.splitlines() if line]

    def changed_paths(self, project, repo, directory, base, head_sha):
        """The paths `head_sha` changed since it left `origin/<base>`: what its PR changes. A file moved counts at
        both its old and its new path."""
        host = self.hosts[project.machine]
        with self._repo_locks[(host.name, repo)]:
            host.check(["git", "fetch", "origin"], cwd=directory)
        return host.check(["git", "diff", "--name-only", "--no-renames", f"origin/{base}...{head_sha}"],
                          cwd=directory).splitlines()

    def codex_login(self, machine):
        """None when Codex is installed and logged in on `machine`; otherwise why not. `Unreachable` when the
        machine does not answer: that says nothing of Codex."""
        host = self.hosts[machine]
        try:
            found = host._answered(host.run(["sh", "-c", CODEX_LOGIN], timeout=CODEX_LOGIN_TIMEOUT))
        except Unreachable:
            raise
        except RunnerError:
            return CODEX_NO_ANSWER
        if found.returncode == 0:
            return None
        return CODEX_NOT_INSTALLED if found.returncode == 127 else CODEX_LOGGED_OUT

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

    def checkout_changed(self, project, cwd, head_sha):
        """Why the checkout in `cwd` is no longer `head_sha` as it was handed, or None: its head moved, or a tracked
        file changed, staged or not. New files, untracked or ignored, such as a test cache, do not count."""
        host = self.hosts[project.machine]
        at = host.check(["git", "rev-parse", "--verify", "HEAD"], cwd=cwd).strip()
        if at != head_sha:
            return f"the checkout moved from {head_sha} to {at}"
        status = host.check(["git", "status", "--porcelain", "--untracked-files=no"], cwd=cwd)
        paths = [line[3:] for line in status.splitlines() if line.strip()]
        return "tracked files changed: " + ", ".join(paths) if paths else None

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
                code = subprocess.run(host.argv(argv, cwd), stdout=log, stderr=subprocess.STDOUT,
                                      timeout=COMMAND_TIMEOUT_SECONDS, env=child_env()).returncode
            except subprocess.TimeoutExpired as e:
                raise RunnerError(f"a project command timed out on {host.name}") from e
            finally:
                if unit:
                    host.stop_scope(unit)
        # 255 is ssh's own code, but a project's command may exit with it too: a machine that still
        # answers says the command did, so only a machine that is gone makes it a drop
        if host.ssh_target is not None and code == SSH_FAILED and not self.reachable(project.machine):
            raise Unreachable(f"{host.name} dropped the connection during a project command")
        return code

    def run_test(self, project, cwd, log_name):
        """Runs the project's `test` in `cwd` and returns its exit code, after its `setup` when the checkout needs it.

        The setup runs in a checkout that never had it, and again when its `inputs` changed since, or, with no
        `inputs`, when the head did. A mark in the checkout's own git folder says what it last set up, so the
        project's files never show it. A setup that fails is the test's exit code, and its output is in the same log.
        """
        if project.setup is not None:
            code = self._set_up(project, cwd, log_name)
            if code != 0:
                return code
        return self.run_command(project, cwd, project.test, log_name)

    def _set_up(self, project, cwd, log_name):
        host = self.hosts[project.machine]
        mark = posixpath.join(host.check(["git", "rev-parse", "--absolute-git-dir"], cwd=cwd).strip(), SETUP_MARK)
        wanted = [project.setup.command]
        for path in project.setup.inputs or ["HEAD"]:
            ref = path if path == "HEAD" else f"HEAD:{path}"
            found = host.run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=cwd)
            wanted.append(f"{path} {found.stdout.strip() if found.returncode == 0 else '-'}")
        wanted = "\n".join(wanted) + "\n"
        done = host.run(["cat", mark], cwd=cwd)
        if done.returncode == 0 and done.stdout == wanted:
            return 0
        host.run(["rm", "-f", mark], cwd=cwd)  # a setup cut off halfway is not a setup done
        code = self.run_command(project, cwd, project.setup.command, log_name)
        if code == 0:
            host.check(["sh", "-c", 'printf %s "$1" > "$2"', "sh", wanted, mark], cwd=cwd)
        return code

    def run_agent(self, machine, cwd, prompt, allowed, denied, run_id, should_stop, model=None, tokens=None,
                  schema=None, resume=None, cli=CLAUDE, temp_files=False):
        """Runs one agent process to its end, with the agent CLI `cli`; `should_stop` is polled while it runs.

        The run is in its own scope (`Host.scoped`): when it ends, or is
        stopped, every process it started on the machine ends with it, over
        SSH too. With no `model`,
        the CLI picks its own default. `tokens` is (file, names): those
        values alone reach this process, in its environment. If one is
        missing the run ends with an error. With a `schema`, the CLI holds the
        reply to that JSON Schema, and the text is the object it returns.
        With `resume`, the prompt goes to that earlier session in `cwd`.
        With `temp_files`, a Codex run that may change no file still writes
        temp files (`TEMP_FILES_LIMITS`); a Claude run is as without it.
        """
        if cli not in AGENT_CLIS:
            raise RunnerError(f"not an agent CLI: {cli!r}")
        host = self.hosts[machine]
        schema_file = held = None
        try:
            if cli == CLAUDE:
                command, reply = _claude_command(allowed, denied, model, schema, resume), _ClaudeReply()
            else:
                schema_file = _put_schema(host, strict_schema(schema)) if schema else None
                programs = held_programs(denied)
                command = _codex_command(_codex_limits(host, cwd, allowed, temp_files), model, schema_file, resume,
                                         held=bool(programs))
                if programs:
                    held = _put_held(host, programs)
                    command = _held(held, command)
                reply = _CodexReply(model, schema)
            return self._run(host, cwd, command, prompt, run_id, should_stop, tokens, reply)
        finally:
            if schema_file:
                host.run(["rm", "-f", schema_file])
            if held:
                host.run(["rm", "-rf", held])

    def _run(self, host, cwd, command, prompt, run_id, should_stop, tokens, reply):
        if tokens:
            path, names = tokens
            script = tokens_script(host.absolute(path), names)
            command = ["sh", "-c", f'{script}; exec "$@"', "sh", *command]
        self.logs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.logs_dir / f"{run_id}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        unit = scope_name(run_id)
        started = time.monotonic()
        process = subprocess.Popen(host.argv(Host.scoped(command, unit), cwd), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=child_env(run_id))
        stopped = threading.Event()
        threading.Thread(target=self._feed, args=(process, prompt), daemon=True).start()
        threading.Thread(target=self._watch, args=(process, should_stop, stopped), daemon=True).start()
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as log:
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    reply.read(line)
            process.wait()
        finally:
            # Ends what the run left behind, and over SSH a stopped run's agent at once.
            host.stop_scope(unit)
        return reply.finish(process.returncode, stopped.is_set(), round((time.monotonic() - started) * 1000))

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
