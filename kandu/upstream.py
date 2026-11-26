"""How far the install's own `main` is from the public Kandu it was cloned from (DESIGN §12). Off by default.

The check only fetches and counts: it never merges, pushes, or changes `main`, a branch or a file of the
install, and it costs no tokens. Updates come in only as one task on the board of Kandu's own project, worked,
reviewed and merged like any other, on the user's yes.
"""
import os
import re
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import tasks
from .github import GitHubError
from .notify import Message

INSTALL_DIR = Path(__file__).resolve().parent.parent
MAIN = "main"  # the install's own branch, the one the user runs
GIT_SECONDS = 120
TICK_SECONDS = 300  # how often the dashboard asks whether a check is due; the check itself waits `every_hours`
REASON_CHARS = 200
TASK_FIELDS = {"priority": "Medium", "kind": "Chore", "size": "Small"}


class CheckError(Exception):
    pass


def _git(repo, *args):
    """Runs git in the install, never asking for a password. Returns its output, or raises CheckError."""
    env = os.environ | {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "LC_ALL": "C"}
    try:
        done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=GIT_SECONDS, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CheckError(f"git {args[0]}: {type(e).__name__}") from None
    if done.returncode != 0:
        raise CheckError(f"git {args[0]}: {_first_line(done.stderr) or f'exit {done.returncode}'}")
    return done.stdout.strip()


def _first_line(text):
    """A line of git's error, with any password in a URL cut out, so the log keeps no secret."""
    line = next((l.strip() for l in text.splitlines() if l.strip()), "")
    return re.sub(r"(\w+://)[^/@\s]*@", r"\1", line)[:REASON_CHARS]


def count(repo, remote, branch):
    """Fetches `remote`'s `branch` and returns (behind, ahead): commits `main` lacks, and its own.

    Only the remote-tracking ref moves; `main`, the checked-out branch and the files stay as they were.
    """
    tracking = f"refs/remotes/{remote}/{branch}"
    _git(repo, "fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", remote,
         f"+refs/heads/{branch}:{tracking}")
    behind = _git(repo, "rev-list", "--count", f"refs/heads/{MAIN}..{tracking}")
    return int(behind), ahead(repo, tracking)


def ahead(repo, tracking):
    """The install's own changes that `main` still holds, counted in commits. The one count of "ahead": the check
    logs it, and the dashboard and «Bring in updates» read it from there.

    Not counted: merge commits, and every commit from before `main`'s last own commit whose files were exactly those
    of a commit of `tracking`, as a change undone or brought back to upstream's. So `main` with upstream's files is 0.
    Only commits `tracking` lacks are searched: an upstream commit that `main` merged has upstream's files by nature.
    """
    theirs = set(_git(repo, "log", "--format=%T", tracking).split())
    log = _git(repo, "log", "--topo-order", "--format=%H %T", f"refs/heads/{MAIN}", f"^{tracking}")
    mine = (line.split() for line in log.splitlines())
    base = next((sha for sha, tree in mine if tree in theirs), None)  # newest first
    return int(_git(repo, "rev-list", "--count", "--no-merges", f"refs/heads/{MAIN}", f"^{tracking}",
                    *([f"^{base}"] if base else [])))


def public_url(repo, remote):
    """The remote's URL without a user or password, for the task's worker; None when it cannot be read."""
    try:
        url = _git(repo, "remote", "get-url", remote)
    except CheckError:
        return None
    if "://" not in url:
        return url  # `git@host:owner/repo`: no password in it
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))  # host and port as written, or none


def check(store, settings, repo=INSTALL_DIR, now=None, tell=None):
    """Counts once and logs the answer: `upstream_checked`, or `upstream_check_failed` with git's reason.

    `tell` gets what the user must hear of it (`news`), if anything.
    """
    before, good = last_try(store), store.last_event("upstream_checked")
    try:
        behind, own = count(repo, settings.remote, settings.branch)
    except CheckError as e:
        event = store.record("upstream_check_failed", now=now, remote=settings.remote, branch=settings.branch,
                             reason=str(e))
    else:
        event = store.record("upstream_checked", now=now, remote=settings.remote, branch=settings.branch,
                             behind=behind, ahead=own)
    message = news(event, before, good)
    if message is not None and tell is not None:
        tell(message)
    return event


def news(event, before, good):
    """What the user hears of a check, the `upstream` kind of notification, or None. `before` is the check before
    it, good or failed, and `good` the last good one before it; both are read from the log, so a restart forgets
    nothing.

    The first check ever is not news, nor are counts the last good check had too. A failure is told once: not again
    until a check succeeds. Git's reason stays on the dashboard.
    """
    if before is None:
        return None
    values = {"theirs": f"{event['remote']}/{event['branch']}"}
    if event["type"] == "upstream_check_failed":
        return None if before["type"] == "upstream_check_failed" else Message("upstream", "upstream_failed", values=values)
    if good is None or (good["behind"], good["ahead"]) == (event["behind"], event["ahead"]):
        return None
    text = "upstream_behind" if event["behind"] else "upstream_counts"
    return Message("upstream", text, values=values | {"behind": event["behind"], "ahead": event["ahead"]})


def last_try(store):
    """The newest check, good or failed. By its place in the log: two in one second have the same time."""
    for day in reversed(store.days()):
        for event in reversed(store.events(day)):
            if event["type"] in ("upstream_checked", "upstream_check_failed"):
                return event
    return None


