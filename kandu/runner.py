"""Where agents run: the swappable layer of DESIGN §9.

Everything goes through a shell script on the project's machine, local or over
SSH, so both paths are the same code. To add isolation, replace this module's
`Runner`: the supervisor only uses its public methods.
"""
import hashlib
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

from . import ghmeter, mainred
from .didban import remote_path
from .state import usage_from_result

DEFAULT_LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
GIT_TIMEOUT_SECONDS = 300
PROBE_TIMEOUT_SECONDS = 20
SSH_FAILED = 255  # ssh's own exit code: it could not connect, or the connection dropped
POLL_SECONDS = 2
STOP_GRACE_SECONDS = 10
# A run in a scope: if the machine has a user systemd, the command runs in its own scope, and when it ends
# Kandu stops that scope, which ends every process the run left behind (a dev server, a test browser).
SCOPE_PROBE = "command -v systemd-run >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1"
# With no user systemd, as on a Windows machine through Git Bash, a run stays under the shell that starts it, whose
# first argument is the run's unit name (`Host.scoped`). Kandu finds that shell by the name and ends it with all it
# started (`STOP_SCRIPT`). The shell waits for its command even when told to stop, so it is still there to be found.
# On Windows a dropped SSH connection ends nothing, and Git Bash's fork leaves no parent link Windows can follow, so
# the shell first puts itself in a Windows job object (`WINDOWS_JOB`) that only it holds a handle to: everything the
# run starts is in the job, and Windows ends the job's processes once that shell ends, by itself or by `taskkill`.
# C# for PowerShell's `Add-Type -MemberDefinition`, kept on one line and short: on Windows the whole SSH command must
# stay under cmd.exe's 8191 characters. `Hold(pid)` makes a job that ends its processes when its last handle closes,
# puts the process `pid` in it, and gives that process the only handle. No child may leave the job: Git Bash would.
WINDOWS_JOB = " ".join([
    '[DllImport("kernel32")] static extern IntPtr CreateJobObject(IntPtr a, string n);',
    '[DllImport("kernel32")] static extern bool SetInformationJobObject(IntPtr j, int c, byte[] i, int l);',
    '[DllImport("kernel32")] static extern IntPtr OpenProcess(int a, bool i, int p);',
    '[DllImport("kernel32")] static extern bool AssignProcessToJobObject(IntPtr j, IntPtr p);',
    '[DllImport("kernel32")] static extern IntPtr GetCurrentProcess();',
    '[DllImport("kernel32")] static extern bool DuplicateHandle(IntPtr a, IntPtr h, IntPtr b, out IntPtr c, int d,'
    ' bool e, int f);',
    'public static int Hold(int pid) {',
    'IntPtr job = CreateJobObject(IntPtr.Zero, null); if (job == IntPtr.Zero) return 1;',
    # JOBOBJECT_EXTENDED_LIMIT_INFORMATION, with LimitFlags = KILL_ON_JOB_CLOSE at offset 16
    'byte[] info = new byte[IntPtr.Size == 8 ? 144 : 112]; BitConverter.GetBytes(0x2000).CopyTo(info, 16);',
    'if (!SetInformationJobObject(job, 9, info, info.Length)) return 2;',
    'IntPtr shell = OpenProcess(0x0141, false, pid); if (shell == IntPtr.Zero) return 3;',  # terminate, quota, dup
    'if (!AssignProcessToJobObject(job, shell)) return 4;',
    'IntPtr held; return DuplicateHandle(GetCurrentProcess(), job, shell, out held, 0, false, 2) ? 0 : 5; }',
])
NO_SCOPE_RUN = (
    'if [ -r /proc/$$/winpid ] && command -v powershell.exe >/dev/null 2>&1; then '
    f'KANDU_RUN_JOB={shlex.quote(WINDOWS_JOB)} KANDU_RUN_WINPID=$(cat /proc/$$/winpid) '
    "powershell.exe -NoProfile -NonInteractive -Command "
    "'Add-Type -Name RunJob -Namespace Kandu -MemberDefinition $env:KANDU_RUN_JOB; "
    "exit [Kandu.RunJob]::Hold([int]$env:KANDU_RUN_WINPID)' </dev/null >/dev/null 2>&1; fi; "
    "trap 'exit 143' TERM HUP; \"$@\"")
