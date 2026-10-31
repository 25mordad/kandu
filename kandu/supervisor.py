"""The main loop (DESIGN §5, §6).

This is code, not an LLM session: everything that needs no judgment runs here,
at no token cost, and still works when Claude is down. Agents, and the queen
above them, are started only for work that needs judgment. No state is kept between cycles beyond what the
board and the PR hold, so a cycle that is cut off resumes where it stopped.
"""
import argparse
import fcntl
import json
import secrets
import shlex
import sys
import threading
import traceback
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta

from pathlib import Path

from . import agents, debt, merge, queen, questions, review, sizing, split
from .config import DEFAULT_LOCAL_DIR, TECHNICAL_DEBT, ConfigError, load_config
from .github import AGENT_LABEL, TOKENS_LABEL, BoardCache, GitHub, GitHubError, blockers_in, branch_for
from .notify import notifier_for
from .runner import DEFAULT_LOGS_DIR, Runner, RunnerError
from .state import StateStore
from .tasks import TaskError
from .usage import UsageError, over_limit, read_usage

BACKLOG, READY, IN_PROGRESS, IN_REVIEW = "Backlog", "Ready", "In progress", "In review"
NEEDS_INFO, BLOCKED, DONE = "Needs info", "Blocked", "Done"
NEEDS_SPLIT = split.NEEDS_SPLIT
RESUMED = (IN_PROGRESS, IN_REVIEW)
MERGEABLE = ("approved", "approved_with_notes")
SIZED_PER_CYCLE = 3  # A long Backlog must not hold up merges and resumed tasks.
END_CHECK_MERGES = 2  # This many merges in a project in one cycle earn a check of the whole project.
LANDING_FAILED = ("deploy_failed", "live_check_failed")
ROOT = Path(__file__).resolve().parent.parent
FAILURE_COMMENT = "Kandu could not finish this step and stopped here. The details are in its local log."


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


def new_run_id(now):
    return f"r-{now:%Y%m%d%H%M%S}-{secrets.token_hex(3)}"


def board_items(config, github, paused=()):
    """Yields (project, item) for each `agent` issue of a registered project that is not paused.

    `paused` names the projects the user paused on the dashboard.
    """
    by_board = {}
    for project in config.projects:
        if project.board is not None and not project.paused and project.name not in paused:
            by_board.setdefault((project.board.owner, project.board.number), []).append(project)
    for projects in by_board.values():
        by_repo = {repo: project for project in projects for repo in project.repos}
        for item in github.board_items(projects[0].board):
            project = by_repo.get(item.repo)
            if project is not None and AGENT_LABEL in item.labels:
                yield project, item


def read_tasks(config, github, paused=()):
    """Returns the tasks the loop may work on: registered, not paused, and not waiting on a blocker."""
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
                TOKENS_LABEL in item.labels and bool(project.task_tokens))


def _can_start(github, item):
    if not github.issue_is_open(item.repo, item.number):
        return False
    return not any(github.issue_is_open(repo, n) for repo, n in blockers_in(item.body, item.repo))


