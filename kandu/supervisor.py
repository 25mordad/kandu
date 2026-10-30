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

from . import agents, merge, queen, review, sizing, split
from .config import DEFAULT_LOCAL_DIR, ConfigError, load_config
from .github import AGENT_LABEL, GitHub, GitHubError, blockers_in, branch_for
from .runner import DEFAULT_LOGS_DIR, Runner, RunnerError
from .state import StateStore
from .usage import UsageError, over_limit, read_usage

BACKLOG, READY, IN_PROGRESS, IN_REVIEW = "Backlog", "Ready", "In progress", "In review"
NEEDS_INFO, BLOCKED, DONE = "Needs info", "Blocked", "Done"
NEEDS_SPLIT = split.NEEDS_SPLIT
RESUMED = (IN_PROGRESS, IN_REVIEW)
MERGEABLE = ("approved", "approved_with_notes")
END_CHECK_MERGES = 2  # This many merges in a project in one cycle earn a check of the whole project.
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

    @property
    def key(self):
        return f"{self.repo}#{self.number}"

    @property
    def order(self):
        return (self.project.priority, self.status not in RESUMED, self.number)


class CycleStop(Exception):
    """This cycle must end; the task resumes in a later one."""


class TaskFailed(Exception):
    """Something went wrong that a person must look at."""


class StoppedByUser(TaskFailed):
    """The user stopped this worker. Only the user sends it back."""


def new_run_id(now):
    return f"r-{now:%Y%m%d%H%M%S}-{secrets.token_hex(3)}"


def board_items(config, github):
    """Yields (project, item) for each `agent` issue of a registered project that is not paused."""
    by_board = {}
    for project in config.projects:
        if project.board is not None and not project.paused:
            by_board.setdefault((project.board.owner, project.board.number), []).append(project)
    for projects in by_board.values():
        by_repo = {repo: project for project in projects for repo in project.repos}
        for item in github.board_items(projects[0].board):
            project = by_repo.get(item.repo)
            if project is not None and AGENT_LABEL in item.labels:
                yield project, item