# Ends every run whose unit name starts with $1, with all it started, and prints the name or pid of each one it ended.
STOP_SCRIPT = "; ".join([
    'mark=$1',
    f'if {SCOPE_PROBE}; then '
    'for unit in $(systemctl --user list-units --plain --no-legend --type=scope "$mark*" | cut -d" " -f1); '
    'do systemctl --user stop "$unit" && echo "${unit%.scope}"; done; exit 0; fi',
    "NL='\n'",
    'tree() { echo "$1"; for child in $(pgrep -P "$1" 2>/dev/null); do tree "$child"; done; }',
    'for dir in /proc/[0-9]*; do pid=${dir#/proc/}; [ "$pid" = "$$" ] && continue',
    # argv is sh, -c, the script of `Host.scoped`, then the unit name
    "args=$(tr '\\0' '\\n' < \"$dir/cmdline\" 2>/dev/null) || continue",
    'args=${args#*"$NL"}; case "$args" in "-c$NL"*) ;; *) continue;; esac',
    'args=${args#*"$NL"}; args=${args#*"$NL"}; case "$args" in "$mark"*) ;; *) continue;; esac',
    'win=$(cat "$dir/winpid" 2>/dev/null)',
    'if [ -n "$win" ] && command -v taskkill >/dev/null 2>&1; '
    'then MSYS2_ARG_CONV_EXCL="*" taskkill /T /F /PID "$win" >/dev/null 2>&1; '
    'else kill -KILL $(tree "$pid") 2>/dev/null; fi',
    'echo "$pid"; done',
])
STOP_TIMEOUT_SECONDS = 60
COMMAND_TIMEOUT_SECONDS = 30 * 60
# A run temporary folder (DESIGN §9): where the machine's option is on, each scoped run gets its own folder, named
# like its scope, in the machine's `run_tmp_dir`. Its first line says which process owns it.
RUN_FOLDER_OWNER = ".kandu-run"
STALE_RUN_FOLDER_MINUTES = 24 * 60  # an older folder whose owner is gone was left when Kandu itself was stopped
MAX_REFERENCE_BYTES = 20 * 1024 * 1024  # what the copy of a task's reference may weigh
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


class ReferenceTooBig(RunnerError):
    """The paths a task's reference names weigh more than a run's copy may."""


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


@dataclass
class AgentResult:
    text: str = ""
    models: dict = field(default_factory=dict)
    duration_ms: int = 0
    is_error: bool = True
    stopped: bool = False
    session_id: str | None = None  # A broken reply is asked for again in this session.
    ended_earlier: list = field(default_factory=list)  # Runs of the same holder still running before it, now ended.