class Supervisor:
    def __init__(self, config, store, github, runner, usage_reader, local_dir=DEFAULT_LOCAL_DIR, now=None):
        self.config = config
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
        self.merged = Counter()
        self._lock = threading.Lock()
        self._deploy_locks = defaultdict(threading.Lock)

    def _is_paused(self, project):
        return project.paused or project.name in self.store.paused_projects()

    def _board_items(self):
        return board_items(self.config, self.github, self.store.paused_projects())

    def _read_tasks(self):
        return read_tasks(self.config, self.github, self.store.paused_projects())

    def _merged_while_blocked(self):
        """Tasks in `Blocked` whose PR someone merged. A failed deploy or live check stays for a person to look at."""
        finished = {e["task"]: e["outcome"] for e in queen.all_events(self.store) if e["type"] == "task_finished"}
        tasks = []
        for project, item in self._board_items():
            if item.status != BLOCKED or finished.get(f"{item.repo}#{item.number}") in LANDING_FAILED:
                continue
            pr = self.github.find_pr(item.repo, item.number)
            if pr is not None and pr["state"] == "MERGED":
                tasks.append(_task(project, item))
        return tasks

    # Limits. Each of these is checked in code before any agent starts.

    @property
    def stop_file(self):
        return self.store.stop_file

    def _hard_stop(self):
        """True when running agents must be killed: the deadline passed or the user pressed stop."""
        return self.now() >= self.deadline or self.stop_file.exists()

    def _gate(self):
        """Returns why no agent may start now, or None."""
        if self.stop_file.exists():
            return "stopped by the user"
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
        usage = self.usage and {"session": self.usage.session, "week": self.usage.week}
        with self._lock:
            self.store.write_current({
                "supervisor": supervisor, "last_reason": reason, "next_wake": next_wake, "usage": usage,
                "workers": list(self.workers.values()), "waiting_for_machine": self.waiting,
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

    def _decide(self, task, decision, reason):
        self.store.record("decision", task=task.key, project=task.project.name, decision=decision, reason=reason)

    # Running agents.

    def _model(self, role):
        return self.config.settings.claude.models.get(role)

    def _agent(self, task, role, cwd, prompt, tools, run_id, tokens=None):
        reason = self._gate()
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
                                       lambda: self._hard_stop() or stop_one.exists(), self._model(role), tokens)
        self.store.record("run_finished", task=task.key, project=task.project.name, run_id=run_id, role=role,
                          models=result.models, duration_ms=result.duration_ms, is_error=result.is_error)
        if stop_one.exists():
            stop_one.unlink()
            raise StoppedByUser(f"{role} run {run_id} was stopped by the user")
        if result.stopped:
            self._halt("stopped while an agent was running")
        if result.is_error:
            reason = self._gate()  # A run cut off by a limit is not the task's fault.
            if reason is not None:
                self._halt(reason)
            raise TaskFailed(f"{role} run {run_id} ended with an error")
        return result

    def _work(self, task, findings=None, pr=None):
        run_id = new_run_id(self.now())
        base = self.github.default_branch(task.repo)
        cwd = self.runner.prepare_worktree(task.project, task.repo, task.branch, base)
        note = queen.note_for(queen.all_events(self.store), task.key)
        prompt = agents.worker_prompt(task.project, task.repo, task.number, task.branch, run_id,
                                      findings=findings, pr=pr, local_dir=self.local_dir,
                                      queen_note=note, tokens=task.tokens)
        project = task.project
        tokens = (project.secrets_from, list(project.task_tokens)) if task.tokens else None
        return self._agent(task, "worker", cwd, prompt, agents.worker_tools(project, task.branch), run_id, tokens)

    def _implement(self, task):
        """First run. Returns the new PR, or None if the worker gave the task back."""
        self._pick(task)
        self._move(task, IN_PROGRESS)
        self._phase(task, "implement")
        result = self._work(task)
        try:
            reply = agents.parse_worker_reply(result.text)
        except agents.ReplyError as e:
            raise TaskFailed(str(e)) from e
        if reply["status"] == "needs_info":
            self.github.comment_issue(task.repo, task.number, reply["question"])
            self._finish(task, NEEDS_INFO, "needs_info")
            return None
        if reply["status"] == "blocked":
            self.github.comment_issue(task.repo, task.number, reply["reason"])
            self._finish(task, BLOCKED, "blocked")
            return None
        pr = self.github.find_pr(task.repo, task.number)
        if pr is None:
            raise TaskFailed("worker reported a PR, but its branch has none")
        return pr

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
        head = pr["headRefOid"]
        round_number = len(reviews) + 1
        self._phase(task, f"review round {round_number}")
        to_fix = self._to_fix(task, reviews[-1]) if reviews else []
        language = self.config.settings.language
        run_id = new_run_id(self.now())
        cwd = self.runner.prepare_review(task.project, task.repo, task.branch, head)
        if to_fix:
            prompt = agents.second_review_prompt(task.repo, task.number, pr["number"], head, language, to_fix)
        else:
            criteria = sizing.criteria_list(self.github.issue_text(task.repo, task.number)["body"])
            first_n = 1 + max((f["n"] for r in reviews for f in r["open"]), default=0)
            companions = self.github.companions(task.repo, pr, self._family_repos(task.project))
            prompt = agents.reviewer_prompt(task.project, task.repo, task.number, pr["number"], head, language,
                                            companions, criteria, first_n, local_dir=self.local_dir)
        result = self._agent(task, "reviewer", cwd, prompt, agents.reviewer_tools(), run_id)
        try:
            report = review.parse_report(result.text)
            review.validate_report(report, to_fix, second=bool(to_fix))
            dropped = (review.carry_over(report, reviews[-1], to_fix) if to_fix else
                       review.hold_to_scope(report, criteria, [f["path"] for f in pr["files"]]))
        except review.ReportError as e:
            raise TaskFailed(f"reviewer run {run_id}: {e}") from e
        if report["head_sha"] != head:
            self._decide(task, "review_voided", "the head moved during the review")
            return
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
            prompt = agents.notes_prompt(task.project, task.repo, task.number, pr["number"], notes, self.local_dir)
            result = self._agent(task, "splitter", cwd, prompt, agents.splitter_tools(), run_id)
            report = split.parse_report(result.text)
            split.validate_notes(report, notes)
            sized = report["notes"]
        except StoppedByUser:
            raise
        except (TaskFailed, split.ReportError, GitHubError, RunnerError) as e:
            self._decide(task, "notes_not_sized", str(e))
            sized = split.unsized_notes(notes)
        self.store.record("notes_sized", task=task.key, project=task.project.name, run_id=run_id, head_sha=head,
                          notes=sized)
        return sized

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
            return self._settle(task, pr, last["open"], "the second review found it not done")
        self._to_debt(task, [n for n in self._sized_notes(task, pr, last) if not split.stays_in_pr(n)])
        to_fix = self._to_fix(task, last)
        if not to_fix:
            return "approved_with_notes"
        if review.fix_tried(pr, account, pr["headRefOid"]):
            return self._settle(task, pr, to_fix, "the fix round changed no code")
        return None

    def _fix(self, task, pr, reviews):
        """One fix round by a fresh worker on the same branch. A round that adds no commit is recorded."""
        head = pr["headRefOid"]
        self._pick(task)
        self._phase(task, f"fix round {len(reviews)}")
        self._work(task, findings=self._to_fix(task, reviews[-1]), pr=pr["number"])
        if self.github.pr(task.repo, pr["number"])["headRefOid"] == head:
            self.github.comment_pr(task.repo, pr["number"], review.fix_comment(head))

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

    def _merge_waits(self, task, pr):
        """Returns why Kandu may not merge. The tests run last: they are the slow check."""
        head = pr["headRefOid"]
        reviewed = self.store.last_event("review_finished", task=task.key, head_sha=head)
        threads = self.github.review_threads(task.repo, pr["number"])
        asked = [q["question"] for q in questions.open_questions(self.store) if q["task"] == task.key]
        reasons = merge.merge_waits(task.project, pr, reviewed, threads, self._unreviewed_companions(task, pr),
                                    self._approved(task, pr), asked)
        if reasons:
            return reasons
        cwd = self.runner.prepare_review(task.project, task.repo, task.branch, head)
        code = self.runner.run_command(task.project, cwd, task.project.test, new_run_id(self.now()))
        return [] if code == 0 else ["the tests fail on this head"]

    def _merge(self, task, pr, outcome, worked):
        """Merges a settled PR if every condition holds. Returns True if it did."""
        if outcome not in MERGEABLE or not (self.config.auto_merge_for(task.project) or self._approved(task, pr)):
            if worked:
                self._decide(task, outcome, "the user decides next")
            return False
        self._pick(task)
        self._phase(task, "merge")
        reasons = self._merge_waits(task, pr)
        if reasons:
            self._decide(task, "merge_waits", "; ".join(reasons))
            return False
        if self._is_paused(task.project):  # The tests take minutes, and a pause may have come in meanwhile.
            return False
        self.github.merge_pr(task.repo, pr["number"], pr["headRefOid"])
        return True

    def _deploy(self, task):
        """Returns False if the deploy failed."""
        project = task.project
        run_id = new_run_id(self.now())
        self._phase(task, "deploy")
        with self._deploy_locks[project.name]:
            base = self.github.default_branch(task.repo)
            cwd = self.runner.prepare_review(project, task.repo, "kandu/deploy", f"origin/{base}")
            code = self.runner.run_command(project, cwd, project.deploy.command, run_id, secrets=True)
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
        base = self.github.default_branch(task.repo)
        cwd = self.runner.prepare_review(project, task.repo, "kandu/live-check", f"origin/{base}")
        prompt = agents.live_check_prompt(project, task.repo, task.number, self.local_dir)
        result = self._agent(task, "live_check", cwd, prompt, agents.live_check_tools(project), run_id)
        try:
            reply = agents.parse_live_check(result.text)
        except agents.ReplyError as e:
            raise TaskFailed(f"live check run {run_id}: {e}") from e
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
            self._close_groups()
        except GitHubError as e:  # the slice is done; the next cycle closes its parent
            self._decide(task, "group_not_closed", str(e))

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

    def _group_ahead(self, project, key, items, released=()):
        """Returns the parent of a group that must finish before `key` may start, or None.

        A `High` task waits only for the slice at work, never for the rest of its group.
        So does a slice of a group under way, or two groups `High` left half done would wait for each other.
        """
        groups = list(self._groups(project, items, released))
        high = key in items and items[key][1].priority == queen.HIGH
        began = any(under_way and key in event["children"] for event, under_way, _ in groups)
        for event, under_way, at_work in groups:
            if key not in event["children"] and (at_work if high or began else under_way):
                return event["task"]
        return None

    def _first_in_line(self, project, items, backlog, released=()):
        """Returns the tasks of `project` nothing else may go before: what is `High`, else the next slice of its group."""
        may_go = [(f"{item.repo}#{item.number}", item) for owner, item in backlog
                  if owner is project and queen.LARGE not in (item.size, self._sized_as(item))]
        may_go = [(key, item) for key, item in may_go if key not in released
                  and self._group_ahead(project, key, items, released) is None]
        high = [key for key, item in may_go if item.priority == queen.HIGH]
        if high:
            return high
        under_way = {child for event, going, _ in self._groups(project, items, released) if going
                     for child in event["children"]}
        return [key for key, _ in may_go if key in under_way]

    def _sized_as(self, item):
        sized = split.current_size(self.store, f"{item.repo}#{item.number}")
        return sized["size"] if sized else None

    # The splitter. It sees each task once, before the queen may release it.

    def _size(self, task, item):
        self._pick(task)
        self._phase(task, "sizing")
        run_id = new_run_id(self.now())
        try:
            base = self.github.default_branch(task.repo)
            cwd = self.runner.prepare_review(task.project, task.repo, f"kandu/size-{task.number}", f"origin/{base}")
            prompt = agents.splitter_prompt(task.project, task.repo, task.number, self.config.settings.language,
                                            self.local_dir)
            result = self._agent(task, "splitter", cwd, prompt, agents.splitter_tools(), run_id)
            report = split.parse_report(result.text)
            split.validate_report(report, item.body)
        except StoppedByUser:
            raise
        except (TaskFailed, split.ReportError, GitHubError, RunnerError) as e:
            self._decide(task, "sizing_failed", str(e))
            self.github.comment_issue(task.repo, task.number, "Kandu could not size this task. "
                                      "Move it back to Backlog to try again. The details are in its local log.")
            self._finish(task, NEEDS_INFO, "sizing_failed")
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
            self.github.comment_issue(task.repo, task.number, f"Kandu sized this {sizing.LARGE}. "
                                      "A proposal to split it waits for the owner.")
            self._move(task, NEEDS_SPLIT)

    def _size_backlog(self):
        """Runs before the queen looks, so a task is sized before she can see it."""
        today = self.store.today()
        sized = 0
        for project, item in sorted(self._board_items(), key=lambda pair: pair[1].priority != queen.HIGH):
            key = f"{item.repo}#{item.number}"
            if self._is_paused(project) or item.status != BACKLOG or split.current_size(self.store, key) is not None:
                continue
            if sized == SIZED_PER_CYCLE:
                return
            if self.store.tokens_counted(today, project=project.name) >= project.daily_token_limit:
                continue
            if not self.runner.reachable(project.machine):
                continue
            task = Task(project, item.repo, item.number, item.title, item.url, item.status,
                        branch_for(item.number, item.title))
            self._run_task(task, lambda task=task, item=item: self._size(task, item))
            sized += 1
            if self.halt_reason is not None:
                return

    def _check_project(self, project):
        """No agent and no tokens: the tests on the default branch, and the live URL answering."""
        problems = []
        for repo in project.repos:
            base = self.github.default_branch(repo)
            cwd = self.runner.prepare_review(project, repo, "kandu/end-check", f"origin/{base}")
            if self.runner.run_command(project, cwd, project.test, new_run_id(self.now())) != 0:
                problems.append(f"tests fail in {repo}")
            if project.deploy is not None and project.deploy.live_url:
                url = shlex.quote(project.deploy.live_url)
                if self.runner.run_command(project, cwd, f"curl -fsS -o /dev/null {url}", new_run_id(self.now())) != 0:
                    problems.append("the live URL does not answer")
        return problems

    def _end_checks(self):
        for project in self.config.projects:
            if self.merged[project.name] < END_CHECK_MERGES or self._is_paused(project):
                continue
            try:
                problems = self._check_project(project)
            except (GitHubError, RunnerError) as e:
                problems = [f"the check could not run: {type(e).__name__}"]
            self.store.record("project_checked", project=project.name, passed=not problems, detail="; ".join(problems))

    def _drive(self, task):
        """Moves one task as far as it can go now. Every step is read back from GitHub first."""
        if self._is_paused(task.project):
            return
        pr = self.github.find_pr(task.repo, task.number)
        if pr is None:
            pr = self._implement(task)
            if pr is None:
                return
            self._warn_if_oversized(task, pr)
        task.branch = pr["headRefName"]  # Worked out from the title, which may have changed since.
        worked = False
        while True:
            if self._is_paused(task.project):
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
                elif not self._merge(task, pr, outcome, worked):
                    return
                elif self.github.pr(task.repo, pr["number"])["state"] != "MERGED":
                    return  # A merge queue took it; a later cycle sees it land.
            worked = True
            pr = self.github.pr(task.repo, pr["number"])

    def _run_task(self, task, drive=None):
        try:
            (drive or (lambda: self._drive(task)))()
        except CycleStop:
            pass
        except Exception as e:
            self._fail(task, e)
        finally:
            with self._lock:
                self.workers.pop(task.key, None)
            self._snapshot("awake")

    def _fail(self, task, error):
        """Details stay in the local log: the comment may be on a public repo."""
        details = "".join(traceback.format_exception_only(error)).strip()
        self._decide(task, "failed", details)
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
        return "the queen reached her daily token limit" if spent >= settings.queen_daily_token_limit else None

    def _startable(self, items):
        """The Backlog tasks nothing blocks and the splitter has sized."""
        return [(project, item) for project, item in items if item.status == BACKLOG and _can_start(self.github, item)
                and self.store.last_event("task_sized", task=f"{item.repo}#{item.number}") is not None]

    def _project_view(self, items, backlog):
        """What she sees of every project: fields, not task bodies, so a long Backlog stays cheap."""
        today = self.store.today()
        board = {f"{item.repo}#{item.number}": (project, item) for project, item in items}
        view = []
        for project in self.config.projects:
            if self._is_paused(project):
                continue
            ours = [item for owner, item in items if owner is project]
            view.append({
                "project": project.name, "rules": project.queen_rules,
                "tokens_today": self.store.tokens_counted(today, project=project.name),
                "daily_token_limit": project.daily_token_limit,
                "max_parallel_workers": project.max_parallel_workers,
                "at_work": sum(1 for item in ours if item.status in (READY, *RESUMED)),
                "first_in_line": self._first_in_line(project, board, backlog),
                "backlog": [{"task": f"{item.repo}#{item.number}", "title": item.title, "priority": item.priority,
                             "kind": item.kind, "size": item.size, "deadline": item.deadline}
                            for owner, item in backlog if owner is project],
            })
        return view

    def _queen(self):
        """Wakes the queen if there is something to decide. Returns True if she looked."""
        settings = self.config.settings.supervisor
        if not settings.queen or self.halt_reason is not None:
            return False
        items = list(self._board_items())
        backlog = self._startable(items)
        keys = [f"{item.repo}#{item.number}" for _, item in backlog]
        before, news = queen.since_last_look(self.store)
        paused = [p for p in self.config.projects if self._is_paused(p)]
        news = [e for e in news if e.get("project") not in {p.name for p in paused}]
        hidden = {repo for p in paused for repo in p.repos}
        if not queen.worth_a_look(before, news, keys, self.store.today(), hidden):
            return False
        if self._gate() is not None or self._queen_limit() is not None:
            return False
        events = before + news
        tasks = sorted({e["task"] for e in news if e.get("task")})
        retries = {task: queen.retries_used(events, task) for task in tasks}
        prompt = agents.queen_prompt(queen.prompt_events(news), self._project_view(items, backlog), retries,
                                     settings.queen_max_retries, self.config.settings.language,
                                     DEFAULT_LOGS_DIR, self.store.today(), queen.unparked(events))
        allowed, denied = agents.queen_tools()
        run_id = new_run_id(self.now())
        self.queen_run = run_id
        self._snapshot("awake")
        try:
            result = self.runner.run_agent(self.config.settings.brain.machine, str(ROOT), prompt, allowed, denied,
                                           run_id, self._hard_stop, self._model("queen"))
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
                              backlog=keys)
            return True
        refused, released = self._carry_out(report["decisions"], events)
        brief = "\n".join([report["brief"], *[f"Refused: {reason}" for reason in refused]])
        self.store.record("queen_finished", run_id=run_id, brief=brief, backlog=[k for k in keys if k not in released])
        return True

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
            if action == "park":
                why = self._park(project, item, task, decision, events)
                if why is not None:
                    refused.append(f"park of {task}: {why}")
                continue
            if action == "release":
                why = "it was already released" if task in released else queen.why_no_release(
                    item, _can_start(self.github, item), self.store.tokens_counted(today, project=project.name),
                    project.daily_token_limit, split.current_size(self.store, task),
                    self._group_ahead(project, task, items, released),
                    self._first_in_line(project, items, backlog, released))
                if why is not None:
                    refused.append(f"release of {task}: {why}")
                    continue
                self.github.set_status(project.board, item.url, READY)
                released.append(task)
            if action == "retry":
                pr = self.github.find_pr(item.repo, item.number)
                why = queen.why_no_retry(events, task, limit, item.status == BLOCKED,
                                         pr is not None and pr["state"] == "MERGED", split.current_size(self.store, task))
                if why is not None:
                    refused.append(f"retry of {task}: {why}")
                    continue
                self.github.set_status(project.board, item.url, READY)
            self.store.record("queen_decided", task=task, project=project.name, action=action, note=note)
            events.append({"type": "queen_decided", "task": task, "action": action})
        return refused, released

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

    def _reachable_tasks(self, tasks):
        machines = {task.project.machine for task in tasks}
        reachable = {m for m in machines if self.runner.reachable(m)}
        self.waiting = [t.key for t in tasks if t.project.machine not in reachable]
        return [t for t in tasks if t.project.machine in reachable]

    def _over_budget(self, task):
        """A project at its daily limit starts nothing, however its task reached `Ready`."""
        limit = task.project.daily_token_limit
        if self.store.tokens_counted(self.store.today(), project=task.project.name) < limit:
            return False
        self._decide(task, "over_budget", f"daily limit of {limit} tokens")
        return True

    def _what_she_released(self, seen):
        """A task ended: she looks again, and what she releases joins this cycle."""
        try:
            if not self._queen():
                return []
            tasks = self._reachable_tasks([t for t in self._read_tasks()
                                           if t.status == READY and t.key not in seen])
        except (GitHubError, RunnerError) as e:
            self.queen_trouble = type(e).__name__
            return []
        seen.update(task.key for task in tasks)
        return tasks

    def _schedule(self, tasks):
        limit = self.config.settings.supervisor.max_parallel_workers
        pending = list(tasks)
        seen = {task.key for task in tasks}
        running = {}
        with ThreadPoolExecutor(max_workers=limit) as pool:
            while pending or running:
                self.halt_reason = self.halt_reason or (self._queen_limit() if pending else None)
                for task in list(pending):
                    if self.halt_reason is not None:
                        break
                    if len(running) >= limit:
                        break
                    in_project = sum(1 for t in running.values() if t.project is task.project)
                    if in_project >= task.project.max_parallel_workers:
                        continue
                    pending.remove(task)
                    if self._over_budget(task):
                        continue
                    running[pool.submit(self._run_task, task)] = task
                if not running:
                    return
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    del running[future]
                pending = sorted(pending + self._what_she_released(seen), key=lambda t: t.order)

    def cycle(self):
        """Runs one cycle and returns why it ended."""
        woke = self.now()
        self.merged.clear()
        self.store.record("supervisor_woke")
        self._snapshot("awake")
        settings = self.config.settings.supervisor
        self.deadline = woke + timedelta(minutes=settings.max_cycle_minutes)
        self.halt_reason = None
        self.queen_trouble = None
        reason = self._gate() or self._queen_limit()
        if reason is None:
            try:
                self._close_groups()
                self._size_backlog()
                self._queen()
            except (GitHubError, RunnerError) as e:
                self.queen_trouble = type(e).__name__
            reason = self._work_through_board()
        if self.queen_trouble is not None:
            reason = f"{reason}; the queen could not look: {self.queen_trouble}"
        next_wake = (woke + timedelta(minutes=settings.wake_every_minutes)).isoformat(timespec="seconds")
        self.store.record("supervisor_slept", reason=reason, next_wake=next_wake)
        self._snapshot("stopped" if self.stop_file.exists() else "asleep", reason, next_wake)
        return reason

    def _work_through_board(self):
        try:
            tasks = self._read_tasks() + self._merged_while_blocked()
        except GitHubError as e:
            return f"could not read the boards: {e}"
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
        tasks = self._read_tasks() + self._merged_while_blocked()
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
        print(json.dumps(store.read_current() | {"stop_requested": store.stop_file.exists()}, ensure_ascii=False, indent=2))
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
    supervisor = Supervisor(config, store, GitHub(cache=BoardCache()), Runner(config, DEFAULT_LOGS_DIR), read_usage, args.local)
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