def read_tasks(config, github):
    """Returns the tasks the loop may work on: registered, not paused, and not waiting on a blocker."""
    tasks = []
    for project, item in board_items(config, github):
        if item.status not in (READY, *RESUMED):
            continue
        if item.status == READY and not _can_start(github, item):
            continue
        tasks.append(Task(project, item.repo, item.number, item.title, item.url, item.status,
                          branch_for(item.number, item.title)))
    return sorted(tasks, key=lambda t: t.order)


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

    def _agent(self, task, role, cwd, prompt, tools, run_id):
        reason = self._gate()
        if reason is not None:
            self._halt(reason)
        allowed, denied = tools
        with self._lock:
            self.workers[task.key]["run_id"] = run_id
        self._snapshot("awake")
        stop_one = self.store.worker_stop_file(run_id)
        result = self.runner.run_agent(task.project.machine, cwd, prompt, allowed, denied, run_id,
                                       lambda: self._hard_stop() or stop_one.exists())
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
                                      queen_note=note)
        return self._agent(task, "worker", cwd, prompt, agents.worker_tools(task.project, task.branch), run_id)

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
        still_open = reviews[-1]["open"] if reviews else []
        first_n = 1 + max((f["n"] for r in reviews for f in r["open"]), default=0)
        run_id = new_run_id(self.now())
        cwd = self.runner.prepare_review(task.project, task.repo, task.branch, head)
        companions = self.github.companions(task.repo, pr, self._family_repos(task.project))
        prompt = agents.reviewer_prompt(task.project, task.repo, task.number, pr["number"], head,
                                        self.config.settings.notify.language, companions, still_open, first_n,
                                        local_dir=self.local_dir)
        result = self._agent(task, "reviewer", cwd, prompt, agents.reviewer_tools(), run_id)
        try:
            report = review.parse_report(result.text)
            review.validate_report(report, still_open)
        except review.ReportError as e:
            raise TaskFailed(f"reviewer run {run_id}: {e}") from e
        if report["head_sha"] != head:
            self._decide(task, "review_voided", "the head moved during the review")
            return
        findings = review.open_after(report, still_open)
        self.github.comment_pr(task.repo, pr["number"], review.comment_for(report, round_number, findings))
        self.store.record("review_finished", task=task.key, project=task.project.name, run_id=run_id,
                          round=round_number, head_sha=head, findings=len(findings),
                          blockers=len(review.blockers(findings)), door=report["door"],
                          criteria_met=all(c["result"] == "met" for c in report["criteria"]),
                          wait_for_user=report["wait_for_user"], user_brief=report["user_brief"])

    def _settle(self, task, pr, findings, reason):
        """No more fix rounds: a blocker stops the task, a note stays on the PR for the user."""
        if not review.blockers(findings):
            return "approved_with_notes"
        self.github.comment_pr(task.repo, pr["number"], f"Kandu stopped here: {reason}. A blocker is still open.")
        self._finish(task, BLOCKED, "blocked")
        return "blocked"

    def _outcome(self, task, pr, reviews):
        """Returns where a reviewed head leaves the task, or None if a fix round should run."""
        account = self.config.settings.github.account
        limit = self.config.settings.supervisor.max_review_rounds
        findings = reviews[-1]["open"]
        if not findings:
            return "approved"
        if len(reviews) > limit:
            return self._settle(task, pr, findings, f"{limit} fix rounds are used up")
        if review.fix_tried(pr, account, pr["headRefOid"]):
            return self._settle(task, pr, findings, "the last fix round changed no code")
        return None

    def _fix(self, task, pr, reviews):
        """One fix round by a fresh worker on the same branch. A round that adds no commit is recorded."""
        head = pr["headRefOid"]
        self._pick(task)
        self._phase(task, f"fix round {len(reviews)}")
        self._work(task, findings=reviews[-1]["open"], pr=pr["number"])
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
        reasons = merge.merge_waits(task.project, pr, reviewed, threads, self._unreviewed_companions(task, pr),
                                    self._approved(task, pr))
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
        self._close_group(task)

    def _close_group(self, task):
        """The parent of a group closes with its last slice."""
        group = next((e for e in queen.all_events(self.store)
                      if e["type"] == "task_split" and task.key in e["children"]), None)
        if group is None or any(self._is_open(c) for c in group["children"] if c != task.key):
            return
        repo, number = group["task"].split("#")
        self.github.close_issue(repo, int(number))
        url = f"https://github.com/{repo}/issues/{number}"
        self.github.set_status(task.project.board, url, DONE)

    def _is_open(self, key):
        repo, number = key.split("#")
        return self.github.issue_is_open(repo, int(number))

    def _group_ahead(self, project, key):
        """Returns the parent of the group that must finish before `key` may start, or None."""
        for event in queen.all_events(self.store):
            if event["type"] != "task_split" or event["project"] != project.name:
                continue
            if key in event["children"]:
                return None
            if any(self._is_open(child) for child in event["children"]):
                return event["task"]
        return None

    # The splitter. It sees each task once, before the queen may release it.

    def _size(self, task, item):
        self._pick(task)
        self._phase(task, "sizing")
        run_id = new_run_id(self.now())
        base = self.github.default_branch(task.repo)
        cwd = self.runner.prepare_review(task.project, task.repo, f"kandu/size-{task.number}", f"origin/{base}")
        prompt = agents.splitter_prompt(task.project, task.repo, task.number, self.local_dir)
        try:
            result = self._agent(task, "splitter", cwd, prompt, agents.splitter_tools(), run_id)
            report = split.parse_report(result.text)
            split.validate_report(report, item.body)
        except StoppedByUser:
            raise
        except (TaskFailed, split.ReportError) as e:
            self._decide(task, "sizing_failed", str(e))
            self.github.comment_issue(task.repo, task.number, "Kandu could not size this task. "
                                      "Move it back to Backlog to try again. The details are in its local log.")
            self._finish(task, NEEDS_INFO, "sizing_failed")
            return
        slices = report.get("slices") or []
        if slices:
            self.store.write_split(task.key, {"title": task.title, "reason": report["reason"], "slices": slices})
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
        for project, item in board_items(self.config, self.github):
            key = f"{item.repo}#{item.number}"
            if item.status != BACKLOG or self.store.last_event("task_sized", task=key) is not None:
                continue
            if self.store.tokens_counted(today, project=project.name) >= project.daily_token_limit:
                continue
            if not self.runner.reachable(project.machine):
                continue
            task = Task(project, item.repo, item.number, item.title, item.url, item.status,
                        branch_for(item.number, item.title))
            self._run_task(task, lambda task=task, item=item: self._size(task, item))
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
            if self.merged[project.name] < END_CHECK_MERGES:
                continue
            try:
                problems = self._check_project(project)
            except (GitHubError, RunnerError) as e:
                problems = [f"the check could not run: {type(e).__name__}"]
            self.store.record("project_checked", project=project.name, passed=not problems, detail="; ".join(problems))

    def _drive(self, task):
        """Moves one task as far as it can go now. Every step is read back from GitHub first."""
        pr = self.github.find_pr(task.repo, task.number)
        if pr is None:
            pr = self._implement(task)
            if pr is None:
                return
            self._warn_if_oversized(task, pr)
        task.branch = pr["headRefName"]  # Worked out from the title, which may have changed since.
        worked = False
        while True:
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

    def _project_view(self, items, backlog):
        """What she sees of every project: fields, not task bodies, so a long Backlog stays cheap."""
        today = self.store.today()
        view = []
        for project in self.config.projects:
            ours = [item for owner, item in items if owner is project]
            view.append({
                "project": project.name, "rules": project.queen_rules,
                "tokens_today": self.store.tokens_counted(today, project=project.name),
                "daily_token_limit": project.daily_token_limit,
                "max_parallel_workers": project.max_parallel_workers,
                "at_work": sum(1 for item in ours if item.status in (READY, *RESUMED)),
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
        items = list(board_items(self.config, self.github))
        backlog = [(project, item) for project, item in items if item.status == BACKLOG and _can_start(self.github, item)
                   and self.store.last_event("task_sized", task=f"{item.repo}#{item.number}") is not None]
        keys = [f"{item.repo}#{item.number}" for _, item in backlog]
        before, news = queen.since_last_look(self.store)
        if not queen.worth_a_look(before, news, keys, self.store.today()):
            return False
        if self._gate() is not None or self._queen_limit() is not None:
            return False
        events = before + news
        tasks = sorted({e["task"] for e in news if e.get("task")})
        retries = {task: queen.retries_used(events, task) for task in tasks}
        prompt = agents.queen_prompt(queen.prompt_events(news), self._project_view(items, backlog), retries,
                                     settings.queen_max_retries, self.config.settings.notify.language,
                                     DEFAULT_LOGS_DIR, self.store.today())
        allowed, denied = agents.queen_tools()
        run_id = new_run_id(self.now())
        self.queen_run = run_id
        self._snapshot("awake")
        try:
            result = self.runner.run_agent(self.config.settings.brain.machine, str(ROOT), prompt, allowed, denied,
                                           run_id, self._hard_stop)
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
            self.store.record("queen_finished", run_id=run_id, brief=f"The queen could not finish this look: {e}.",
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
        items = {f"{item.repo}#{item.number}": (project, item) for project, item in board_items(self.config, self.github)}
        refused, released = [], []
        today = self.store.today()
        for decision in decisions:
            task, action, note = decision["task"], decision["action"], decision["note"]
            if task not in items:
                refused.append(f"{task}: not a task of a running project")
                continue
            project, item = items[task]
            if action == "release":
                why = "it was already released" if task in released else queen.why_no_release(
                    item, _can_start(self.github, item), self.store.tokens_counted(today, project=project.name),
                    project.daily_token_limit, self.store.last_event("task_sized", task=task),
                    self._group_ahead(project, task))
                if why is not None:
                    refused.append(f"release of {task}: {why}")
                    continue
                self.github.set_status(project.board, item.url, READY)
                released.append(task)
            if action == "retry":
                pr = self.github.find_pr(item.repo, item.number)
                why = queen.why_no_retry(events, task, limit, item.status == BLOCKED,
                                         pr is not None and pr["state"] == "MERGED")
                if why is not None:
                    refused.append(f"retry of {task}: {why}")
                    continue
                self.github.set_status(project.board, item.url, READY)
            self.store.record("queen_decided", task=task, project=project.name, action=action, note=note)
            events.append({"type": "queen_decided", "task": task, "action": action})
        return refused, released

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
            tasks = self._reachable_tasks([t for t in read_tasks(self.config, self.github)
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
                pending += self._what_she_released(seen)

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
            tasks = read_tasks(self.config, self.github)
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
        tasks = read_tasks(self.config, self.github)
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
    supervisor = Supervisor(config, store, GitHub(), Runner(config, DEFAULT_LOGS_DIR), read_usage, args.local)
    if args.dry_run:
        print("\n".join(supervisor.dry_run()))
        return 0
    _locked_cycle(supervisor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