class Host:
    """Runs commands on one machine."""

    def __init__(self, machine):
        self.name = machine.name
        self.ssh_target = None if machine.reach == "local" else machine.reach.split()[1]
        self.run_tmp_dir = machine.run_tmp_dir if getattr(machine, "run_tmp", False) else None
        self._home = None

    def argv(self, command, cwd=None):
        change_dir = f"cd {remote_path(cwd)} && " if cwd else ""
        script = f'{ghmeter.path_line()}; {change_dir}exec {shlex.join(command)}'
        if self.ssh_target is None:
            return ["sh", "-c", script]
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", self.ssh_target, script]

    @staticmethod
    def scoped(command, unit):
        """`command` in its own systemd scope named `unit`. Where the machine has no user systemd, under a shell
        named by `unit` (`NO_SCOPE_RUN`), so `stop_runs` can find it and end its process tree."""
        script = (f'if {SCOPE_PROBE}; then exec systemd-run --user --scope --quiet --collect '
                  f'--property=TimeoutStopSec={STOP_GRACE_SECONDS} --unit={shlex.quote(unit)} -- "$@"; fi; '
                  f'{NO_SCOPE_RUN}')
        return ["sh", "-c", script, unit, *command]

    def stop_scope(self, unit):
        """Ends whatever is left of the run `unit`. A run that is already gone is fine."""
        self.stop_runs(unit)

    def stop_runs(self, mark):
        """Ends every run on this machine whose unit name starts with `mark`, with all it started, and returns
        their names (a scope) or pids (a shell with no user systemd). One that cannot be reached ends nothing."""
        try:
            result = self.run(["sh", "-c", STOP_SCRIPT, "sh", mark], timeout=STOP_TIMEOUT_SECONDS)
        except RunnerError:
            return []
        return (result.stdout or "").split() if result.returncode == 0 else []

    def run_folder(self, unit):
        """The run temporary folder of the run in scope `unit`, or None where the machine's option is off."""
        if self.run_tmp_dir is None:
            return None
        return posixpath.join(self.absolute(self.run_tmp_dir), unit)

    @staticmethod
    def in_run_folder(command, folder, claude=False):
        """`command` with its own fresh `folder` as its temporary directory, and Claude's too for an agent run.

        First, every folder beside it older than a day whose owning process is gone is removed: what a run left when
        Kandu itself was stopped. Then `folder` is made, and this shell, which becomes `command`, is named its owner.
        """
        base = shlex.quote(posixpath.dirname(folder))
        names = "TMPDIR CLAUDE_CODE_TMPDIR" if claude else "TMPDIR"
        script = (f'base={base}; mkdir -p "$base" || {{ echo "kandu: cannot make the run folder in $base" >&2; exit 1; }}; '
                  f'for old in "$base"/kandu-*; do [ -d "$old" ] || continue; '
                  f'[ -n "$(find "$old" -maxdepth 0 -mmin +{STALE_RUN_FOLDER_MINUTES})" ] || continue; '
                  f'owner=$(head -n 1 "$old/{RUN_FOLDER_OWNER}" 2>/dev/null); '
                  f'case "$owner" in ""|*[!0-9]*) ;; *) kill -0 "$owner" 2>/dev/null && continue ;; esac; '
                  f'rm -rf "$old"; done; '
                  f'dir={shlex.quote(folder)}; mkdir -m 700 "$dir" || {{ echo "kandu: cannot make the run folder $dir" >&2; exit 1; }}; echo $$ > "$dir/{RUN_FOLDER_OWNER}"; '
                  + "; ".join(f'{name}="$dir"; export {name}' for name in names.split())
                  + '; exec "$@"')
        return ["sh", "-c", script, "sh", *command]

    def remove_run_folder(self, folder):
        """Removes a run's temporary folder once the run has ended. One a dropped connection leaves goes later."""
        try:
            self.run(["rm", "-rf", "--", folder], timeout=PROBE_TIMEOUT_SECONDS)
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


def holder_mark(holder):
    """How the unit name of every run for `holder` (a task) starts, and no other's."""
    return f"kandu-h{hashlib.sha256(holder.encode()).hexdigest()[:12]}-"