def due(store, every_hours, now):
    """True when no check ran in the last `every_hours`; a failed one counts too, so a dead network is not hammered."""
    last = last_try(store)
    return last is None or now - datetime.fromisoformat(last["ts"]) >= timedelta(hours=every_hours)


def status(store, settings):
    """What the dashboard shows: the last check's counts, or that it failed and when the last good one was."""
    last, good = last_try(store), store.last_event("upstream_checked")
    failed = last is not None and last["type"] == "upstream_check_failed"
    return {"remote": settings.remote, "branch": settings.branch, "every_hours": settings.every_hours,
            "checked_at": last and last["ts"], "failed": failed,
            "behind": None if failed or good is None else good["behind"],
            "ahead": None if failed or good is None else good["ahead"],
            "last_good_at": good and good["ts"]}


def open_task(store, github):
    """The task the button made, while its issue is open, as {"task", "url"}; else None.

    Read from Kandu's copy. One the copy does not know yet was made a moment ago, so it counts as open.
    """
    made = store.last_event("upstream_task_created")
    if made is None:
        return None
    repo, _, number = made["task"].rpartition("#")
    try:
        is_open = github.issue_is_open(repo, int(number))
    except GitHubError:
        is_open = True
    return {"task": made["task"], "url": made["url"]} if is_open else None


def task_text(behind, ahead, remote, branch, url):
    """The task's title and body: bring the commits in through a PR, keep the install's own changes
    except those upstream merged, where upstream's version wins."""
    commits = f"{behind} commit{'s' if behind != 1 else ''}"
    theirs = f"`{remote}/{branch}`"
    own = f", and holds {ahead} commit{'s' if ahead != 1 else ''} of its own" if ahead else ""
    reach = (f"The worker's clone may not have the `{remote}` remote: add it as `git remote add {remote} {url}`, "
             f"and never push to it.\n") if url else f"Fetch {theirs} into the worker's clone; never push to it.\n"
    title = f"Bring in the {commits} of {theirs} that main lacks"
    body = f"""## Goal
The install's `main` holds the {commits} of {theirs} it lacks, brought in through a PR the user approves.

## Current behavior
The last upstream check found `main` {commits} behind {theirs}{own}.

## Acceptance criteria
- [ ] The {commits} of {theirs} that `main` lacks are merged into this task's branch, and its PR targets `main`.
- [ ] Where an upstream change conflicts with the install's own change, the install's own change is kept.
- [ ] Where the install's own change was sent upstream and merged there, perhaps edited, the upstream version is kept, so `main` holds one version of it. Such a change is an install commit whose subject matches an upstream commit's, which may end in ` (#N)`.
- [ ] The project's tests pass on the branch.

## How to test
On the PR's branch, `git rev-list --count HEAD..{remote}/{branch}` prints `0`, and the test command passes.
{reach}
## Out of scope
- Sending a PR or a push to the upstream repo.
- Merging into `main` other than through this task's PR.
- Changing how a task is worked, reviewed or merged.

## Blocked by
none
"""
    return title, body


class Upstream:
    """The check and the button, with their locks: two presses or a press beside the timer run one at a time."""

    def __init__(self, repo=INSTALL_DIR):
        self.repo = Path(repo)
        self._check_lock = threading.Lock()
        self._task_lock = threading.Lock()

    def check(self, store, settings, now=None, tell=None):
        with self._check_lock:
            return check(store, settings, self.repo, now, tell)

    def tick(self, store, settings, now=None, tell=None):
        """Checks when due. Returns True when it checked."""
        now = now or datetime.now().astimezone()
        with self._check_lock:
            if not settings.enabled or not due(store, settings.every_hours, now):
                return False
            check(store, settings, self.repo, now, tell)
            return True

    def bring_in(self, config, github, store, local_dir):
        """Returns (task, url, created). Raises ValueError when there is nothing to bring in."""
        settings = config.settings.upstream
        with self._task_lock:
            made = open_task(store, github)
            if made is not None:
                return made["task"], made["url"], False
            shown = status(store, settings)
            if not shown["behind"]:
                raise ValueError("main lacks no upstream commit at the last good check: check again first")
            title, body = task_text(shown["behind"], shown["ahead"], settings.remote, settings.branch,
                                    public_url(self.repo, settings.remote))

            def created(url):  # Logged as soon as the issue exists, so a failed board step makes no second one.
                number = int(url.rstrip().rsplit("/", 1)[1])
                store.record("upstream_task_created", task=f"{repo}#{number}", project=settings.project, url=url,
                             behind=shown["behind"])

            project = next(p for p in config.projects if p.name == settings.project)
            repo = project.repos[0]
            url = tasks.create_task(config, github, settings.project, repo, title, body, local_dir,
                                    created=created, store=store, **TASK_FIELDS)
            number = int(url.rstrip().rsplit("/", 1)[1])
            return f"{repo}#{number}", url, True


def tick_forever(tick, stop=None, every=TICK_SECONDS):
    """Calls `tick()` in a background thread until `stop` is set. A tick that raises waits for the next."""
    stop = stop or threading.Event()

    def loop():
        while not stop.is_set():
            try:
                tick()
            except Exception:  # The dashboard keeps serving whatever one check did.
                pass
            stop.wait(every)
    threading.Thread(target=loop, daemon=True, name="upstream").start()
    return stop
