"""The main loop (DESIGN §5, §6).

This is code, not an LLM session: everything that needs no judgment runs here,
at no token cost, and still works when Claude is down. Agents, and the queen
above them, are started only for work that needs judgment. No state is kept between cycles beyond what the
board and the PR hold, so a cycle that is cut off resumes where it stopped.
"""
import argparse
import contextlib
import fcntl
import json
import secrets
import shlex
import sys
import threading
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta

from pathlib import Path

from . import agents, clean, debt, mainred, merge, openprs, queen, questions, reference, review, show, sizing, split, tasks, waits
from .config import DEFAULT_LOCAL_DIR, TECHNICAL_DEBT, ConfigError, load_config
from .flights import Flights
from .base import base_branch, missing as base_missing, missing_repos, record as record_base
from .github import BREAKING_LABEL, TOKENS_LABEL, GitHubError, blockers_in, branch_for, task_blockers, transient
from .mirror import open_github
from .notify import CHECK_NOT_RUN, LIVE_DOWN, TESTS_FAIL_IN, notifier_for
from .runner import DEFAULT_LOGS_DIR, LocalWork, ReferenceTooBig, Runner, RunnerError, Unreachable
from .state import StateStore
from .tasks import TaskError
from .tries import FAILURE_COMMENT, INTERNAL_TRIES, SIZING_FAILURE_COMMENT, count as count_tries
from .usage import QUEEN_LIMIT, SESSION_LIMIT, WEEK_LIMIT, UsageError, over_limit, read_usage

BACKLOG, READY, IN_PROGRESS, IN_REVIEW = "Backlog", "Ready", "In progress", "In review"
NEEDS_INFO, BLOCKED, DONE = "Needs info", "Blocked", "Done"
NEEDS_SPLIT = split.NEEDS_SPLIT
RESUMED = (IN_PROGRESS, IN_REVIEW)
MERGEABLE = ("approved", "approved_with_notes")
END_CHECK_MERGES = 2  # This many merges in a project in one cycle earn a check of the whole project.
LANDING_FAILED = ("deploy_failed", "live_check_failed")
BROKEN_REPLY = (agents.ReplyError, review.ReportError, split.ReportError)
BASE_MOVED = "Base branch was modified"  # another merge landed first; GitHub asks to try once more
MERGE_TRIES = 3  # Kandu's own trouble is tried this often in a row before the task stops for a look.
MERGE_RETRY_WAITS = (10, 30, 60)  # seconds before each quick new try of a merge GitHub refused for a moment
MERGE_RETRY_EVERY = 60  # then one try about this often, until the cycle ends
RETRY_POLL_SECONDS = 5  # how often a wait for the next try looks for a stop or a yes
ALREADY_MERGED = "already merged"
STOPPED_BY_USER = "stopped by the user"
CUT_OFF = "cycle time ran out while an agent was running"
RETRYING = "GitHub refused the merge for a moment; Kandu tries it again by itself"
WAITS_FOR_BLOCKER = "blocker {} is not merged yet"
WAITS_FOR_TEST_RUN = waits.TEST_RUN_BUSY
ROOT = Path(__file__).resolve().parent.parent
PR_TEST_OUTPUT_LINES = 40  # the tail of the test run a `pr_test_output` section holds
UNREAD_PR_BODY = ("The worker pushed this branch, but Kandu could not read its last message, so this description "
                  "is Kandu's. The review reads the code.")


@dataclass
class Task:
    project: object
    repo: str
    number: int
    title: str
    url: str
    status: str
    branch: str
    priority: str = ""
    tokens: bool = False           # labelled `uses-tokens`: its worker gets the project's task_tokens
    breaking: bool = False         # labelled `breaking`: its PR carries the same mark

    @property
    def key(self):
        return f"{self.repo}#{self.number}"

    @property
    def order(self):
        """Work already begun, then `High`. Nothing that is running gives way."""
        rank = 0 if self.status in RESUMED else 1 if self.priority == queen.HIGH else 2
        return (rank, self.project.priority, self.number)


class CycleStop(Exception):
    """This cycle must end; the task resumes in a later one."""


class TaskFailed(Exception):
    """Something went wrong that a person must look at."""


class StoppedByUser(TaskFailed):
    """The user stopped this worker. Only the user sends it back."""


class KanduTrouble(TaskFailed):
    """Kandu's own trouble, not the task's: a run that crashed, or a reply that could not be read."""


def internal(error):
    """True for trouble that a later try can pass without anyone changing anything."""
    if isinstance(error, StoppedByUser):
        return False
    if isinstance(error, (KanduTrouble, RunnerError)):
        return True
    return isinstance(error, GitHubError) and transient(error)


def new_run_id(now):
    return f"r-{now:%Y%m%d%H%M%S}-{secrets.token_hex(3)}"


def board_items(config, github, paused=()):
    """Yields (project, item) for each `agent` issue of a registered project that is not paused.

    `paused` names the projects the user paused on the dashboard.
    """
    return ((project, item) for project, item in _registered_items(config, github, paused) if not item.by_hand)


def by_hand_items(config, github, paused=()):
    """Yields (project, item) for each open by-hand task of a registered project that is not paused.

    They are for the queen to name, never to work: `board_items` leaves them out, so no decision can reach them.
    """
    return ((project, item) for project, item in _registered_items(config, github, paused) if item.waits_by_hand)


def _registered_items(config, github, paused):
    by_board = {}
    for project in config.projects:
        if project.board is not None and not project.paused and project.name not in paused:
            by_board.setdefault((project.board.owner, project.board.number), []).append(project)
    for projects in by_board.values():
        by_repo = {repo: project for project in projects for repo in project.repos}
        for item in github.board_items(projects[0].board):
            project = by_repo.get(item.repo)
            if project is not None:
                yield project, item


def read_tasks(config, github, paused=()):
    """Returns the tasks the loop may work on: registered, not paused, and open.

    A blocker does not hold back the work, only the merge (`_merge_waits`).
    """
    tasks = []
    for project, item in board_items(config, github, paused):
        if item.status not in (READY, *RESUMED):
            continue
        if item.status == READY and not _can_start(github, item):
            continue
        tasks.append(_task(project, item))
    return sorted(tasks, key=lambda t: t.order)


def _task(project, item):
    return Task(project, item.repo, item.number, item.title, item.url, item.status,
                branch_for(item.number, item.title), item.priority,
                TOKENS_LABEL in item.labels and bool(project.task_tokens), BREAKING_LABEL in item.labels)


def _can_start(github, item):
    return github.issue_is_open(item.repo, item.number)


def open_blockers(github, repo, issue, last_of=lambda key: None):
    """The blockers of a task still open: its PR may not merge before theirs.

    `issue` is its `issue_text`: the references under `## Blocked by` and GitHub's "blocked by" relationships the
    copy keeps for it both count, each once (`task_blockers`).

    `last_of(key)` names a split task's last slice, or None. A split blocker stands for its last slice: slices merge
    in order, so the last one closing means all are done, while the split task stays open until then.
    """
    keys = dict.fromkeys(last_of(key) or key for key in (f"{r}#{n}" for r, n in task_blockers(issue["body"], repo, issue.get("blocked_by"))))
    pairs = [key.rpartition("#") for key in keys]
    return [f"{r}#{n}" for r, _, n in pairs if github.issue_is_open(r, int(n))]


# What ends a hold in her view: a new head, a dropped blocker, a new merge decision or a worker's step.
# Anything else on the task, such as her own decision or a drop ask, leaves the merge held as before.
_ENDS_HOLD = {"review_finished", "blocker_dropped", "decision", "task_picked", "phase_changed", "run_finished",
              "task_finished"}


def _held_by_blockers(events, key):
    """True when the task's last merge check held it only on open blockers, with no new head, review or step since."""
    for event in reversed(events):
        if event.get("task") != key:
            continue
        if event["type"] == "merge_held":
            return True
        if event["type"] in _ENDS_HOLD:
            return False
    return False


# What ends Kandu's own retry of a merge in her view: a new head, an end, or any decision but its own internal error.
_ENDS_RETRY = ("review_finished", "task_finished", "merge_held")


# The decisions after which a first run's pushed reply has done its job: its PR is open.
OPENED = ("pr_opened", "reply_unread_pr_found")


def merge_retry(events, key):
    """The task's last `merge_retry` while Kandu still tries its merge again by itself, or None."""
    for event in reversed(events):
        if event.get("task") != key:
            continue
        if event["type"] == "merge_retry":
            return event
        if event["type"] in _ENDS_RETRY or (event["type"] == "decision" and event["decision"] != "internal_error"):
            return None
    return None


def _at_work(item, events, github):
    """A worker builds it or is about to. A review takes no worker's place, nor does a merge held only by blockers,
    nor a `Ready` card whose issue is closed, which the loop never starts."""
    if item.status == READY and not _can_start(github, item):
        return False
    return item.status in (READY, IN_PROGRESS) and not _held_by_blockers(events, f"{item.repo}#{item.number}")