def scope_name(name, holder=None):
    """A unit name for one run: unique, and only characters systemd takes. A run for a `holder` is found by it."""
    start = holder_mark(holder) if holder else "kandu-"
    return f"{start}{re.sub(r'[^A-Za-z0-9_.-]', '-', name)[:80]}-{uuid.uuid4().hex[:8]}"


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

    def prepare_worktree(self, project, repo, branch, base, cleaned=None, settled=None):
        """Returns the worker's worktree on `branch`, reusing it if an earlier run left one.

        A merge, rebase or cherry-pick an earlier run left unfinished there is cancelled first (dropped, with its
        uncommitted changes, when git refuses to cancel it), and `cleaned` is called with what was cancelled. A reused worktree behind the pushed branch is fast-forwarded; one ahead
        keeps its commits. One whose branch has drifted from the pushed one is settled, and `settled` is called
        with how (see `_settle`). If the folder holds commits that are not on GitHub beside an unfinished
        operation, or a drift cannot be settled safely, it is left as it is and `LocalWork` is raised.
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
                    self._catch_up(host, directory, branch, settled)
                return directory
            host.check(["git", "worktree", "prune"], cwd=path)
            start = f"origin/{branch}" if pushed else f"origin/{base}"
            host.check(["git", "worktree", "add", "-B", branch, directory, start], cwd=path)
        return directory

    @classmethod
    def _catch_up(cls, host, directory, branch, settled):
        """Fast-forwards a reused worktree to the pushed `branch` when it is behind. A worktree that already holds
        the pushed head (equal or ahead) keeps its commits, for the worker to push. One that has drifted apart
        is settled by `_settle`."""
        remote = f"origin/{branch}"
        if host.succeeds(["git", "merge-base", "--is-ancestor", remote, "HEAD"], cwd=directory):
            return
        if host.succeeds(["git", "merge-base", "--is-ancestor", "HEAD", remote], cwd=directory):
            host.check(["git", "merge", "--ff-only", remote], cwd=directory)
            return
        cls._settle(host, directory, branch, settled)

    @staticmethod
    def _settle(host, directory, branch, settled):
        """Settles a local `branch` that has drifted from the pushed one, keeping every commit of both sides.

        When each commit only on GitHub has a copy with the same change in the folder, as after a rebase onto a
        newer base, the folder's branch replaces GitHub's with `--force-with-lease`, so a push made meanwhile is
        never overwritten. Otherwise the pushed head is merged into the folder's branch, for the worker to push.
        A merge in conflict, uncommitted changes in its way, or a refused lease leave the folder as it was and
        raise `LocalWork`."""
        remote = f"origin/{branch}"
        head = host.check(["git", "rev-parse", "HEAD"], cwd=directory).strip()
        theirs = host.check(["git", "rev-parse", remote], cwd=directory).strip()
        local = host.check(["git", "rev-list", "--count", "HEAD", "--not", remote], cwd=directory).strip()
        missing = host.check(["git", "rev-list", "--count", "--cherry-pick", "--right-only", f"HEAD...{remote}"],
                             cwd=directory).strip()

        def stop(why):
            return LocalWork(f"The task's branch {branch} on {host.name} has drifted from the one on GitHub, and "
                             f"Kandu cannot settle the two safely: {why}. It left the folder as it is and nothing is "
                             f"lost: rebase or merge the two there, push the result, then send the task back.")

        if missing == "0":  # merges never count as copied, so a branch with one is merged, never forced
            if not host.succeeds(["git", "push", f"--force-with-lease=refs/heads/{branch}:{theirs}", "origin",
                                  f"HEAD:refs/heads/{branch}"], cwd=directory):
                raise stop(f"GitHub's branch moved from {theirs[:12]} meanwhile, so pushing over it was refused")
            if settled is not None:
                settled(f"the folder's branch held a rebased copy of every commit on GitHub, so Kandu pushed it "
                        f"({head[:12]}) over GitHub's {theirs[:12]} with --force-with-lease")
            return
        if host.check(["git", "status", "--porcelain", "--untracked-files=no"], cwd=directory).strip():
            raise stop(f"merging the {missing} commit(s) only on GitHub needs the folder's uncommitted changes "
                       f"out of the way")
        message = f"Merge the pushed {branch} into the task's folder"
        if not host.succeeds(["git", "merge", "--no-ff", "-m", message, remote], cwd=directory):
            conflicts = host.check(["git", "diff", "--name-only", "--diff-filter=U"], cwd=directory).split()
            merge_head = host.check(["git", "rev-parse", "--git-path", "MERGE_HEAD"], cwd=directory).strip()
            if host.succeeds(["test", "-e", merge_head], cwd=directory):
                host.check(["git", "merge", "--abort"], cwd=directory)
            raise stop(f"merging GitHub's head {theirs[:12]} conflicts in {', '.join(conflicts)}" if conflicts
                       else f"merging GitHub's head {theirs[:12]} failed")
        if settled is not None:
            settled(f"the folder's branch ({local} commit(s) of its own) and GitHub's ({missing} of its own) had "
                    f"drifted apart, so Kandu merged GitHub's head {theirs[:12]} into the folder's {head[:12]}")

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
        if host.succeeds(["git", operation, "--abort"], cwd=directory):
            if cleaned is not None:
                cleaned(f"cancelled a {operation} an earlier run left unfinished in the task's folder")
            return
        # `--abort` refuses a folder where a file it must reset changed after it was staged, such as a built file
        # a worker rebuilt after resolving a conflict. Every commit here is on GitHub, so only work that was never
        # committed or reviewed is lost: the operation is dropped and the folder reset to the task's branch.
        host.check(["git", operation, "--quit"], cwd=directory)
        host.check(["git", "checkout", "--force", branch], cwd=directory)
        host.check(["git", "reset", "--hard"], cwd=directory)
        if cleaned is not None:
            cleaned(f"dropped a {operation} an earlier run left unfinished in the task's folder, and its uncommitted "
                    f"changes: git refused to cancel it, and every commit there is on GitHub")

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

        With `scoped`, what the command leaves running is ended with it, and where the machine's option is on it
        gets its own run temporary folder, removed when it ends. A deploy passes False: it may start what must
        keep running, so it gets neither.
        """
        host = self.hosts[project.machine]
        script = command
        if secrets and project.secrets_from:
            script = f"set -a; . {shlex.quote(host.absolute(project.secrets_from))}; set +a; {command}"
        unit = scope_name(log_name) if scoped else None
        folder = host.run_folder(unit) if unit else None
        argv = ["sh", "-c", script]
        if folder:
            argv = Host.in_run_folder(argv, folder)
        if unit:
            argv = Host.scoped(argv, unit)
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
                if folder:
                    host.remove_run_folder(folder)
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

    def test_failures(self, log_name):
        """The names of the tests that failed in the run `log_name`, read from its local log; none if it names none."""
        try:
            return mainred.failing_tests((self.logs_dir / f"{log_name}.log").read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return []

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

    def make_reference_folder(self, project):
        """A fresh folder of one run's own on the project's machine, for a read-only copy of a task's reference.

        In the machine's `run_tmp_dir` where its option is on, else in its temporary directory. Returns its path.
        """
        host = self.hosts[project.machine]
        base = host.absolute(host.run_tmp_dir) if host.run_tmp_dir else None
        script = ('base=$1; if [ -n "$base" ]; then mkdir -p "$base" || exit 1; else base=${TMPDIR:-/tmp}; fi; '
                  'mktemp -d "$base/kandu-ref-XXXXXXXX"')
        return host.check(["sh", "-c", script, "sh", base or ""]).strip()

    def copy_reference(self, source, repo, paths, project, folder):
        """Copies the tracked files under `paths` of `source`'s copy of `repo` into `folder` on `project`'s machine,
        and makes them read-only. Only those files: nothing untracked, no symlink, no git folder.

        Returns (paths copied, paths not found). Raises `Unreachable` when the source machine does not answer, and
        `ReferenceTooBig` or `RunnerError` when it cannot be read.
        """
        origin = self.hosts[source.machine]
        copy = origin.absolute(repo_path(source, repo))
        found, missing = [], []
        for path in paths:
            listed = origin._answered(origin.run(["git", "--literal-pathspecs", "ls-files", "-z", "--", path], cwd=copy))
            if listed.returncode != 0:
                raise RunnerError(f"git ls-files failed on {origin.name}: {listed.stderr.strip()}")
            (found if listed.stdout else missing).append(path)
        if not found:
            return [], missing
        pack = ('git --literal-pathspecs ls-files -z -- "$@" | tar -c --null -T - -f - | head -c $(({max} + 1))'
                .format(max=MAX_REFERENCE_BYTES))
        try:
            packed = subprocess.run(origin.argv(["sh", "-c", pack, "sh", *found], cwd=copy), capture_output=True,
                                    timeout=GIT_TIMEOUT_SECONDS, env=child_env())
        except subprocess.TimeoutExpired as e:
            raise RunnerError(f"reading the reference timed out on {origin.name}") from e
        if origin.ssh_target is not None and packed.returncode == SSH_FAILED:
            raise Unreachable(f"{origin.name} did not answer over SSH")
        if packed.returncode != 0 or not packed.stdout:
            raise RunnerError(f"could not read the reference on {origin.name}")
        if len(packed.stdout) > MAX_REFERENCE_BYTES:
            raise ReferenceTooBig(f"the reference is larger than {MAX_REFERENCE_BYTES} bytes")
        host = self.hosts[project.machine]
        target = posixpath.join(folder, repo.replace("/", "__"))
        unpack = ('mkdir -p "$1" && tar -x -f - -C "$1" --no-same-owner --no-same-permissions && '
                  'find "$1" -type l -delete && chmod -R a-w "$1"')
        try:
            done = subprocess.run(host.argv(["sh", "-c", unpack, "sh", target]), input=packed.stdout,
                                  capture_output=True, timeout=GIT_TIMEOUT_SECONDS, env=child_env())
        except subprocess.TimeoutExpired as e:
            raise RunnerError(f"writing the reference timed out on {host.name}") from e
        if host.ssh_target is not None and done.returncode == SSH_FAILED:
            raise Unreachable(f"{host.name} did not answer over SSH")
        if done.returncode != 0:
            raise RunnerError(f"could not write the reference on {host.name}")
        return found, missing

    def remove_reference_folder(self, project, folder):
        """Removes a run's reference folder, read-only files and all. One a dropped connection leaves is only a
        temporary folder: a later run's sweep or the machine's own cleaning takes it."""
        try:
            self.hosts[project.machine].run(["sh", "-c", 'chmod -R u+w -- "$1" 2>/dev/null; rm -rf -- "$1"', "sh", folder],
                                            timeout=PROBE_TIMEOUT_SECONDS)
        except RunnerError:
            pass

    def run_agent(self, machine, cwd, prompt, allowed, denied, run_id, should_stop, model=None, tokens=None,
                  schema=None, resume=None, holder=None):
        """Runs one `claude` process to its end; `should_stop` is polled while it runs.

        The run is in its own scope (`Host.scoped`): when it ends, or is
        stopped, every process it started on the machine ends with it, over
        SSH too. Where the machine's option is on, the run has its own run
        temporary folder, removed when it ends. With no `model`,
        `claude` picks its own default. `tokens` is (file, names): those
        values alone reach this process, in its environment. If one is
        missing the run ends with an error. With a `schema`, `claude` holds the
        reply to that JSON Schema, and the text is the object it returns.
        With `resume`, the prompt goes to that earlier session in `cwd`.
        A `holder` (a task) has one run at a time: a run of it still going on
        the machine, as one a cycle that died leaves behind, is ended before
        this one starts, and named in `ended_earlier`.
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
        ended = host.stop_runs(holder_mark(holder)) if holder else []
        unit = scope_name(run_id, holder)
        folder = host.run_folder(unit)
        if folder:
            command = Host.in_run_folder(command, folder, claude=True)
        process = subprocess.Popen(host.argv(Host.scoped(command, unit), cwd), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=child_env(run_id))
        stopped = threading.Event()
        threading.Thread(target=self._feed, args=(process, prompt), daemon=True).start()
        threading.Thread(target=self._watch, args=(process, should_stop, stopped, lambda: host.stop_scope(unit)),
                         daemon=True).start()
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
            if folder:
                host.remove_run_folder(folder)
        if result is None:
            return AgentResult(stopped=stopped.is_set(), ended_earlier=ended)
        usage = usage_from_result(result)
        structured = result.get("structured_output")
        text = json.dumps(structured, ensure_ascii=False) if structured is not None else result.get("result") or ""
        return AgentResult(text=text, models=usage["models"], duration_ms=usage["duration_ms"],
                           is_error=usage["is_error"] or process.returncode != 0, stopped=stopped.is_set(),
                           session_id=result.get("session_id"), ended_earlier=ended)

    @staticmethod
    def _feed(process, prompt):
        # In its own thread: a prompt larger than the pipe must not block reading the output.
        try:
            process.stdin.write(prompt)
            process.stdin.close()
        except (BrokenPipeError, ValueError):
            pass

    @staticmethod
    def _watch(process, should_stop, stopped, end):
        while process.poll() is None:
            if should_stop():
                stopped.set()
                process.terminate()
                time.sleep(STOP_GRACE_SECONDS)
                if process.poll() is None:
                    end()  # all the run started, while the shell it runs under is still there to find them by
                    process.kill()
                return
            time.sleep(POLL_SECONDS)