class Supervisor:
    def __init__(self, config, store, github, runner, usage_reader, local_dir=DEFAULT_LOCAL_DIR, now=None,
                 sleep=time.sleep, flights=None):
        self.config = config
        self.flights = flights or Flights(config, store)  # the free flights, which the merge cleanup may end
        self.store = store
        self.github = github
        self.runner = runner
        self.usage_reader = usage_reader
        self.local_dir = local_dir
        self.now = now or (lambda: datetime.now().astimezone())
        self.deadline = None
        self.usage = None
        self.halt_reason = None
        self.queen_trouble = None
        self.queen_run = None
        self.workers = {}
        self.waiting = []
        self.seen = {}  # machine -> whether it answered when last asked this cycle
        self.waiting_on = defaultdict(set)  # machine -> the tasks that wait for it this cycle, to be sized or worked
        self.unreachable = []  # what the dashboard shows of each machine that does not answer
        self.merged = Counter()
        self.held = {}  # task key -> the head whose merge waited this cycle
        self.base_held = set()  # the projects whose base branch GitHub lacks this cycle: none of their tasks starts
        self.pr_held = set()  # the projects at their open-PR limit: none of their tasks starts from `Ready`
        self.retrying = {}  # task key -> the merge GitHub refused for a moment, tried again in this cycle
        self.sleep = sleep
        self.next_wake = None
        self._lock = threading.Lock()
        self._deploy_locks = defaultdict(threading.Lock)
        self._main_lock = threading.Lock()  # one test of `main` at a time, so a commit is tested at most twice
        self._test_locks = defaultdict(threading.Lock)  # project name -> held while its merge test runs

    def _is_paused(self, project):
        """Paused by the user or the registry, or held this cycle because its base branch is missing."""
        return project.name in self._held_projects() or project.paused

    def _held_projects(self):
        return self.store.paused_projects() | self.base_held

    def _check_bases(self):
        """Reads whether each project that names a base branch has it on GitHub, and returns those that lack it.

        What GitHub shows is recorded when it changes, for the dashboard. A read that fails keeps the last record.
        """
        for project in self.config.projects:
            if not project.base_branch:
                continue
            try:
                record_base(self.store, project, missing_repos(self.github, project))
            except GitHubError:
                pass
        return {p.name for p in self.config.projects if p.base_branch and base_missing(self.store, p) is not None}

    def _check_open_prs(self):
        """Reads from the copy which projects are at their open-PR limit, and logs a hold that starts or ends."""
        self.pr_held = openprs.held(self.store)
        for project in self.config.projects:
            self._open_pr_hold(project)

    def _open_pr_hold(self, project):
        """The open PRs that hold `project` from new work now, or None. Read from the copy, so it costs GitHub
        nothing, and asked again before each start: a PR opened this cycle counts at once."""
        prs = openprs.at_limit(self.github, project, self.config.settings.github.account)
        with self._lock:
            was = project.name in self.pr_held
            if openprs.record(self.store, project, prs, was):
                self.pr_held.add(project.name)
            else:
                self.pr_held.discard(project.name)
        return prs

    def _board_items(self):
        """A spec issue is never work, even when someone puts it on the board with the `agent` label."""
        specs = tasks.specs(self.store)
        return [(project, item) for project, item in board_items(self.config, self.github, self._held_projects())
                if f"{item.repo}#{item.number}" not in specs]

    def _by_hand(self):
        """The open by-hand tasks the queen names in her brief. A spec issue is never one."""
        specs = tasks.specs(self.store)
        return [(project, item) for project, item in by_hand_items(self.config, self.github, self._held_projects())
                if f"{item.repo}#{item.number}" not in specs]

    def _read_tasks(self):
        specs = tasks.specs(self.store)
        return [task for task in read_tasks(self.config, self.github, self._held_projects())
                if task.key not in specs]

    def _sent_on(self):
        """Tasks outside the working columns that the queen sent a worker or a checker to."""
        events = queen.all_events(self.store)
        return [_task(project, item) for project, item in self._board_items()
                if item.status not in (READY, *RESUMED, DONE)
                and queen.pending_step(events, f"{item.repo}#{item.number}") is not None]

    def _merged_elsewhere(self, tasks):
        """Tasks in `Ready`, `In progress`, `In review` or `Blocked` whose PR merged and that `tasks` does not hold.

        A `Ready` card whose issue is already closed is not read as a task to start, and a `Blocked` one is not read
        at all, yet each lands like any merged task. A `Blocked` card whose deploy or live check failed stays for a
        person to look at."""
        finished = {e["task"]: e["outcome"] for e in queen.all_events(self.store) if e["type"] == "task_finished"}
        have = {task.key for task in tasks}
        found = []
        for project, item in self._board_items():
            key = f"{item.repo}#{item.number}"
            if item.status not in (READY, *RESUMED, BLOCKED) or key in have:
                continue
            if item.status == BLOCKED and finished.get(key) in LANDING_FAILED:
                continue
            pr = self.github.find_pr(item.repo, item.number)
            if pr is not None and pr["state"] == "MERGED":
                found.append(_task(project, item))
        return found

    def _board_tasks(self):
        tasks = self._read_tasks()
        return tasks + self._merged_elsewhere(tasks)

    def _tell_stuck_cards(self):
        """A `Ready` card whose issue is closed is never driven: the user hears of it once, with why. One whose PR
        merged lands anyway (`_merged_elsewhere`), so it is not stuck."""
        for project, item in self._board_items():
            key = f"{item.repo}#{item.number}"
            if item.status != READY or _can_start(self.github, item) \
                    or self.store.last_event("card_stuck", task=key) is not None:
                continue
            pr = self.github.find_pr(item.repo, item.number)
            if pr is None or pr["state"] != "MERGED":
                self.store.record("card_stuck", task=key, project=project.name, reason="its issue is closed")

    # Limits. Each of these is checked in code before any agent starts.

    @property
    def stop_file(self):
        return self.store.stop_file

    def _hard_stop(self):
        """True when running agents must be killed: the deadline passed. A stop by the user kills none: each run
        finishes its step, and nothing new starts (`_stopping`)."""
        return self.deadline is not None and self.now() >= self.deadline

    def _stopping(self):
        """True while the user has Kandu stopped: no new run, merge or deploy starts. A resume undoes it at once."""
        return self.stop_file.exists()

    def _gate(self):
        """Returns why no agent may start now, or None."""
        if self._stopping():
            return STOPPED_BY_USER
        return self._limit()

    def _limit(self):
        """Returns which limit holds every agent back now, or None: the cycle's time or the usage."""
        if self.deadline is not None and self.now() >= self.deadline:
            return "cycle time is up"
        if self.config.settings.claude.auth == "api_key":
            return None
        try:
            self.usage = self.usage_reader()
        except UsageError as e:
            return f"usage unknown, not starting: {e}"
        return over_limit(self.usage, self.config.settings.claude.limits)

    def _halt(self, reason):
        with self._lock:
            self.halt_reason = self.halt_reason or reason
        raise CycleStop(reason)

    # The dashboard's snapshot.

    def _snapshot(self, supervisor, reason=None, next_wake=None):
        usage = self.usage and {"session": self.usage.session, "week": self.usage.week,
                                "session_resets": self.usage.session_resets, "week_resets": self.usage.week_resets}
        with self._lock:
            self.store.write_current({
                "supervisor": supervisor, "last_reason": reason, "next_wake": next_wake, "usage": usage,
                "workers": list(self.workers.values()), "waiting_for_machine": self.waiting,
                "unreachable_machines": self.unreachable,
                "queen_run": self.queen_run,
            })

    def _phase(self, task, phase):
        self.store.record("phase_changed", task=task.key, project=task.project.name, phase=phase)
        with self._lock:
            self.workers.setdefault(task.key, {
                "task": task.key, "project": task.project.name, "machine": task.project.machine,
                "started": self.now().isoformat(timespec="seconds"),
            })["phase"] = phase
        self._snapshot("awake")

    def _pick(self, task):
        with self._lock:
            first = task.key not in self.workers
        if first:
            self.store.record("task_picked", task=task.key, project=task.project.name)

    def _move(self, task, status):
        self.github.set_status(task.project.board, task.url, status)
        task.status = status

    def _finish(self, task, status, outcome):
        self._move(task, status)
        self.store.record("task_finished", task=task.key, project=task.project.name, outcome=outcome)

    def _reread(self, read):
        """Reads GitHub into the copy after an agent changed it. A refusal that passes by itself ends only this
        cycle's turn of the task: it stays where it is and resumes in the next."""
        try:
            return read()
        except GitHubError as e:
            if transient(e):
                raise CycleStop(f"GitHub could not be read: {e}") from e
            raise

    def _head_after_push(self, task, pr):
        """The PR's head after a push, read once the PR shows its branch's last commit. A PR that still shows an
        older one ends this cycle's turn of the task: judged now, a push would count as none."""
        pushed = self._reread(lambda: self.github.refresh_pushed_pr(task.repo, pr["number"]))
        if self.github.pr(task.repo, pr["number"])["headRefOid"] != pushed:
            raise CycleStop(f"{task.key}: GitHub does not show the push to the PR yet")
        return pushed

    def _decide(self, task, decision, reason):
        self.store.record("decision", task=task.key, project=task.project.name, decision=decision, reason=reason)

    def _base(self, project, repo):
        """The branch of `repo` the project's work starts from, its PRs target and Kandu merges into."""
        return base_branch(self.github, project, repo)

    # Running agents.

    def _model(self, role, project=None):
        return self.config.model_for(project, role)

    def _rules(self, role):
        return getattr(self.config.settings.permissions, role)

    def _remote(self, project):
        """True when the project's agents run away from the brain, where `bin/kandu-show` is not."""
        return project.machine != self.config.settings.brain.machine

    def _copy(self, project):
        """None on the brain, where agents run `bin/kandu-show`; away from it, how Kandu reads an item from its copy
        into the prompt."""
        if not self._remote(project):
            return None
        return lambda repo, number: show.render(self.github.item(repo, number))

    def _note_reference(self, task, notes):
        """Says on the task why a reference was not, or not wholly, copied. Each note once, in no run's words: the
        issue may be on a public repo, so no repo or path is named."""
        for note in notes:
            if self.store.last_event("reference_note", task=task.key, project=task.project.name, note=note):
                continue
            self.store.record("reference_note", task=task.key, project=task.project.name, note=note)
            try:
                self.github.comment_issue(task.repo, task.number, f"Kandu could not give the run a copy of the "
                                          f"task's reference. {note}")
            except GitHubError as e:
                self._decide(task, "internal_error", f"could not post a reference note: {e}")

    @contextlib.contextmanager
    def _reference(self, task):
        """Gives a run the read-only copy of the task's `Reference`: yields (folder, prompt text), or (None, None).

        Only with `reference_copy` on. A reference it cannot copy never stops the run: it leaves a note on the task
        and the run goes on without it. The folder is removed when the run ends, however it ends.
        """
        folder = None
        try:
            if self.config.settings.supervisor.reference_copy:
                folder, text = self._copy_reference(task)
            else:
                text = None
            yield (folder, text) if text else (None, None)
        finally:
            if folder is not None:
                self.runner.remove_reference_folder(task.project, folder)

    def _copy_reference(self, task):
        body = self.github.issue_text(task.repo, task.number)["body"]
        wanted, notes = reference.plan(task.repo, body, self.config.projects, self.github.repo_is_public)
        folder, copied = None, []
        try:
            for source, ref, number in wanted:
                if folder is None:
                    folder = self.runner.make_reference_folder(task.project)
                try:
                    done, missing = self.runner.copy_reference(source, ref.repo, ref.paths, task.project, folder)
                except Unreachable:
                    notes.append(f"Reference {number}: {reference.UNREACHABLE}.")
                    continue
                except ReferenceTooBig:
                    notes.append(f"Reference {number}: {reference.TOO_BIG}.")
                    continue
                except RunnerError:
                    notes.append(f"Reference {number}: {reference.UNREADABLE}.")
                    continue
                if missing:
                    notes.append(f"Reference {number}: {reference.MISSING_PATH}.")
                if done:
                    copied.append((ref.repo, done))
        except BaseException:
            if folder is not None:
                self.runner.remove_reference_folder(task.project, folder)
            raise
        if folder is not None:
            self.store.record("reference_copied", task=task.key, project=task.project.name,
                              repos=[repo for repo, _ in copied], paths=sum(len(p) for _, p in copied))
        self._note_reference(task, notes)
        if not copied:
            if folder is not None:
                self.runner.remove_reference_folder(task.project, folder)
            return None, None
        return folder, reference.prompt_section(folder, copied)

    def _spec(self, task):
        """The Solution, Decisions and Reference of the spec the task names, read from Kandu's copy alone, for its
        agents' prompts; None when the task names no spec. A named spec the copy cannot give is recorded and the
        prompt goes without it."""
        body = (self.github.copied_issue_text(task.repo, task.number) or {}).get("body", "")
        key = tasks.named_spec(body, task.repo)
        if key is None:
            return None
        repo, _, number = key.partition("#")
        spec = self.github.copied_issue_text(repo, int(number))
        text = tasks.spec_for_agents(spec["body"]) if spec else ""
        if not text:
            self._decide(task, "spec_unreadable", f"the spec {key} is not in Kandu's copy or holds none of "
                         f"{', '.join(tasks.SPEC_FOR_AGENTS)}; the agent works without it")
            return None
        return text

    def _agent(self, task, role, cwd, prompt, tools, run_id, tokens=None, schema=None, resume=None):
        if self._stopping():
            raise CycleStop(STOPPED_BY_USER)  # not kept for the cycle: a resume lets the rest go on
        reason = self._limit()
        if reason is not None:
            self._halt(reason)
        if self._is_paused(task.project):
            raise CycleStop
        allowed, denied = tools
        with self._lock:
            self.workers[task.key]["run_id"] = run_id
        self._snapshot("awake")
        stop_one = self.store.worker_stop_file(run_id)
        result = self.runner.run_agent(task.project.machine, cwd, prompt, allowed, denied, run_id,
                                       lambda: self._hard_stop() or stop_one.exists(), self._model(role, task.project), tokens,
                                       schema, resume, holder=task.key)
        if result.ended_earlier:
            # An earlier run of this task was still going on its machine, as one a dead cycle leaves behind.
            self._decide(task, "earlier_run_ended", f"before {role} run {run_id} on {task.project.machine}: "
                         + ", ".join(result.ended_earlier))
        self.store.record("run_finished", task=task.key, project=task.project.name, run_id=run_id, role=role,
                          models=result.models, duration_ms=result.duration_ms, is_error=result.is_error)
        if stop_one.exists():
            stop_one.unlink()
            raise StoppedByUser(f"{role} run {run_id} was stopped by the user")
        if result.stopped:
            self._halt(CUT_OFF)
        if result.is_error:
            reason = self._limit()  # A run cut off by a limit is not the task's fault.
            if reason is not None:
                self._halt(reason)
            raise KanduTrouble(f"{role} run {run_id} ended with an error")
        return result

    def _reply(self, task, role, cwd, tools, result, read, tokens=None, schema=None):
        """Returns `read(text)`. A reply it cannot read is asked for once more, in the same session: the work is
        done, and only its last message is missing."""
        try:
            return read(result.text)
        except BROKEN_REPLY as e:
            if not result.session_id:
                raise
            self._decide(task, "reply_reasked", f"{role}: {e}")
            again = self._agent(task, role, cwd, agents.reask_prompt(e), tools, new_run_id(self.now()), tokens,
                                schema, resume=result.session_id)
            return read(again.text)

    def _worktree(self, task, base):
        """The task's worktree, with whatever an earlier run left unfinished there cancelled and a branch that drifted
        from GitHub's settled."""
        try:
            return self.runner.prepare_worktree(task.project, task.repo, task.branch, base,
                                                cleaned=lambda what: self._decide(task, "worktree_cleaned", what),
                                                settled=lambda how: self._decide(task, "worktree_settled", how))
        except LocalWork as e:
            raise TaskFailed(str(e)) from e  # a later try would only meet the same commits again

    def _work(self, task, findings=None, pr=None, order=None, cwd=None):
        """Runs a worker on the task's branch. `cwd` is a worktree Kandu already prepared, such as an open merge."""
        run_id = new_run_id(self.now())
        if cwd is None:
            cwd = self._worktree(task, self._base(task.project, task.repo))
        note = queen.note_for(queen.all_events(self.store), task.key)
        blockers = open_blockers(self.github, task.repo, self.github.issue_text(task.repo, task.number))
        project = task.project
        tokens = (project.secrets_from, list(project.task_tokens)) if task.tokens else None
        with self._reference(task) as (folder, copied):
            prompt = agents.worker_prompt(task.project, task.repo, task.number, task.branch, run_id,
                                          findings=findings, pr=pr, local_dir=self.local_dir,
                                          queen_note=note, tokens=task.tokens, open_blockers=blockers, order=order,
                                          copy=self._copy(task.project), reference=copied,
                                          spec=self._spec(task))
            tools = agents.worker_tools(project, task.branch, self._rules("worker"), self._remote(project), folder)
            result = self._agent(task, "worker", cwd, prompt, tools, run_id, tokens, agents.WORKER_SCHEMA)
            read = agents.parse_worker_reply if pr is not None else agents.parse_first_reply
            return self._reply(task, "worker", cwd, tools, result, read, tokens, agents.WORKER_SCHEMA)

    def _open_pr(self, task, reply):
        """Kandu opens the PR, from the task's own branch into the project's base branch, with the worker's title and
        description. A PR an earlier run left open on the branch gets the new description instead."""
        body = self._with_test_output(task, agents.closing_body(reply["pr_body"], task.number))
        pr = self.github.find_pr(task.repo, task.number)
        if pr is not None and pr["state"] == "OPEN":
            self.github.edit_pr_body(task.repo, pr["number"], body)
            return self.github.pr(task.repo, pr["number"])
        base = self._base(task.project, task.repo)
        try:
            number = self.github.create_pr(task.repo, task.branch, base, reply["pr_title"], body)
        except GitHubError as e:
            # The answer may be lost while the PR was made: look before calling it a failure.
            self._reread(lambda: self.github.refresh_repo(task.repo))
            pr = self.github.find_pr(task.repo, task.number)
            if pr is not None and pr["state"] == "OPEN":
                return pr
            if transient(e):  # the pushed reply is saved: a later cycle opens the PR, and no try is spent
                self._decide(task, "pr_open_waits", f"GitHub refused the PR for a moment: {e}")
                raise CycleStop(f"the PR could not be opened from {task.branch} yet: {e}") from e
            raise TaskFailed(f"the PR could not be opened from {task.branch}: {e}") from e
        self._decide(task, "pr_opened", f"#{number} from {task.branch}")
        return self.github.pr(task.repo, number)

    def _edit_pr_body(self, task, pr, reply):
        """A round may replace the PR's description; Kandu sends it. A refusal is recorded and does not undo the
        round: its commits are pushed, and it is judged by them."""
        if reply is not None and reply.get("status") == "pushed" and reply.get("pr_body"):
            try:
                body = self._with_test_output(task, agents.closing_body(reply["pr_body"], task.number))
                self.github.edit_pr_body(task.repo, pr["number"], body)
            except GitHubError as e:
                self._decide(task, "pr_body_not_edited", str(e))

    def _with_test_output(self, task, body):
        """With the project's `pr_test_output`, the PR body's section of that name holds the tail of Kandu's own run of
        the project's `test` on the pushed branch, in place of what the worker wrote there (DESIGN §4). A team repo's
        gate may close a PR without it. On a public repo a line with a private word is left out."""
        heading = task.project.pr_test_output
        if not heading:
            return body
        log = new_run_id(self.now())
        try:
            with self._test_locks[task.project.name]:  # one test run at a time per project, as the merge gate's
                cwd = self.runner.prepare_review(task.project, task.repo, task.branch, f"origin/{task.branch}")
                code = self.runner.run_test(task.project, cwd, log)
        except RunnerError as e:
            self._decide(task, "pr_test_output_not_run", str(e))
            return tasks.with_section(body, heading, f"Kandu could not run `{task.project.test}` on this branch: {e}")
        lines = self.runner.log_tail(log, PR_TEST_OUTPUT_LINES).splitlines()
        if self.github.repo_is_public(task.repo):
            words = [w.lower() for w in tasks.private_words(self.local_dir)]
            lines = [l for l in lines if not any(w in l.lower() for w in words)]
        tail = "\n".join(l for l in lines if not l.lstrip().startswith("```")) or "(no output)"
        result = "passed" if code == 0 else f"failed with exit code {code}"
        self.store.record("pr_test_output", task=task.key, project=task.project.name, code=code)
        return tasks.with_section(body, heading, f"`{task.project.test}` on this branch {result}; the last lines:\n\n"
                                               f"```\n{tail}\n```")

    def _pushed_unopened(self, task):
        """The reply of a first run whose worker pushed but whose PR was not opened yet, or None. A cycle that GitHub
        cut short after the push keeps it, so the next opens the PR with no new worker."""
        found = None
        for event in queen.all_events(self.store):
            if event.get("task") != task.key:
                continue
            if event["type"] == "worker_pushed":
                found = event
            elif event["type"] == "task_finished" or (event["type"] == "decision" and event["decision"] in OPENED):
                found = None
        if found is None or found["branch"] != task.branch:
            return None
        if not self.runner.branch_pushed(task.project, task.repo, task.branch):
            return None
        return {"status": "pushed", "pr_title": found["pr_title"], "pr_body": found["pr_body"]}

    def _implement(self, task):
        """First run. Returns the new PR, or None if the worker gave the task back."""
        self._pick(task)
        self._move(task, IN_PROGRESS)
        saved = self._pushed_unopened(task)
        if saved is not None:
            self._decide(task, "pushed_reply_reused", f"{task.branch} is pushed: its PR opens with no new run")
            self._reread(lambda: self.github.refresh_repo(task.repo))
            return self._open_pr(task, saved)
        self._phase(task, "implement")
        try:
            reply = self._work(task)
        except agents.ReplyError as e:
            # The work may be done even if its last message is not: a PR or a pushed branch is its answer.
            self._reread(lambda: self.github.refresh_repo(task.repo))
            pr = self.github.find_pr(task.repo, task.number)
            if pr is not None and pr["state"] == "OPEN":
                self._decide(task, "reply_unread_pr_found", str(e))
                return pr
            if not self.runner.branch_pushed(task.project, task.repo, task.branch):
                raise KanduTrouble(str(e)) from e
            self._decide(task, "reply_unread_branch_pushed", str(e))
            title = self.github.issue_text(task.repo, task.number)["title"]
            reply = {"status": "pushed", "pr_title": title, "pr_body": UNREAD_PR_BODY}
        if reply["status"] == "needs_info":
            self.github.comment_issue(task.repo, task.number, reply["question"])
            self._finish(task, NEEDS_INFO, "needs_info")
            return None
        if reply["status"] == "blocked":
            self.github.comment_issue(task.repo, task.number, reply["reason"])
            self._finish(task, BLOCKED, "blocked")
            return None
        self.store.record("worker_pushed", task=task.key, project=task.project.name, branch=task.branch,
                          pr_title=reply["pr_title"], pr_body=reply["pr_body"])  # kept if GitHub cuts the cycle short
        self._reread(lambda: self.github.refresh_repo(task.repo))  # the worker pushed its branch
        return self._open_pr(task, reply)

    def _warn_if_oversized(self, task, pr):
        """A warning only: it shows where the scale and the splitter's estimates are wrong."""
        sized = self.store.last_event("task_sized", task=task.key)
        files = pr.get("changedFiles", 0)
        if sized is not None and not sizing.fits(sized["size"], 0, files):
            self._decide(task, "oversized", f"{files} files changed, sized {sized['size']}")

    def _family_repos(self, project):
        return [repo for p in self.config.projects if p.family == project.family for repo in p.repos]

    def _review(self, task, pr, reviews):
        """One review round on the PR's head. Posts the result on the PR."""
        self._pick(task)
        if task.status != IN_REVIEW:
            self._move(task, IN_REVIEW)
        self.github.mark_pr(task.repo, pr)
        if task.breaking:  # before any merge, so the merged PR still says it was breaking
            self.github.mark_breaking(task.repo, pr)
        head = pr["headRefOid"]
        round_number = len(reviews) + 1
        self._phase(task, f"review round {round_number}")
        to_fix = self._to_fix(task, reviews[-1]) if reviews else []
        language = self.config.settings.language
        run_id = new_run_id(self.now())
        cwd = self.runner.prepare_review(task.project, task.repo, task.branch, head)
        stale = []

        def read(text):
            report = review.parse_report(text)
            stale[:] = review.validate_report(report, to_fix, second=bool(to_fix))
            return report
        with self._reference(task) as (folder, copied):
            if to_fix:
                prompt = agents.second_review_prompt(task.repo, task.number, pr["number"], head, language, to_fix,
                                                     self._base(task.project, task.repo), self._copy(task.project),
                                                     reference=copied, spec=self._spec(task))
            else:
                criteria = sizing.criteria_list(self.github.issue_text(task.repo, task.number)["body"])
                first_n = 1 + max((f["n"] for r in reviews for f in r["open"]), default=0)
                companions = self.github.companions(task.repo, pr, self._family_repos(task.project))
                prompt = agents.reviewer_prompt(task.project, task.repo, task.number, pr["number"], head, language,
                                                companions, criteria, first_n, self._base(task.project, task.repo),
                                                local_dir=self.local_dir, copy=self._copy(task.project),
                                                reference=copied, spec=self._spec(task))
            tools = agents.reviewer_tools(task.project, self._rules("reviewer"), self._remote(task.project), folder)
            result = self._agent(task, "reviewer", cwd, prompt, tools, run_id, schema=review.REPORT_SCHEMA)
            try:
                report = self._reply(task, "reviewer", cwd, tools, result, read, schema=review.REPORT_SCHEMA)
                dropped = (review.carry_over(report, reviews[-1], to_fix) if to_fix else
                           review.hold_to_scope(report, criteria, [f["path"] for f in pr["files"]]))
            except review.ReportError as e:
                raise KanduTrouble(f"reviewer run {run_id}: {e}") from e
        if report["head_sha"] != head:
            self._decide(task, "review_voided", "the head moved during the review")
            return
        if stale:
            self._decide(task, "stale_earlier_ignored",
                         f"{len(stale)} results for findings no longer open: {[e.get('n') if isinstance(e, dict) else e for e in stale]}")
        if dropped:
            self._decide(task, "findings_dropped", f"{dropped} outside what this review may report")
        findings = review.open_after(report, to_fix)
        self.github.comment_pr(task.repo, pr["number"],
                               review.comment_for(report, round_number, findings, second=bool(to_fix)))
        self.store.record("review_finished", task=task.key, project=task.project.name, run_id=run_id,
                          round=round_number, head_sha=head, findings=len(findings),
                          blockers=len(review.blockers(findings)), door=report["door"],
                          criteria_met=all(c["result"] == "met" for c in report["criteria"]),
                          wait_for_user=report["wait_for_user"], user_brief=report["user_brief"])

    def _sized_notes(self, task, pr, last):
        """The splitter sizes a review's notes once. If it cannot, they must still not hold the PR."""
        head = pr["headRefOid"]
        sized = self.store.last_event("notes_sized", task=task.key, head_sha=head)
        notes = [f for f in last["open"] if not f["blocker"]]
        if sized is not None or not notes:
            return sized["notes"] if sized else []
        self._phase(task, "sizing notes")
        run_id = new_run_id(self.now())
        try:
            cwd = self.runner.prepare_review(task.project, task.repo, task.branch, head)
            prompt = agents.notes_prompt(task.project, task.repo, task.number, pr["number"], notes, self.local_dir,
                                         self._copy(task.project))
            tools = agents.role_tools(self._rules("splitter"), self._remote(task.project))
            result = self._agent(task, "splitter", cwd, prompt, tools, run_id, schema=agents.NOTES_SCHEMA)

            def read(text):
                report = split.parse_report(text)
                split.validate_notes(report, notes)
                return report
            sized = self._reply(task, "splitter", cwd, tools, result, read, schema=agents.NOTES_SCHEMA)["notes"]
        except StoppedByUser:
            raise
        except (TaskFailed, split.ReportError, GitHubError, RunnerError) as e:
            self._decide(task, "notes_not_sized", str(e))
            sized = split.unsized_notes(notes)
        self.store.record("notes_sized", task=task.key, project=task.project.name, run_id=run_id, head_sha=head,
                          notes=sized)
        return sized

    def _retried_after(self, task, head):
        """True if the queen sent the task back after this head's review: it gets one more fix round."""
        seen = self.store.last_event("review_finished", task=task.key, head_sha=head)
        retry = self.store.last_event("queen_decided", task=task.key, action="retry")
        return seen is not None and retry is not None and retry["ts"] > seen["ts"]

    def _to_fix(self, task, last):
        """What a review leaves for the worker: its blockers, and the notes that stay in the PR."""
        if last.get("second"):
            return review.blockers(last["open"])
        sized = self.store.last_event("notes_sized", task=task.key, head_sha=last["head_sha"])
        here = {note["n"] for note in sized["notes"] if split.stays_in_pr(note)} if sized else set()
        return [f for f in last["open"] if f["blocker"] or f["n"] in here]

    def _to_debt(self, task, notes):
        """Reports notes to the queen, who parks them. Reported once, so a restart repeats nothing."""
        for note in notes:
            if self.store.last_event("debt_reported", task=task.key, finding=note["n"]) is None:
                self.store.record("debt_reported", task=task.key, project=task.project.name, finding=note["n"],
                                  title=note["title"], body=note["body"], size=note["size"], reason=note["reason"])

    def _settle(self, task, pr, findings, reason):
        """No more rounds: a blocker stops the task, a note left undone goes to the queen and holds nothing."""
        sized = self.store.last_event("notes_sized", task=task.key)
        left = {f["n"] for f in findings if not f["blocker"]}
        self._to_debt(task, [note for note in (sized["notes"] if sized else []) if note["n"] in left])
        if not review.blockers(findings):
            return "approved_with_notes"
        self.github.comment_pr(task.repo, pr["number"], f"Kandu stopped here: {reason}. A blocker is still open.")
        self._finish(task, BLOCKED, "blocked")
        return "blocked"

    def _outcome(self, task, pr, reviews):
        """Returns where a reviewed head leaves the task, or None if a fix round should run."""
        account = self.config.settings.github.account
        last = reviews[-1]
        if not last["open"]:
            return "approved"
        if last.get("second"):
            if not self._retried_after(task, last["head_sha"]):
                return self._settle(task, pr, last["open"], "the second review found it not done")
        else:
            self._to_debt(task, [n for n in self._sized_notes(task, pr, last) if not split.stays_in_pr(n)])
        to_fix = self._to_fix(task, last)
        if not to_fix:
            return "approved_with_notes"
        if review.fix_tried(pr, account, pr["headRefOid"]) and not self._resumed_since_no_commit(task):
            return self._settle(task, pr, to_fix, "the fix round changed no code")
        return None

    def _resumed_since_no_commit(self, task):
        """True if the user sent the task back to work after its last round that pushed nothing. Their «Back to
        work» buys one more fix round on the same head, since what held the round back may be gone (a blocker merged
        since). That round, if it pushes nothing too, is recorded again and settles the task as before."""
        for day in reversed(self.store.days()):
            for event in reversed(self.store.events(day)):
                if event.get("task") != task.key:
                    continue
                if event["type"] == "user_resumed":
                    return True
                if event["type"] == "decision" and event.get("decision") == "round_no_commit":
                    return False
        return False

    def _fix(self, task, pr, reviews):
        """One fix round by a fresh worker on the same branch."""
        self._round(task, pr, f"fix round {len(reviews)}", self._to_fix(task, reviews[-1]))

    def _round(self, task, pr, phase, findings, cwd=None, mark=review.fix_comment):
        """A fresh worker on the same branch. A round that adds no commit is marked on the PR with `mark`, so it is
        not run again on that head. Returns the worker's reply, or None if it could not be read."""
        head = pr["headRefOid"]
        self._pick(task)
        self._phase(task, phase)
        reply = None
        try:
            reply = self._work(task, findings=findings, pr=pr["number"], cwd=cwd)
        except agents.ReplyError:
            pass  # A round is judged by its commits, not by its last message.
        self._edit_pr_body(task, pr, reply)
        if self._head_after_push(task, pr) == head:
            self.github.comment_pr(task.repo, pr["number"], mark(head))
            # For the user's eyes: the card says the task will not move on by itself, and why.
            why = (reply or {}).get("reason") or (reply or {}).get("question") or "it pushed no new commit"
            self._decide(task, "round_no_commit", f"{phase}: {why}"[:300])
        return reply

    def _conflicts(self, pr, outcome):
        """A reviewed PR that only its conflicts hold back, and that no update round has tried on this head.
        Only an update round's mark holds it back; a fix round's, or a mark from before the two had their own, does
        not."""
        return (outcome in MERGEABLE and pr["mergeable"] == "CONFLICTING"
                and not review.update_tried(pr, self.config.settings.github.account, pr["headRefOid"]))

    def _main_to_bring(self, task, pr, outcome):
        """True when this reviewed PR waited on a red `main` that passes again and `main` is not yet brought into it.
        The update is the one a conflict gets; the new head is reviewed and merged as usual."""
        if outcome not in MERGEABLE or not self.store.project_setting(task.project, "watch_main"):
            return False
        pending = mainred.waiting_for_update(self.store, task.project, task.repo)
        return pending is not None and task.key in pending["tasks"]

    def _update(self, task, pr):
        """Brings the project's base branch into the PR. Kandu merges; a worker only resolves the files in conflict.
        The new head is reviewed again before any merge. Returns False if the task stopped here."""
        pending = mainred.waiting_for_update(self.store, task.project, task.repo)
        if pending is not None and task.key in pending["tasks"] and self.store.project_setting(task.project, "watch_main"):
            self.store.record("main_updated", project=task.project.name, repo=task.repo, commit=pending["commit"],
                              task=task.key)  # once: a conflict the round leaves is not tried again for this green
        base = self._base(task.project, task.repo)
        run_id = new_run_id(self.now())
        cwd = self._worktree(task, base)
        message = f"Merge {base} into {task.branch}\n\nKandu-Run: {run_id}"
        merged, conflicts = self.runner.merge_base(task.project, task.repo, cwd, task.branch, base, message,
                                                   kept=lambda note: self._decide(task, "unpushed_kept", note))
        if not conflicts:
            self._pick(task)
            self._phase(task, "update")
            self.runner.push_branch(task.project, cwd, task.branch)
            self._decide(task, "base_merged", f"{base} merged into the PR with no conflict, run {run_id}")
            if self._head_after_push(task, pr) == pr["headRefOid"]:
                # Nothing new to push, yet GitHub sees a conflict: marked, so this head is not tried again.
                self.github.comment_pr(task.repo, pr["number"], review.update_comment(pr["headRefOid"]))
            return True
        conflict = {"n": 0, "blocker": True, "path": "", "line": None,
                    "body": self._resolve_order(task, base, conflicts)}
        try:
            reply = self._round(task, pr, "update", [conflict], cwd=cwd, mark=review.update_comment)
        except BaseException:
            # Even a round cut short may have pushed: markers must not reach a review.
            if self._merge_left_markers(task, pr, cwd, conflicts):
                return False
            raise
        if self._merge_left_markers(task, pr, cwd, conflicts):
            return False
        if not self.runner.has_commit(task.project, task.repo, cwd, task.branch, merged):
            why = reply["reason"] if reply is not None and reply.get("status") == "blocked" else \
                "The merge was not finished and pushed."
            self.github.comment_pr(task.repo, pr["number"], f"Kandu stopped here: the update round could not "
                                                            f"bring `{base}` in. {why}")
            self._finish(task, BLOCKED, "blocked")
            return False
        return True

    @staticmethod
    def _resolve_order(task, base, conflicts, then=None):
        """What a worker does with a merge of `base` Kandu left open at `conflicts`. With `then`, the queen's order,
        it does that order too, in the same round; without it, it changes nothing else."""
        files = ", ".join(f"`{path}`" for path in conflicts)
        order = (f"Kandu began merging `{base}` into your branch and stopped at conflicts in {files}. Resolve each "
                 "conflict so that the changes of both sides stay, `git add` those files, and finish the merge with "
                 f"`git commit`, with the message `Merge {base} into {task.branch}` and your `Kandu-Run:` line. Do "
                 "not start another merge or a rebase.")
        if then is None:
            return order + " Run the tests and push. Change nothing else."
        return order + f" Then, in the same round, do this, run the tests and push:\n\n{then}"

    def _update_waits(self, task, pr):
        """A conflicting PR gets no update round while a blocking task is open: it cannot merge before that task,
        whose merge moves the base again. The wait is recorded as the merge gate records one held only by its
        blockers, so later cycles skip the task until its head or its open blockers change. The user's yes, a
        one-way door or a question never hold an update: a yes holds for one head, and an update after it would void
        it. Returns True if the update waits."""
        blockers = self._open_blockers(task)
        if not blockers:
            return False
        self._merge_held(task, pr, self._blocker_reasons(blockers), blockers)
        return True

    @staticmethod
    def _blocker_reasons(blockers):
        return [WAITS_FOR_BLOCKER.format(key) for key in blockers]

    def _merge_held(self, task, pr, reasons, blockers):
        """Records why the merge waits. `blockers` is given when only they hold it, so `_held_as_before` skips it."""
        self._decide(task, "merge_waits", "; ".join(reasons))
        if blockers:
            self.store.record("merge_held", task=task.key, project=task.project.name, head_sha=pr["headRefOid"],
                              blockers=blockers)
        with self._lock:
            self.held[task.key] = pr["headRefOid"]

    def _wrong_base(self, task, pr):
        """Holds a PR whose base is not the project's base branch, for the user, whatever auto-merge or a yes says:
        Kandu merges into no other branch and brings no other into it. Returns True when it held the PR."""
        reasons = merge.base_waits(pr, self._base(task.project, task.repo))
        if reasons:
            self._merge_held(task, pr, reasons, None)
        return bool(reasons)

    def _merge_left_markers(self, task, pr, cwd, conflicts):
        """Blocks the task if the pushed branch still holds a conflict marker in a file the merge stopped at."""
        left = self.runner.conflict_markers(task.project, task.repo, cwd, task.branch, conflicts)
        if not left:
            return False
        names = ", ".join(f"`{path}`" for path in left)
        self.github.comment_pr(task.repo, pr["number"], f"Kandu stopped here: the update round left conflict "
                                                        f"markers in {names}.")
        self._finish(task, BLOCKED, "blocked")
        return True

    # What the queen sends on.

    def _step_done(self, task, step, result, summary=None, head_sha=None, run_id=None):
        self.store.record("queen_step_done", task=task.key, project=task.project.name, action=step["action"],
                          decided=step["ts"], result=result, summary=summary, head_sha=head_sha, run_id=run_id)

    def _rework(self, task, pr, step):
        """A fresh worker on the PR's branch does what she asks. The new head is then reviewed as usual.

        A PR that conflicts with its base gets the base merged in first, as an update round would: a worker never
        runs `git merge`, so her order could not bring the base in, and an update round that already stopped on
        this head is not run again. A clean merge Kandu pushes; one with conflicts is left open, and the worker
        resolves it and does her order in the same round. Returns False if the task stopped here."""
        if pr is None or pr["state"] != "OPEN":
            self._step_done(task, step, result="no_open_pr")
            return True
        head = pr["headRefOid"]
        task.branch = pr["headRefName"]
        self._pick(task)
        self._move(task, IN_PROGRESS)
        self._phase(task, "queen's rework")
        order, cwd, conflicts = step["note"], None, []
        base = self._base(task.project, task.repo)
        if pr.get("mergeable") == "CONFLICTING" and not merge.base_waits(pr, base):  # never into another branch
            cwd = self._worktree(task, base)
            message = f"Merge {base} into {task.branch}\n\nKandu-Run: {new_run_id(self.now())}"
            _, conflicts = self.runner.merge_base(task.project, task.repo, cwd, task.branch, base, message,
                                                  kept=lambda note: self._decide(task, "unpushed_kept", note))
            if conflicts:
                order = self._resolve_order(task, base, conflicts, then=order)
            else:
                self.runner.push_branch(task.project, cwd, task.branch)
                self._decide(task, "base_merged", f"{base} merged into the PR with no conflict, before her rework")
        try:
            self._edit_pr_body(task, pr, self._work(task, pr=pr["number"], order=order, cwd=cwd))
        except agents.ReplyError:
            pass  # judged by its commits, as a fix round is
        except BaseException:
            # Even a round cut short may have pushed: markers must not reach a review.
            if conflicts and self._merge_left_markers(task, pr, cwd, conflicts):
                self._step_done(task, step, result="markers_left")
                return False
            raise
        if conflicts and self._merge_left_markers(task, pr, cwd, conflicts):
            self._step_done(task, step, result="markers_left")
            return False
        self._reread(lambda: self.github.refresh_pr(task.repo, pr["number"]))
        after = self.github.pr(task.repo, pr["number"])["headRefOid"]
        self._step_done(task, step, result="pushed" if after != head else "no_commit", head_sha=after)
        return True

    def _verify(self, task, pr, step):
        """A checker finds out what she asked, on the PR's head or the project's base branch. She reads it next look."""
        self._pick(task)
        self._phase(task, "queen's check")
        run_id = new_run_id(self.now())
        head = pr["headRefOid"] if pr is not None and pr["state"] == "OPEN" else None
        base = self._base(task.project, task.repo)
        if head is not None:
            cwd = self.runner.prepare_review(task.project, task.repo, pr["headRefName"], head)
        else:
            cwd = self.runner.prepare_review(task.project, task.repo, f"kandu/check-{task.number}", f"origin/{base}")
        prompt = agents.check_prompt(task.project, task.repo, task.number, pr["number"] if head else None, head,
                                     step["note"], base, self.local_dir, self._copy(task.project))
        tools = agents.reviewer_tools(task.project, self._rules("reviewer"), self._remote(task.project))
        result = self._agent(task, "checker", cwd, prompt, tools, run_id, schema=agents.CHECK_SCHEMA)
        try:
            reply = self._reply(task, "checker", cwd, tools, result, agents.parse_check, schema=agents.CHECK_SCHEMA)
        except agents.ReplyError as e:
            raise KanduTrouble(f"checker run {run_id}: {e}") from e
        self._step_done(task, step, result=reply["result"], summary=reply["summary"], head_sha=head, run_id=run_id)

    # Merge, deploy and live check.

    def _unreviewed_companions(self, task, pr):
        names = []
        for repo, number in self.github.companions(task.repo, pr, self._family_repos(task.project)):
            head = self.github.pr(repo, number)["headRefOid"]
            seen = self.store.last_event("review_finished", head_sha=head)
            if seen is None or seen["blockers"]:
                names.append(f"{repo}#{number}")
        return names

    def _approved(self, task, pr):
        """Only the local log counts, and only for this head: a new commit voids the user's yes as well."""
        return self.store.last_event("user_approved", task=task.key, head_sha=pr["headRefOid"]) is not None

    def _answered(self, task, pr):
        """The user's yes for this head settles the review's questions and the door. So does the queen's, on a
        project whose switch lets her yes answer one-way merges."""
        return self._approved(task, pr) or (
            self.store.project_setting(task.project, "queen_answers_one_way")
            and self.store.last_event("queen_approved", task=task.key, head_sha=pr["headRefOid"]) is not None)

    def _merge_waits(self, task, pr):
        """Returns why Kandu may not merge. The tests run last: they are the slow check."""
        head = pr["headRefOid"]
        reviewed = self.store.last_event("review_finished", task=task.key, head_sha=head)
        threads = self.github.review_threads(task.repo, pr["number"])
        asked = [q["question"] for q in questions.open_questions(self.store) if q["task"] == task.key]
        reasons = merge.merge_waits(task.project, pr, reviewed, threads, self._unreviewed_companions(task, pr),
                                    self._answered(task, pr), asked)
        if waits.CI_FAILING in reasons:  # each failing check by name, with its link, so the user sees where
            reasons += waits.check_reasons(pr.get("statusCheckRollup"))
        reasons += [WAITS_FOR_BLOCKER.format(key) for key in self._open_blockers(task)]
        if self._main_is_red(task):
            reasons.append(mainred.RED_REASON)
        if reasons:
            return reasons
        if self._tests_wait(task.key, head):
            return [merge.TESTS_WAIT]  # the tests are not run again on this head
        # One merge test at a time per project: slow tests time out when they share the machine. A PR whose turn
        # has not come waits for a later look, and this is no failed run.
        lock = self._test_locks[task.project.name]
        if not lock.acquire(blocking=False):
            return [WAITS_FOR_TEST_RUN]
        try:
            cwd = self.runner.prepare_review(task.project, task.repo, task.branch, head)
            code = self.runner.run_test(task.project, cwd, new_run_id(self.now()))
        finally:
            lock.release()
        if code == 0:
            self.store.record("tests_passed", task=task.key, project=task.project.name, head_sha=head)
            return []
        self.store.record("tests_failed", task=task.key, project=task.project.name, head_sha=head)
        if self.store.project_setting(task.project, "watch_main"):
            self._check_main(task.project, task.repo)
            if self._main_is_red(task):
                return [mainred.RED_REASON]
        return [merge.TESTS_WAIT if self._tests_wait(task.key, head) else merge.TESTS_FAIL]

    def _tests_wait(self, key, head):
        """True when this head's tests failed twice in the merge gate and nothing has asked for them since."""
        events = [e for day in self.store.days() for e in self.store.events(day)]
        return merge.tests_wait(events, key, head)

    # A project whose `main` is red (mainred.py).

    def _main_is_red(self, task):
        """True when the project watches `main`, it is red, and this is not the task that fixes it."""
        project = task.project
        return (bool(self.store.project_setting(project, "watch_main")) and task.key not in mainred.fix_tasks(self.store, project)
                and bool(mainred.red_events(self.store, project)))

    def _check_main(self, project, repo):
        """Tests the current `main` of `repo` until its commit is decided: at most two runs, kept per commit.
        `main` is red only when both fail, and a pass ends a red one."""
        base = self._base(project, repo)
        with self._main_lock:
            commit = self.github.branch_tip(repo, base)
            if commit is None:
                return
            kept = mainred.runs(self.store, project, repo, commit)
            while not mainred.decided(kept):
                cwd = self.runner.prepare_review(project, repo, "kandu/main-check", commit)
                log = new_run_id(self.now())
                passed = self.runner.run_test(project, cwd, log) == 0
                self.store.record("main_tested", project=project.name, repo=repo, commit=commit, run=len(kept) + 1,
                                  passed=passed, failing=[] if passed else self.runner.test_failures(log))
                kept = mainred.runs(self.store, project, repo, commit)
            current = mainred.red(self.store, project, repo)
            if mainred.is_red(kept):
                if current is None or current["commit"] != commit:
                    self._declare_red(project, repo, commit, kept, current)
                if not mainred.fix_opened(self.store, project, repo, commit):
                    self._open_fix_task(project, repo, commit, kept)  # also retries one that failed before
            elif current is not None and any(r["passed"] for r in kept):
                self.store.record("main_green", project=project.name, repo=repo, commit=commit)

    def _declare_red(self, project, repo, commit, kept, current):
        """Records the red `main`. The user hears once while it stays red: a commit that is red after another keeps
        the first one's `since`."""
        failing = list(dict.fromkeys(name for run in kept for name in run["failing"]))
        since = current["since"] if current is not None else self.now().isoformat(timespec="seconds")
        self.store.record("main_red", project=project.name, repo=repo, commit=commit, failing=failing, since=since)

    def _open_fix_task(self, project, repo, commit, kept):
        """Opens the one fix task of a red commit; a failure leaves it unopened, so the next look tries again."""
        failing = list(dict.fromkeys(name for run in kept for name in run["failing"]))
        title = f"Fix the failing tests on main of {repo.split('/')[-1]}"
        body = mainred.fix_body(repo, commit, failing, project.test)

        def created(url):
            number = url.rstrip().rsplit("/", 1)[1]
            self.store.record("main_fix_opened", project=project.name, repo=repo, commit=commit,
                              task=f"{repo}#{number}", url=url.strip())
        try:
            url = tasks.create_task(self.config, self.github, project.name, repo, title, body, self.local_dir,
                                    "High", "Bug", "Small", created=created, store=self.store)
        except TaskError as e:
            raise KanduTrouble(f"could not open the task that fixes main: {e}") from e
        self.github.set_status(project.board, url, READY)  # it starts even while `main` is red

    def _watch_main(self):
        """A red `main` is tested again once its commit changed, so a fix that landed ends the red."""
        for project in self.config.projects:
            if (not self.store.project_setting(project, "watch_main") or self._is_paused(project) or self._stopping()
                    or not self._reachable(project.machine)):
                continue
            for repo in project.repos:
                current = mainred.red(self.store, project, repo)
                try:
                    if current is not None and (self.github.branch_tip(repo, self._base(project, repo)) != current["commit"]
                                                or not mainred.fix_opened(self.store, project, repo, current["commit"])):
                        self._check_main(project, repo)
                except (GitHubError, RunnerError, KanduTrouble):
                    continue  # the next cycle looks again

    def _not_while_red(self, tasks):
        """While a project's `main` is red, no task of it starts except the one that fixes it; those at work go on."""
        red = {p.name for p in self.config.projects
               if self.store.project_setting(p, "watch_main") and mainred.red_events(self.store, p)}
        fixes = {p.name: mainred.fix_tasks(self.store, p) for p in self.config.projects if p.name in red}
        return [t for t in tasks if not (t.status == READY and t.project.name in red and t.key not in fixes[t.project.name])]

    def _holds(self, task, pr):
        """What holds a settled PR's merge whatever the user says: a failing check or an open blocker. They are
        recorded with the yes the user is asked for, so the user hears of them too."""
        failing = waits.check_reasons(pr.get("statusCheckRollup"))
        try:
            blockers = [WAITS_FOR_BLOCKER.format(key) for key in self._open_blockers(task)]
        except GitHubError:
            blockers = []  # the yes is still asked for; the merge gate checks the blockers again after it
        return ([waits.CI_FAILING] if failing else []) + failing + blockers

    def _open_blockers(self, task):
        issue = self.github.issue_text(task.repo, task.number)
        return open_blockers(self.github, task.repo, issue, lambda key: tasks.last_slice(self.store, key))

    def _held_as_before(self, task, pr):
        """True when the last merge check waited only on open blockers, and its head and those blockers are unchanged.

        Read from the copy, which the cycle's read of the repo brought up to date: a new commit or a closed blocker
        shows there, and the next drive checks the merge in full. So does a blocker the user dropped since.
        Any other reason is checked each cycle.
        """
        if pr["state"] != "OPEN":
            return False
        for day in reversed(self.store.days()):
            for event in reversed(self.store.events(day)):
                if event.get("task") != task.key:
                    continue
                if event["type"] == "blocker_dropped":
                    return False  # the user took a blocker out since: check it all again
                if event["type"] == "merge_held":
                    return event["head_sha"] == pr["headRefOid"] and event["blockers"] == self._open_blockers(task)
                if event["type"] == "decision" and event["decision"] == "merge_waits":
                    return False  # it waited on something else too
        return False

    def _merge(self, task, pr, outcome, worked):
        """Merges a settled PR if every condition holds. Returns True if it did, None if the base moved under it."""
        if outcome not in MERGEABLE or not (self.config.auto_merge_for(task.project) or self._approved(task, pr)):
            if worked:
                self._decide(task, outcome, waits.REASONS_APART.join([waits.USER_DECIDES, *self._holds(task, pr)]))
            return False
        self._pick(task)
        self._phase(task, "merge")
        with self._lock:
            retry = self.retrying.get(task.key)
        retry = retry if retry is not None and retry["head"] == pr["headRefOid"] else None
        self._reread(lambda: self.github.refresh_pr(task.repo, pr["number"]))  # checks and threads, as they are now
        fresh = self.github.pr(task.repo, pr["number"])
        if fresh["headRefOid"] != pr["headRefOid"]:
            if retry is not None:
                self._end_retry(task, "merge_retry_ended", "a new commit came in; it is reviewed first")
            return False  # a new commit came in; it is reviewed first
        pr = fresh
        if self._wrong_base(task, pr):  # its base may have changed since it was read
            return False
        # A merge tried again passed every check on this head in this cycle: only GitHub's answer is missing.
        reasons = [] if retry is not None else self._merge_waits(task, pr)
        if reasons:
            blockers = self._open_blockers(task)
            self._merge_held(task, pr, reasons, blockers if reasons == self._blocker_reasons(blockers) else None)
            return False
        # The tests take minutes, and a pause or a stop may have come in meanwhile.
        if self._is_paused(task.project) or self._stopping():
            return False
        try:
            tries = self._merge_now(task, pr, quick=retry is None)
        except GitHubError as e:
            if BASE_MOVED in str(e):
                with self._lock:
                    self.retrying.pop(task.key, None)
                self._decide(task, "base_moved", "another merge landed first; the PR is read again")
                self._reread(lambda: self.github.refresh_pr(task.repo, pr["number"]))
                return None
            if not transient(e):
                with self._lock:
                    self.retrying.pop(task.key, None)
                raise
            self._retry_later(task, pr, e)
        if retry is not None or tries > 1:
            self._end_retry(task, "merge_retried", "GitHub took the merge on a new try")
        return True

    def _merge_now(self, task, pr, quick):
        """Merges, trying again at once on a refusal that passes by itself. Returns how many tries it took.

        A try that finds the PR merged counts as merged: GitHub may land a merge it answered with an error.
        """
        waits = list(MERGE_RETRY_WAITS) if quick else []
        tries = 0
        while True:
            tries += 1
            try:
                self.github.merge_pr(task.repo, pr["number"], pr["headRefOid"])
                return tries
            except GitHubError as e:
                if ALREADY_MERGED in str(e).lower():
                    self._merged_meanwhile(task, pr)  # reads it into the copy
                    return tries
                if not transient(e):
                    raise
                if self._merged_meanwhile(task, pr):
                    return tries
                if not waits or not self._time_for(waits[0]):
                    raise
                next_try = self.now() + timedelta(seconds=waits[0])
                self._record_retry(task, pr["headRefOid"], next_try.isoformat(timespec="seconds"), e)
                self.sleep(waits.pop(0))

    def _time_for(self, seconds):
        """True when the cycle still runs `seconds` from now."""
        return (self.deadline is not None and self.now() + timedelta(seconds=seconds) < self.deadline
                and not self.stop_file.exists())

    def _merged_meanwhile(self, task, pr):
        try:
            self.github.refresh_pr(task.repo, pr["number"])
            return self.github.pr(task.repo, pr["number"])["state"] == "MERGED"
        except GitHubError:
            return False

    def _record_retry(self, task, head, next_try, error):
        self.store.record("merge_retry", task=task.key, project=task.project.name, head_sha=head, next_try=next_try,
                          reason=str(error)[:300])

    def _retry_later(self, task, pr, error):
        """Keeps a merge GitHub refused for a moment in this cycle, tried again about every minute.

        Past the cycle's end the refusal is the cycle's one internal error, and the next cycle tries again.
        """
        next_try = self.now() + timedelta(seconds=MERGE_RETRY_EVERY)
        if not self._time_for(MERGE_RETRY_EVERY):
            with self._lock:
                self.retrying.pop(task.key, None)
            self._record_retry(task, pr["headRefOid"], self.next_wake, error)
            raise error
        self._record_retry(task, pr["headRefOid"], next_try.isoformat(timespec="seconds"), error)
        with self._lock:
            self.retrying[task.key] = {"task": task, "head": pr["headRefOid"], "error": error, "next_try": next_try,
                                       "queued": False}
        raise CycleStop(f"GitHub refused the merge for a moment: {error}")

    def _retry_read_later(self, task, retry, error):
        """A merge tried again whose PR GitHub could not read for a moment: it waits for its next try, like a refusal.

        Past the cycle's end it is the cycle's one internal error, as a refused merge is.
        """
        with self._lock:
            if self.retrying.get(task.key) is not retry:
                return False
            retry["error"] = error
            retry["next_try"] = self.now() + timedelta(seconds=MERGE_RETRY_EVERY)
            retry["queued"] = False
        self._record_retry(task, retry["head"], retry["next_try"].isoformat(timespec="seconds"), error)
        return True

    def _end_retry(self, task, decision, reason):
        with self._lock:
            self.retrying.pop(task.key, None)
        self._decide(task, decision, reason)

    def _yes_after_retry(self, key, head):
        """True when the user's or the queen's yes for this head came after the task's last refused merge."""
        for day in reversed(self.store.days()):
            for event in reversed(self.store.events(day)):
                if event.get("task") != key:
                    continue
                if event["type"] == "merge_retry":
                    return False
                if event["type"] in queen.APPROVALS and event["head_sha"] == head:
                    return True
        return False

    def _due_retries(self):
        """The merges tried again now: their time came, or a yes for their head came after the refusal."""
        now = self.now()
        with self._lock:
            waiting = [r for r in self.retrying.values() if not r["queued"]]
        due = [r for r in waiting if r["next_try"] <= now or self._yes_after_retry(r["task"].key, r["head"])]
        with self._lock:
            for retry in due:
                retry["queued"] = True
        return [retry["task"] for retry in due]

    def _waits_for_retry(self):
        with self._lock:
            return any(not r["queued"] for r in self.retrying.values())

    def _next_retry_in(self):
        """Seconds until the next look at the merges tried again, or None when none waits."""
        if not self._waits_for_retry():
            return None
        with self._lock:
            first = min(r["next_try"] for r in self.retrying.values() if not r["queued"])
        return max(0, min(RETRY_POLL_SECONDS, (first - self.now()).total_seconds()))

    def _wait_for_retries(self):
        """Waits until a merge is due again. Returns its tasks, or [] when the cycle must end first."""
        while self.halt_reason is None and not self._hard_stop() and not self._stopping():
            due = self._due_retries()
            if due:
                return due
            self.sleep(self._next_retry_in())
        return []

    def _give_up_retries(self):
        """The merges still waiting when the cycle ends: each is the cycle's one internal error on its task."""
        with self._lock:
            left = [r for r in self.retrying.values() if not r["queued"]]
            self.retrying.clear()
        for retry in left:
            task = retry["task"]
            self._record_retry(task, retry["head"], self.next_wake, retry["error"])
            self._fail(task, retry["error"])

    def _deploy(self, task):
        """Returns False if the deploy failed."""
        project = task.project
        run_id = new_run_id(self.now())
        self._phase(task, "deploy")
        with self._deploy_locks[project.name]:
            base = self._base(task.project, task.repo)
            cwd = self.runner.prepare_review(project, task.repo, "kandu/deploy", f"origin/{base}")
            code = self.runner.run_command(project, cwd, project.deploy.command, run_id, secrets=True, scoped=False)
        if code != 0:
            self.github.comment_issue(task.repo, task.number, "Kandu merged this, but the deploy failed. "
                                      "Nothing was reverted. The details are in its local log.")
            self._finish(task, BLOCKED, "deploy_failed")
            return False
        self.store.record("deployed", task=task.key, project=project.name, run_id=run_id)
        return True

    def _live_check(self, task):
        """Returns False if the live software does not show what the issue asked for."""
        project = task.project
        self._phase(task, "live check")
        run_id = new_run_id(self.now())
        base = self._base(task.project, task.repo)
        cwd = self.runner.prepare_review(project, task.repo, "kandu/live-check", f"origin/{base}")
        prompt = agents.live_check_prompt(project, task.repo, task.number, self.local_dir, self._copy(project))
        tools = agents.live_check_tools(project, self._rules("live_check"), self._remote(project))
        result = self._agent(task, "live_check", cwd, prompt, tools, run_id, schema=agents.LIVE_CHECK_SCHEMA)
        try:
            reply = self._reply(task, "live_check", cwd, tools, result, agents.parse_live_check,
                                schema=agents.LIVE_CHECK_SCHEMA)
        except agents.ReplyError as e:
            raise KanduTrouble(f"live check run {run_id}: {e}") from e
        if reply["result"] == "pass":
            return True
        self.github.comment_issue(task.repo, task.number, f"Kandu's live check failed: {reply['summary']}\n\n"
                                  "Nothing was reverted.")
        self._finish(task, BLOCKED, "live_check_failed")
        return False

    def _land(self, task):
        """Takes a merged PR to `Done`. A step already done is not repeated after a restart."""
        self._pick(task)
        if task.project.deploy is not None:
            deployed = self.store.last_event("deployed", task=task.key)
            if deployed is None and not self._deploy(task):
                return
            if task.project.deploy.live_url and not self._live_check(task):
                return
        self._finish(task, DONE, "merged")
        self.merged[task.project.name] += 1
        try:
            # GitHub closes the issue from the PR's `Closes #N` only when the repo lets it, so Kandu does it too:
            # a blocker whose issue stays open holds every task after it (`open_blockers`).
            if self._is_open(task.key):
                self.github.close_issue(task.repo, task.number)
            self._close_groups()
        except GitHubError as e:  # the slice is done; the next cycle closes its parent
            self._decide(task, "group_not_closed", str(e))
        try:
            self._close_specs()
        except GitHubError as e:  # the task is done; the next cycle closes its spec
            self._decide(task, "spec_not_closed", str(e))

    def _close_groups(self):
        """The parent of a group closes with its last slice, however that slice ended."""
        events = queen.all_events(self.store)
        closed = {e["task"] for e in events if e["type"] == "group_closed"}
        for group in (e for e in events if e["type"] == "task_split" and e["task"] not in closed):
            project = next((p for p in self.config.projects if p.name == group["project"]), None)
            if project is None or self._is_paused(project) or any(self._is_open(child) for child in group["children"]):
                continue
            repo, number = group["task"].split("#")
            self.github.close_issue(repo, int(number))
            self.github.set_status(project.board, f"https://github.com/{repo}/issues/{number}", DONE)
            self.store.record("group_closed", task=group["task"], project=project.name)

    def _close_specs(self):
        """A spec issue closes when the last task that names it closes, however that task ended.

        A spec no task names yet stays open.
        """
        events = queen.all_events(self.store)
        closed = {e["task"] for e in events if e["type"] == "spec_closed"}
        tasks_of = {}
        for event in events:
            if event["type"] == "spec_named":
                tasks_of.setdefault(event["spec"], []).append(event["task"])
        for spec in (e for e in events if e["type"] == "spec_created" and e["task"] not in closed):
            project = next((p for p in self.config.projects if p.name == spec["project"]), None)
            named = tasks_of.get(spec["task"], [])
            if project is None or self._is_paused(project) or not named or any(self._is_open(t) for t in named):
                continue
            repo, number = spec["task"].split("#")
            self.github.close_issue(repo, int(number))
            self.store.record("spec_closed", task=spec["task"], project=project.name)
            closed.add(spec["task"])

    def _is_open(self, key):
        repo, number = key.split("#")
        return self.github.issue_is_open(repo, int(number))

    def _groups(self, project, items, released):
        """Yields (group, under way, a slice is at work) for each group of `project` still open.

        The board decides, not the order the groups were made in. A group none of
        whose slices started is not under way, and a slice in `Technical debt` does not count.
        `items` is the board by task, and `released` what this look already released.
        """
        events = queen.all_events(self.store)
        closed = {e["task"] for e in events if e["type"] == "group_closed"}
        status = lambda child: items[child][1].status if child in items else None
        for event in events:
            if event["type"] != "task_split" or event["project"] != project.name or event["task"] in closed:
                continue
            slices = [child for child in event["children"] if status(child) != TECHNICAL_DEBT]
            remaining = [child for child in slices if self._is_open(child)]
            unstarted = [child for child in remaining if status(child) == BACKLOG and child not in released]
            at_work = any(status(child) in (READY, *RESUMED) or child in released for child in remaining)
            yield event, bool(remaining) and unstarted != slices, at_work

    def _first_in_line(self, project, items, backlog, released=()):
        """Returns the `High` tasks of `project`: nothing else there may go before them. Groups do not wait."""
        return [f"{item.repo}#{item.number}" for owner, item in backlog
                if owner is project and item.priority == queen.HIGH and f"{item.repo}#{item.number}" not in released
                and queen.LARGE not in (item.size, self._sized_as(item))]

    def _sized_as(self, item):
        sized = split.current_size(self.store, f"{item.repo}#{item.number}")
        return sized["size"] if sized else None

    # The splitter. It sees each task once, before the queen may release it.

    def _size(self, task, item):
        self._pick(task)
        self._phase(task, "sizing")
        run_id = new_run_id(self.now())
        try:
            base = self._base(task.project, task.repo)
            cwd = self.runner.prepare_review(task.project, task.repo, f"kandu/size-{task.number}", f"origin/{base}")
            prompt = agents.splitter_prompt(task.project, task.repo, task.number, self.config.settings.language,
                                            self.local_dir, self._copy(task.project), self._spec(task))
            tools = agents.role_tools(self._rules("splitter"), self._remote(task.project))
            result = self._agent(task, "splitter", cwd, prompt, tools, run_id, schema=agents.SPLIT_SCHEMA)

            def read(text):
                report = split.parse_report(text)
                split.validate_report(report, item.body, task.project.forbidden)
                return report
            report = self._reply(task, "splitter", cwd, tools, result, read, schema=agents.SPLIT_SCHEMA)
        except StoppedByUser:
            raise
        except (TaskFailed, split.ReportError, GitHubError, RunnerError) as e:
            if isinstance(e, Unreachable) or internal(e) and self._internal_tries(task) + 1 < INTERNAL_TRIES:
                # still unsized, so the next cycle sizes it again; the reason names the error's class, as
                # tries.py tells a drop, which is no try, by it
                self._decide(task, "internal_error", "".join(traceback.format_exception_only(e)).strip())
                return
            self._decide(task, "sizing_failed", str(e))
            self.github.comment_issue(task.repo, task.number, SIZING_FAILURE_COMMENT)
            self._finish(task, NEEDS_INFO, "sizing_failed")
            return
        areas = split.check_forbidden(report, task.project.forbidden)
        if areas:
            self._hold(task, item, run_id, report, areas)
            return
        slices = report.get("slices") or []
        if slices:
            self.store.write_split(task.key, {"title": task.title, "reason": report["reason"], "slices": slices,
                                              "user_text": split.user_text(report)})
        self.github.set_field(task.project.board, task.url, "Size", report["size"])
        self.store.record("task_sized", task=task.key, project=task.project.name, run_id=run_id, size=report["size"],
                          files=report["files"], door=report["door"], criteria=sizing.criteria_in(item.body),
                          slices=len(slices))
        if slices:
            self._move(task, NEEDS_SPLIT)
            self._wait_for_split(task)

    def _hold(self, task, item, run_id, report, areas):
        """A task that must change a forbidden area stays in Backlog, out of the queen's sight, until the user
        releases it. Nothing is split: it is sized again once released."""
        self.github.set_field(task.project.board, task.url, "Size", report["size"])
        self.store.record("task_sized", task=task.key, project=task.project.name, run_id=run_id, size=report["size"],
                          files=report["files"], door=report["door"], criteria=sizing.criteria_in(item.body), slices=0)
        self.store.record("task_held", task=task.key, project=task.project.name, areas=areas, reason=report["reason"])
        self._decide(task, "task_held", f"needs {', '.join(areas)}")
        # The areas stay in the local log: the repo may be public.
        self.github.comment_issue(task.repo, task.number, "Kandu held this task: it needs a forbidden area of its "
                                  "project. It waits for the owner before any work starts.")

    def _wait_for_split(self, task):
        """Every proposal waits in `Needs split`, also when every slice is two-way: for the user's yes, and on a
        project whose split approval is the queen's, also for hers."""
        approval = self.store.project_setting(task.project, "split_approval")
        why = "the user accepts this project's splits" if approval == "user" \
            else "the queen may accept it, or leave it for the user"
        self._decide(task, "split_waits", why)
        self.github.comment_issue(task.repo, task.number, f"Kandu sized this {sizing.LARGE}. "
                                  "A proposal to split it waits for the owner.")

    def _size_backlog(self):
        """Runs before the queen looks, so a task is sized before she can see it.

        A splitter only reads, so every unsized task goes in this cycle, side by side.
        """
        today = self.store.today()
        unsized = []
        for project, item in sorted(self._board_items(), key=lambda pair: pair[1].priority != queen.HIGH):
            key = f"{item.repo}#{item.number}"
            if self._is_paused(project) or item.status != BACKLOG or split.current_size(self.store, key) is not None:
                continue
            if self.store.tokens_counted(today, project=project.name) >= project.daily_token_limit:
                continue
            if project.name in self.pr_held:  # sized, it would be released into a new PR
                continue
            if not self._reachable(project.machine):
                self.waiting_on[project.machine].add(key)
                continue
            task = Task(project, item.repo, item.number, item.title, item.url, item.status,
                        branch_for(item.number, item.title))
            unsized.append((task, item))
        if not unsized:
            return
        with ThreadPoolExecutor(max_workers=self.config.settings.supervisor.max_parallel_sizers) as pool:
            futures = [pool.submit(self._size_if_running, task, item) for task, item in unsized]
            wait(futures)

    def _size_if_running(self, task, item):
        # A limit reached or a stop meanwhile leaves the rest for the next cycle.
        if self.halt_reason is None and not self._stopping():
            self._run_task(task, lambda: self._size(task, item))

    def _check_project(self, project):
        """No agent and no tokens: the tests on the project's base branch, and the live URL answering."""
        problems = []
        for repo in project.repos:
            base = self._base(project, repo)
            cwd = self.runner.prepare_review(project, repo, "kandu/end-check", f"origin/{base}")
            if self.runner.run_test(project, cwd, new_run_id(self.now())) != 0:
                problems.append(f"{TESTS_FAIL_IN}{repo}")
            if project.deploy is not None and project.deploy.live_url:
                url = shlex.quote(project.deploy.live_url)
                if self.runner.run_command(project, cwd, f"curl -fsS -o /dev/null {url}", new_run_id(self.now())) != 0:
                    problems.append(LIVE_DOWN)
        return problems

    def _end_checks(self):
        """A machine that does not answer gets no check: a dropped connection says nothing of the project."""
        for project in self.config.projects:
            if self.merged[project.name] < END_CHECK_MERGES or self._is_paused(project) or self._stopping():
                continue
            if not self._reachable(project.machine):
                self.store.record("check_skipped", project=project.name, machine=project.machine)
                continue
            try:
                problems = self._check_project(project)
            except Unreachable:
                problems = None
            except (GitHubError, RunnerError) as e:
                problems = [f"{CHECK_NOT_RUN}: {type(e).__name__}"]
            if problems is None or problems and not self._reachable(project.machine):  # it dropped during the check
                self.store.record("check_skipped", project=project.name, machine=project.machine)
                continue
            self.store.record("project_checked", project=project.name, passed=not problems, detail="; ".join(problems))

    def _drive(self, task):
        """Moves one task as far as it can go now. Every step is read back from GitHub first.

        A pause or a stop lets the step that runs finish, and starts no other: no run, merge or deploy."""
        if self._is_paused(task.project) or self._stopping():
            return
        pr = self.github.find_pr(task.repo, task.number)
        if self._closed_elsewhere(task, pr):
            return
        step = queen.pending_step(queen.all_events(self.store), task.key)
        if step is not None and step["action"] == "verify":
            self._verify(task, pr, step)
            if task.status not in (READY, *RESUMED):
                return
        elif step is not None:
            if not self._rework(task, pr, step):
                return
            pr = self.github.find_pr(task.repo, task.number)
        if pr is None:
            pr = self._implement(task)
            if pr is None:
                return
            self._warn_if_oversized(task, pr)
        elif self._held_as_before(task, pr):
            return  # nothing it waits for has changed: no read from GitHub, no new decision
        else:
            # The copy may be older than this cycle: someone may have pushed to the branch since.
            self._reread(lambda: self.github.refresh_pr(task.repo, pr["number"]))
            pr = self.github.pr(task.repo, pr["number"])
        task.branch = pr["headRefName"]  # Worked out from the title, which may have changed since.
        worked, tries = False, 1
        while True:
            if self._is_paused(task.project) or self._stopping():
                return  # The step that was running has finished. The next cycle after a resume picks it up here.
            if pr["state"] == "MERGED":
                self._land(task)
                return
            if pr["state"] == "CLOSED":
                self.github.comment_issue(task.repo, task.number, "Kandu stopped: the PR of this task was closed.")
                self._finish(task, BLOCKED, "pr_closed")
                return
            reviews = review.reviews_on(pr, self.config.settings.github.account)
            if not reviews or reviews[-1]["head_sha"] != pr["headRefOid"]:
                self._review(task, pr, reviews)
            else:
                outcome = self._outcome(task, pr, reviews)
                if outcome is None:
                    self._fix(task, pr, reviews)
                elif self._wrong_base(task, pr):
                    return
                elif self._conflicts(pr, outcome) or self._main_to_bring(task, pr, outcome):
                    if self._update_waits(task, pr) or not self._update(task, pr):
                        return
                elif (merged := self._merge(task, pr, outcome, worked)) is None and tries < MERGE_TRIES:
                    tries += 1  # read again: it merges now, or its new conflict goes to an update round
                elif not merged:
                    return
                elif self.github.pr(task.repo, pr["number"])["state"] != "MERGED":
                    return  # A merge queue took it; a later cycle sees it land.
            worked = True
            pr = self.github.pr(task.repo, pr["number"])

    def _closed_elsewhere(self, task, pr):
        """Ends a task whose issue was closed while it was still open, and returns True if it did.

        Closed as completed by another merged PR or a commit on the default branch, the task is replaced: its own
        open PR is closed with a comment naming the replacement, and the card goes to `Done`. Closed any other way,
        the task stops in `Blocked` and its PR stays as it is. A task whose own PR merged lands, whatever closed its
        issue; so does one its own PR closed while the copy still shows that PR open, as the fresh read finds it merged.
        """
        if pr is not None and pr["state"] == "MERGED":
            return False
        closed = self.github.issue_closure(task.repo, task.number)
        if closed is None:
            return False
        by = closed["by"]
        if pr is not None and by is not None and (by["repo"], by.get("number")) == (task.repo, pr["number"]):
            return False
        if closed["reason"] == "COMPLETED" and by is not None and by["merged"]:
            replacement = f"{by['repo']}#{by['number']}" if "number" in by else f"{by['repo']}@{by['sha'][:7]}"
            if pr is not None and pr["state"] == "OPEN":
                self.github.comment_pr(task.repo, pr["number"], f"Kandu: closing this PR. Its issue was completed by "
                                                                f"{replacement}, which replaces it.")
                self.github.close_pr(task.repo, pr["number"])
            self._decide(task, "replaced", f"its issue was completed by {replacement}")
            self._finish(task, DONE, "replaced")
            return True
        self.github.comment_issue(task.repo, task.number, "Kandu stopped: this issue was closed while its task was open.")
        self._finish(task, BLOCKED, "issue_closed")
        return True

    def _run_task(self, task, drive=None):
        with self._lock:
            retry = self.retrying.get(task.key)
        try:
            (drive or (lambda: self._drive(task)))()
        except CycleStop as e:
            if retry is not None and isinstance(e.__cause__, GitHubError) and self._retry_read_later(task, retry,
                                                                                                    e.__cause__):
                retry = None  # it waits for its next try in this cycle
        except Exception as e:
            self._fail(task, e)
        finally:
            with self._lock:
                self.workers.pop(task.key, None)
                if retry is not None and self.retrying.get(task.key) is retry:
                    del self.retrying[task.key]  # the drive ended some other way: the next cycle takes it up
            self._snapshot("awake")

    def _internal_tries(self, task):
        """Kandu's own errors on this task since it last moved on."""
        return count_tries(queen.all_events(self.store), task.key)

    def _fail(self, task, error):
        """Details stay in the local log: the comment may be on a public repo.

        Kandu's own trouble leaves the task where it is, so the next cycle tries it again with nobody's help.
        Only when it comes back `INTERNAL_TRIES` times in a row does the task stop for the queen to look at.
        """
        details = "".join(traceback.format_exception_only(error)).strip()
        if isinstance(error, Unreachable):  # the rest of this cycle waits for the machine; tries.py counts no try
            with self._lock:
                self.seen[task.project.machine] = False
        if isinstance(error, Unreachable) or internal(error) and self._internal_tries(task) + 1 < INTERNAL_TRIES:
            self._decide(task, "internal_error", details)  # a drop is no try, so it never uses up the last one
            return
        self._decide(task, "failed", details)
        queen.end_failed_step(self.store, queen.all_events(self.store), task.key, task.project.name, details)
        try:
            self.github.comment_issue(task.repo, task.number, FAILURE_COMMENT)
            self._finish(task, BLOCKED, "stopped_by_user" if isinstance(error, StoppedByUser) else "failed")
        except Exception as e:
            self._decide(task, "failed", f"could not mark the task Blocked: {type(e).__name__}: {e}")

    # The queen.

    def _queen_limit(self):
        """Returns why no new work may start because of her own spending, or None."""
        settings = self.config.settings.supervisor
        if not settings.queen:
            return None
        spent = self.store.tokens_counted(self.store.today(), role="queen")
        return QUEEN_LIMIT if spent >= settings.queen_daily_token_limit else None

    def _startable(self, items):
        """The Backlog tasks nothing blocks, the splitter has sized and nothing holds."""
        return [(project, item) for project, item in items if item.status == BACKLOG and _can_start(self.github, item)
                and self.store.last_event("task_sized", task=f"{item.repo}#{item.number}") is not None
                and split.held(self.store, f"{item.repo}#{item.number}") is None]

    def _waiting(self, items, events):
        """The tasks of a project that wait on something, with the newest reason, so none is forgotten."""
        why = {}
        for event in events:
            if event["type"] == "decision" and event["decision"] in ("merge_waits", "failed"):
                why[event["task"]] = event["reason"][:300]
            elif event["type"] == "task_finished":
                why[event["task"]] = event["outcome"]
            elif event["type"] == "review_finished":
                why.pop(event["task"], None)  # a new head: what held the old one may be gone
        waiting = []
        for item in items:
            key = f"{item.repo}#{item.number}"
            retry = merge_retry(events, key)
            if retry is not None:
                waiting.append({"task": key, "status": item.status, "why": RETRYING, "next_try": retry["next_try"],
                                "stopped_for_a_loop": queen.waits_after_loop(events, key)})
            elif (item.status in (BLOCKED, NEEDS_INFO, IN_REVIEW) or _held_by_blockers(events, key)) and key in why:
                waiting.append({"task": key, "status": item.status, "why": why[key],
                                "stopped_for_a_loop": queen.waits_after_loop(events, key)})
        return waiting

    def _splits(self, items):
        """The split proposals she may accept: each one in `Needs split` of a project whose split approval is hers."""
        pending = {s["task"]: s for s in split.pending(self.store)}
        return [(project, pending[key]) for project, item in items
                if item.status == NEEDS_SPLIT and (key := f"{item.repo}#{item.number}") in pending
                and self.store.project_setting(project, "split_approval") == "queen"]

    def _project_view(self, items, backlog, events=(), by_hand=(), splits=()):
        """What she sees of every project: fields, not task bodies, so a long Backlog stays cheap.

        `by_hand` holds the open by-hand tasks: hers to name, never to act on. `splits` holds the split proposals
        she may accept, each named by the version she reads.
        """
        today = self.store.today()
        board = {f"{item.repo}#{item.number}": (project, item) for project, item in items}
        view = []
        for project in self.config.projects:
            if self._is_paused(project):
                continue
            ours = [item for owner, item in items if owner is project]
            view.append({
                "project": project.name, "rules": project.queen_rules,
                "queen_answers_one_way": self.store.project_setting(project, "queen_answers_one_way"),
                "tokens_today": self.store.tokens_counted(today, project=project.name),
                "daily_token_limit": project.daily_token_limit,
                "max_parallel_workers": project.max_parallel_workers,
                "at_work": sum(1 for item in ours if _at_work(item, events, self.github)),
                "first_in_line": self._first_in_line(project, board, backlog),
                "waiting": self._waiting(ours, events),
                "backlog": [{"task": f"{item.repo}#{item.number}", "title": item.title, "priority": item.priority,
                             "kind": item.kind, "size": item.size, "deadline": item.deadline}
                            for owner, item in backlog if owner is project],
                "by_hand": [{"task": f"{item.repo}#{item.number}", "title": item.title, "status": item.status}
                            for owner, item in by_hand if owner is project],
                "splits": [{"task": proposal["task"], "title": proposal["title"], "reason": proposal["reason"],
                            "proposal": split.fingerprint(proposal),
                            "slices": [{"title": part["title"], "size": part["size"], "door": part["door"]}
                                       for part in proposal["slices"]]}
                           for owner, proposal in splits if owner is project],
            })
        return view

    def _queen(self):
        """Wakes the queen if there is something to decide. Returns True if she looked."""
        settings = self.config.settings.supervisor
        if not settings.queen or self.halt_reason is not None:
            return False
        if self._stopping():
            return False
        self._carry_out_held()
        items = list(self._board_items())
        backlog = self._startable(items)
        keys = [f"{item.repo}#{item.number}" for _, item in backlog]
        by_hand = self._by_hand()
        hand_keys = sorted(f"{item.repo}#{item.number}" for _, item in by_hand)
        splits = self._splits(items)
        split_keys = sorted(f"{p['task']}@{split.fingerprint(p)}" for _, p in splits)
        before, news = queen.since_last_look(self.store)
        paused = [p for p in self.config.projects if self._is_paused(p)]
        news = [e for e in news if e.get("project") not in {p.name for p in paused}]
        hidden = {repo for p in paused for repo in p.repos}
        if not queen.worth_a_look(before, news, keys, self.store.today(), hidden, hand_keys, split_keys):
            return False
        if self._gate() is not None or self._queen_limit() is not None:
            return False
        events = before + news
        view = self._project_view(items, backlog, events, by_hand, splits)
        tasks = sorted({e["task"] for e in news if e.get("task")} | {w["task"] for p in view for w in p["waiting"]})
        steps = {task: done for task, done in queen.steps_on(events, tasks).items() if done}
        prompt = agents.queen_prompt(queen.prompt_events(news), view, steps,
                                     settings.queen_max_retries, self.config.settings.language,
                                     DEFAULT_LOGS_DIR, self.store.today(), queen.unparked(events),
                                     settings.queen_drop_blockers)
        allowed, denied = agents.role_tools(self._rules("queen"))
        run_id = new_run_id(self.now())
        self.queen_run = run_id
        self._snapshot("awake")
        try:
            result = self.runner.run_agent(self.config.settings.brain.machine, str(ROOT), prompt, allowed, denied,
                                           run_id, self._hard_stop, self._model("queen"),
                                           schema=agents.QUEEN_SCHEMA)
        finally:
            self.queen_run = None
            self._snapshot("awake")
        self.store.record("run_finished", task=None, project=None, run_id=run_id, role="queen",
                          models=result.models, duration_ms=result.duration_ms, is_error=result.is_error)
        if result.stopped:
            return True
        try:
            if result.is_error:
                raise agents.ReplyError("her run ended with an error")
            report = agents.parse_queen_report(result.text)
        except agents.ReplyError as e:
            # Everything stays as it is. Nothing here is worth paying for again each cycle.
            self.store.record("queen_finished", run_id=run_id, brief=f"{queen.COULD_NOT_FINISH}: {e}.",
                              backlog=keys, by_hand=hand_keys, splits=split_keys)
            return True
        if self._stopping():
            # Stop came while she looked: her decisions wait, recorded, until the user resumes Kandu.
            self.store.record("queen_held", run_id=run_id, brief=report["brief"], decisions=report["decisions"],
                              backlog=keys, by_hand=hand_keys, splits=split_keys)
            return False
        self._finish_look(run_id, report["brief"], report["decisions"], events, keys, hand_keys, split_keys)
        return True

    def _finish_look(self, run_id, brief, decisions, events, keys, hand_keys, split_keys):
        refused, released = self._carry_out(decisions, events)
        brief = "\n".join([brief, *[f"Refused: {reason}" for reason in refused]])
        self.store.record("queen_finished", run_id=run_id, brief=brief, backlog=[k for k in keys if k not in released],
                          by_hand=hand_keys, splits=split_keys)

    def _carry_out_held(self):
        """Her look that a stop held back is carried out on the first cycle after the resume, on a fresh board."""
        held = queen.held_look(self.store)
        if held is not None:
            self._finish_look(held["run_id"], held["brief"], held["decisions"], queen.all_events(self.store),
                              held["backlog"], held["by_hand"], held["splits"])

    def _carry_out(self, decisions, events):
        """Code checks every decision here, on a fresh board: she may have taken minutes.

        Returns why each refused one was refused, and what she released.
        """
        limit = self.config.settings.supervisor.queen_max_retries
        items = {f"{item.repo}#{item.number}": (project, item) for project, item in self._board_items()}
        refused, released = [], []
        today = self.store.today()
        backlog = self._startable(items.values()) if any(d["action"] == "release" for d in decisions) else []
        for decision in decisions:
            task, action, note = decision["task"], decision["action"], decision["note"]
            if task not in items:
                refused.append(f"{task}: not a task of a running project")
                continue
            project, item = items[task]
            why = self._why_not_while_retrying(events, task, decision)
            if why is not None:
                refused.append(f"{action} of {task}: {why}")
                continue
            why = queen.why_no_step(events, task, action, limit) if action in queen.LIMITED else None
            if why is not None:
                refused.append(f"{action} of {task}: {why}")
                self._loop_stop(project, item, task, action, why, events)
                continue
            if action == "park":
                why = self._park(project, item, task, decision, events)
                if why is not None:
                    refused.append(f"park of {task}: {why}")
                continue
            if action == "release":
                why = "it was already released" if task in released else queen.why_no_release(
                    item, _can_start(self.github, item), self.store.tokens_counted(today, project=project.name),
                    project.daily_token_limit, split.current_size(self.store, task),
                    self._first_in_line(project, items, backlog, released), split.held(self.store, task))
                if why is not None:
                    refused.append(f"release of {task}: {why}")
                    continue
                self.github.set_status(project.board, item.url, READY)
                released.append(task)
            if action == "retry":
                pr = self.github.find_pr(item.repo, item.number)
                why = queen.why_no_retry(events, task, limit, item.status in (BLOCKED, NEEDS_INFO),
                                         pr is not None and pr["state"] == "MERGED", split.current_size(self.store, task))
                if why is not None:
                    refused.append(f"retry of {task}: {why}")
                    continue
                self.github.set_status(project.board, item.url,
                                       BACKLOG if queen.last_outcome(events, task) == "sizing_failed" else READY)
            if action in queen.SENT_ON or action in ("new_task", "block_on"):
                why = self._send_on(project, item, task, decision)
                if why is not None:
                    refused.append(f"{action} of {task}: {why}")
                    continue
            if action == "drop_blocker":
                why = self._ask_to_drop(project, item, task, decision, events)
                if why is not None:
                    refused.append(f"drop_blocker of {task}: {why}")
                    continue
            if action == "accept_split":
                why = self._accept_split(project, task, decision)
                if why is not None:
                    refused.append(f"accept_split of {task}: {why}")
                    continue
            if action == "approve":
                why = self._queen_approve(project, item, task, decision)
                if why is not None:
                    refused.append(f"approve of {task}: {why}")
                    continue
            self.store.record("queen_decided", task=task, project=project.name, action=action, note=note)
            events.append({"type": "queen_decided", "task": task, "action": action, "note": note})
        return refused, released

    def _why_not_while_retrying(self, events, task, decision):
        """While Kandu tries a merge again by itself, nothing reaches the user: no question, and no second yes."""
        retry = merge_retry(events, task)
        if retry is None:
            return None
        if decision["action"] == "ask_user":
            return f"Kandu tries the merge again by itself, next at {retry['next_try']}"
        if decision["action"] == "approve" and any(
                e["type"] == "queen_approved" and e["task"] == task and e["head_sha"] == decision["head_sha"]
                for e in events):
            return f"she already approved this head, and Kandu tries the merge again by itself, next at " \
                   f"{retry['next_try']}"
        return None

    def _loop_stop(self, project, item, task, action, why, events):
        """Past a step's limit she is going round: the task stops, and the user hears of it once."""
        if queen.waits_after_loop(events, task):
            return
        if item.status not in (BLOCKED, DONE):
            self.github.set_status(project.board, item.url, BLOCKED)
        events.append(self.store.record("loop_stopped", task=task, project=project.name, action=action, reason=why))

    def _send_on(self, project, item, task, decision):
        """Her rework, verify, new task or merge order. Returns why not, or None."""
        action = decision["action"]
        pr = self.github.find_pr(item.repo, item.number)
        merged = pr is not None and pr["state"] == "MERGED"
        if item.status == DONE and action != "new_task":
            return "it is Done"
        if action in queen.SENT_ON and not self._is_open(task):
            return "its issue is closed: only the user can reopen it"
        if action == "rework":
            why = queen.why_no_rework(pr, merged)
            if why is None:
                self.github.set_status(project.board, item.url, IN_PROGRESS)
            return why
        if action == "verify":
            if pr is not None and pr["state"] == "OPEN" and self._tests_wait(task, pr["headRefOid"]):
                return merge.TESTS_WAIT + ": a check on this head waits too"
            return None  # carried out when code next drives the task; see `_sent_on`
        if action == "block_on":
            return self._block_on(item, task, decision["on"])
        return self._new_task(project, item, task, decision)

    def _block_on(self, item, task, on):
        repo, _, number = on.partition("#")
        if on == task or not number.isdigit():
            return f"{on} is not another task"
        if not any(repo in p.repos for p in self.config.projects):
            return f"{on} is not in a registered repo"
        body = self.github.issue_text(item.repo, item.number)["body"]
        if (repo, int(number)) in blockers_in(body, item.repo):
            return f"it already waits on {on}"
        self.github.edit_issue(item.repo, item.number, item.title, queen.with_blocker(body, item.repo, on))
        return None

    def _new_task(self, project, item, task, decision):
        """She opens a task as the user would, held to every rule of a new task, in the same repo."""
        fields = [decision[name] for name in ("priority", "kind", "size")]
        try:
            url = tasks.create_task(self.config, self.github, project.name, item.repo, decision["title"],
                                    decision["body"], self.local_dir, *fields, store=self.store)
        except (TaskError, GitHubError) as e:
            return str(e)
        key = f"{item.repo}#{url.rsplit('/', 1)[-1]}"
        self.store.record("queen_opened", task=task, project=project.name, url=url)
        if decision.get("blocks"):
            why = self._block_on(item, task, key)
            if why is not None:
                return f"opened {key}, but {why}"
        return None

    def _ask_to_drop(self, project, item, task, decision, events):
        """Her ask puts a question on the dashboard and changes nothing: only the user's yes drops the blocker.

        Returns why not, or None.
        """
        if not self.config.settings.supervisor.queen_drop_blockers:
            return "asking to drop a blocker is off"
        blocker = decision["on"]
        if not queen.names_blocker(self.github.issue_text(item.repo, item.number)["body"], item.repo, blocker):
            return f"its issue does not name {blocker} under Blocked by"
        if any(a["task"] == task and a["blocker"] == blocker for a in queen.drop_asks(events)):
            return f"it already asks to drop {blocker}"
        events.append(self.store.record("blocker_drop_asked", task=task, project=project.name, blocker=blocker,
                                        reason=decision["note"]))
        return None

    def _queen_approve(self, project, item, task, decision):
        """Her yes to one head answers its questions and its door, on a project whose switch lets it. While the switch
        is off, code refuses her yes to a head with a one-way door or a question for the user: those wait for the
        user's yes. The merge gate still checks everything else, and `auto_merge` stays the user's: she never merges
        a project the user merges by hand. Returns why not, or None.
        """
        pr = self.github.find_pr(item.repo, item.number)
        head = decision["head_sha"]
        reviewed = self.store.last_event("review_finished", task=task, head_sha=head)
        why = queen.why_no_approval(pr, reviewed, head)
        if why is not None:
            return why
        if not self.store.project_setting(project, "queen_answers_one_way"):
            asked = [q for q in questions.open_questions(self.store) if q["task"] == task]
            if reviewed["door"] != "two-way" or reviewed["wait_for_user"] or asked:
                return "the user answers this project's one-way merges"
        self.store.record("queen_approved", task=task, project=project.name, head_sha=head, note=decision["note"])
        return None

    def _accept_split(self, project, task, decision):
        """Her yes to the proposal she read is the user's yes, with all of its checks: `split.approve` refuses a task
        not in `Needs split` and a proposal that changed since. She can neither decline nor change one.
        Returns why not, or None.
        """
        if self.store.project_setting(project, "split_approval") != "queen":
            return "the user accepts this project's splits"
        try:
            children = split.approve(self.config, self.github, self.store, task, self.local_dir,
                                     seen=decision["proposal"])
        except (TaskError, split.ReportError, GitHubError) as e:
            return str(e)
        self.store.record("queen_accepted_split", task=task, project=project.name, children=children,
                          note=decision["note"])
        return None

    def _park(self, project, item, task, decision, events):
        """Only a note the splitter reported may become a task. Returns why not, or None."""
        finding = decision["finding"]
        note = next((e for e in queen.unparked(events) if e["task"] == task and e["finding"] == finding), None)
        if note is None:
            return f"no note {finding} waits to be parked"
        try:
            url = debt.add(self.config, self.github, project.name, item.repo, note["title"], note["body"],
                           self.local_dir)
        except (TaskError, GitHubError) as e:
            return str(e)
        self.store.record("debt_parked", task=task, project=project.name, finding=finding, url=url,
                          note=decision["note"])
        events.append({"type": "debt_parked", "task": task, "finding": finding})
        return None

    # One cycle.

    def _reachable(self, machine):
        answered = self.runner.reachable(machine)
        with self._lock:
            self.seen[machine] = answered
        return answered

    def _reachable_tasks(self, tasks):
        machines = {task.project.machine for task in tasks}
        reachable = {m for m in machines if self._reachable(m)}
        self.waiting = [t.key for t in tasks if t.project.machine not in reachable]
        for task in tasks:
            if task.project.machine not in reachable:
                self.waiting_on[task.project.machine].add(task.key)
        return [t for t in tasks if t.project.machine in reachable]

    def _outage(self, machine):
        """When the machine's open outage began, or None while it answers."""
        down = self.store.last_event("machine_down", machine=machine)
        if down is None or self.store.last_event("machine_up", machine=machine, since=down["since"]) is not None:
            return None
        return down["since"]

    def _watch_machines(self):
        """One message per long outage that tasks wait on, and one when the machine answers again.

        A machine is asked only when a task needs it, or while its outage is open, so its return is seen.
        """
        now = self.now()
        limit = timedelta(hours=self.config.settings.supervisor.unreachable_alert_hours)
        unreachable = []
        for machine in (m.name for m in self.config.settings.machines):
            since = self._outage(machine)
            answered = self.seen.get(machine)
            if answered is None and since is not None:
                answered = self._reachable(machine)
            if answered is None:
                continue
            told = since is not None and self.store.last_event("machine_unreachable", machine=machine,
                                                                 since=since) is not None
            if answered:
                if since is not None:
                    self.store.record("machine_up", machine=machine, since=since, told=told)
                continue
            if since is None:
                since = now.isoformat(timespec="seconds")
                self.store.record("machine_down", machine=machine, since=since)
            waiting = sorted(self.waiting_on.get(machine, ()))
            unreachable.append({"machine": machine, "since": since, "tasks": waiting})
            if waiting and not told and now - datetime.fromisoformat(since) >= limit:
                self.store.record("machine_unreachable", machine=machine, since=since, waiting=len(waiting))
        self.unreachable = unreachable

    def _over_budget(self, task):
        """A project at its daily limit starts nothing, however its task reached `Ready`."""
        limit = task.project.daily_token_limit
        if self.store.tokens_counted(self.store.today(), project=task.project.name) < limit:
            return False
        self._decide(task, "over_budget", f"daily limit of {limit} tokens")
        return True

    def _yes_came(self, key):
        """A merge that waited this cycle, and a yes for that head that came after: the queen's or the user's."""
        head = self.held.get(key)
        return head is not None and any(self.store.last_event(kind, task=key, head_sha=head) is not None
                                        for kind in ("queen_approved", "user_approved"))

    def _what_she_released(self, seen, busy=()):
        """A task ended: she looks again. What she releases joins this cycle, and so does a merge she said yes to."""
        try:
            if not self._queen():
                return []
            read = self._read_tasks()
            ready = [t for t in read if t.status == READY] + self._sent_on()
            ready = [t for t in ready if t.key not in seen]
            ready += [t for t in read if t.status == IN_REVIEW and t.key not in busy and self._yes_came(t.key)]
            tasks = self._reachable_tasks(self._not_while_red(list({t.key: t for t in ready}.values())))
        except (GitHubError, RunnerError) as e:
            self.queen_trouble = type(e).__name__
            return []
        with self._lock:
            for task in tasks:
                self.held.pop(task.key, None)  # one more drive for each yes
        seen.update(task.key for task in tasks)
        return tasks

    def _schedule(self, tasks):
        limit = self.config.settings.supervisor.max_parallel_workers
        pending = list(tasks)
        seen = {task.key for task in tasks}
        running = {}
        building = lambda t: t.status != IN_REVIEW  # A review only reads, so it takes no worker's place.
        with ThreadPoolExecutor(max_workers=limit + sum(1 for t in tasks if not building(t))) as pool:
            while pending or running or self._waits_for_retry():
                self.halt_reason = self.halt_reason or (self._queen_limit() if pending else None)
                for task in list(pending):
                    if self.halt_reason is not None or self._stopping():
                        break
                    if building(task):
                        if sum(1 for t in running.values() if building(t)) >= limit:
                            continue
                        in_project = sum(1 for t in running.values() if t.project is task.project and building(t))
                        if in_project >= task.project.max_parallel_workers:
                            continue
                        if task.status == READY and self._open_pr_hold(task.project) is not None:
                            continue  # its next start would open one PR too many; an open PR's work goes on
                    pending.remove(task)
                    if self._over_budget(task):
                        continue
                    running[pool.submit(self._run_task, task)] = task
                if not running:
                    if not self._waits_for_retry():
                        break
                    due = self._wait_for_retries()
                    if not due:
                        break
                    pending = sorted(pending + due, key=lambda t: t.order)
                    continue
                done, _ = wait(running, timeout=self._next_retry_in(), return_when=FIRST_COMPLETED)
                for future in done:
                    del running[future]
                pending += self._due_retries()
                if done:
                    busy = {t.key for t in pending} | {t.key for t in running.values()}
                    pending += self._what_she_released(seen, busy)
                pending.sort(key=lambda t: t.order)
        self._give_up_retries()

    def cycle(self):
        """Runs one cycle and returns why it ended. While the user has Kandu stopped, none starts."""
        if self._stopping():
            return self._stay_stopped()
        woke = self.now()
        self.merged.clear()
        self.held.clear()
        self.retrying.clear()
        self.seen.clear()
        self.waiting_on.clear()
        self.store.carry_settings(self.config.projects)  # before the first snapshot: it tells an install that ran before
        self.store.record("supervisor_woke")
        clean_since = self.store.clean_since(woke)  # set once, on the first cycle that has the cleanup
        self._snapshot("awake")
        settings = self.config.settings.supervisor
        self.deadline = woke + timedelta(minutes=settings.max_cycle_minutes)
        self.next_wake = (woke + timedelta(minutes=settings.wake_every_minutes)).isoformat(timespec="seconds")
        self.halt_reason = None
        self.queen_trouble = None
        failed = self.github.refresh(self.config)  # the copy as GitHub holds it now, before anything is decided
        if failed:
            self.store.record("copy_not_read", detail="; ".join(failed))
        self.base_held = self._check_bases()
        self._check_open_prs()
        reason = self._gate() or self._queen_limit()
        if reason is None:
            try:
                self._close_groups()
                self._close_specs()
                self._size_backlog()
                self._watch_main()
                self._tell_stuck_cards()
                self._queen()
            except (GitHubError, RunnerError) as e:
                self.queen_trouble = type(e).__name__
            reason = self._work_through_board()
        self._tell_open_flights()
        self._clean(clean_since)
        self._watch_machines()
        if self._stopping():
            reason = STOPPED_BY_USER  # the runs at work finished their step; the user stopped the rest
        if self.queen_trouble is not None:
            reason = f"{reason}; the queen could not look: {self.queen_trouble}"
        self.store.record("supervisor_slept", reason=reason, next_wake=self.next_wake, resets=self._resets(reason))
        self._snapshot("stopped" if self._stopping() else "asleep", reason, self.next_wake)
        return reason

    def _resets(self, reason):
        """When the limit that put the cycle to sleep resets, as an ISO time, so the user hears when work goes on.

        None when no limit did, or `/usage` did not say. The queen's tokens count by the local day.
        """
        if reason.startswith(QUEEN_LIMIT):
            midnight = (self.now().astimezone() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            return midnight.isoformat(timespec="seconds")
        if self.usage is None:
            return None
        if reason.startswith(SESSION_LIMIT):
            return self.usage.session_resets
        if reason.startswith(WEEK_LIMIT):
            return self.usage.week_resets
        return None

    def _stay_stopped(self):
        """A wake while stopped starts no cycle. The stop is recorded once, so the user hears of it once."""
        last = self.store.last_event("supervisor_slept")
        if last is None or last["reason"] != STOPPED_BY_USER:
            self.store.record("supervisor_slept", reason=STOPPED_BY_USER, next_wake=None, resets=None)
        self._snapshot("stopped", STOPPED_BY_USER)
        return STOPPED_BY_USER

    def _clean(self, since):
        """After the work: a merged task's folders and branches go. It costs no tokens, so no limit holds it back."""
        if not self.config.settings.supervisor.clean_merged or self.stop_file.exists():
            return
        clean.Cleaner(self.config, self.store, self.github, self.runner, flights=self.flights,
                      clock=lambda: self.now().timestamp()).run(since)

    def _tell_open_flights(self):
        """A task closed without a merge while its free flight is still open: the user hears once."""
        clean.tell_open_flights(self.store, self.github, self.flights)

    def _work_through_board(self):
        try:
            tasks = self._board_tasks()
            tasks += [t for t in self._sent_on() if t.key not in {other.key for other in tasks}]
        except GitHubError as e:
            return f"could not read the boards: {e}"
        tasks = self._not_while_red(tasks)
        if not tasks:
            return "nothing ready"
        tasks = self._reachable_tasks(tasks)
        if not tasks:
            return "waiting for a machine"
        self._schedule(tasks)
        self._end_checks()
        return self.halt_reason or "tasks ran out"

    def dry_run(self):
        """Shows what a cycle would do. Starts no agent and writes nothing."""
        reason = self._gate()
        if reason is not None:
            return [f"would sleep: {reason}"]
        tasks = self._board_tasks()
        return [f"{t.key} [{t.status}] on {t.project.machine}: reachable={self.runner.reachable(t.project.machine)}"
                for t in tasks] or ["would sleep: nothing ready"]


def _locked_cycle(supervisor):
    supervisor.store.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(supervisor.store.root / "supervisor.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("supervisor: another cycle is running")
            return
        print(f"supervisor: {supervisor.cycle()}")


def main():
    parser = argparse.ArgumentParser(description="Kandu's supervisor.")
    parser.add_argument("command", nargs="?", default="cycle", choices=("cycle", "stop", "resume", "status"))
    parser.add_argument("--dry-run", action="store_true", help="show what a cycle would do; no agent, no writes")
    parser.add_argument("--local", default=DEFAULT_LOCAL_DIR, help="config directory (default: local/)")
    args = parser.parse_args()
    store = StateStore()
    if args.command == "status":
        print(json.dumps(store.read_current() | {"stop_requested": store.stop_file.exists(),
                                                 "stopping": store.stopping()}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "stop":
        store.request_stop()
        return 0
    if args.command == "resume":
        store.stop_file.unlink(missing_ok=True)
        return 0
    try:
        config = load_config(args.local)
    except ConfigError as e:
        print(f"supervisor: {len(e.problems)} problem(s) in config; run: python -m kandu.config", file=sys.stderr)
        return 1
    supervisor = Supervisor(config, store, open_github(config), Runner(config, DEFAULT_LOGS_DIR), read_usage, args.local)
    if args.dry_run:
        print("\n".join(supervisor.dry_run()))
        return 0
    notifier = notifier_for(config.settings, store)
    if notifier is not None:
        store.listeners.append(notifier.on_event)
    try:
        _locked_cycle(supervisor)
    except Exception:
        if notifier is not None:
            notifier.crashed()
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
