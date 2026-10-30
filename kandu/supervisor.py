"""The main loop (DESIGN §5, §6).

This is code, not an LLM session: everything that needs no judgment runs here,
at no token cost, and still works when Claude is down. Agents are started only
for work that needs judgment. No state is kept between cycles beyond what the
board and the PR hold, so a cycle that is cut off resumes where it stopped.
"""
import argparse
import fcntl
import json
import secrets
import sys
import threading
import traceback
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import agents, review
from .config import DEFAULT_LOCAL_DIR, ConfigError, load_config
from .github import AGENT_LABEL, GitHub, GitHubError, blockers_in, branch_for
from .runner import DEFAULT_LOGS_DIR, Runner
from .state import StateStore
from .usage import UsageError, over_limit, read_usage

READY, IN_PROGRESS, IN_REVIEW = "Ready", "In progress", "In review"
NEEDS_INFO, BLOCKED, DONE = "Needs info", "Blocked", "Done"
RESUMED = (IN_PROGRESS, IN_REVIEW)
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


def new_run_id(now):
    return f"r-{now:%Y%m%d%H%M%S}-{secrets.token_hex(3)}"


def read_tasks(config, github):
    """Returns the tasks the loop may work on: registered, not paused, and not waiting on a blocker."""
    by_board = {}
    for project in config.projects:
        if project.board is not None and not project.paused:
            by_board.setdefault((project.board.owner, project.board.number), []).append(project)
    tasks = []
    for projects in by_board.values():
        by_repo = {repo: project for project in projects for repo in project.repos}
        for item in github.board_items(projects[0].board):
            project = by_repo.get(item.repo)
            if project is None or AGENT_LABEL not in item.labels:
                continue
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
        self.workers = {}
        self.waiting = []
        self._lock = threading.Lock()

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
            raise TaskFailed(f"{role} run {run_id} was stopped by the user")
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
        prompt = agents.worker_prompt(task.project, task.repo, task.number, task.branch, run_id,
                                      findings=findings, pr=pr, local_dir=self.local_dir)
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
                          blockers=len(review.blockers(findings)), door=report["door"])

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

    def _drive(self, task):
        """Moves one task as far as it can go now. Every step is read back from GitHub first."""
        pr = self.github.find_pr(task.repo, task.number)
        if pr is None:
            pr = self._implement(task)
            if pr is None:
                return
        task.branch = pr["headRefName"]  # Worked out from the title, which may have changed since.
        worked = False
        while True:
            if pr["state"] == "MERGED":
                self._finish(task, DONE, "merged")
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
                if outcome is not None:
                    if worked:
                        self._decide(task, outcome, "the user decides next")
                    return
                self._fix(task, pr, reviews)
            worked = True
            pr = self.github.pr(task.repo, pr["number"])

    def _run_task(self, task):
        try:
            self._drive(task)
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
            self._finish(task, BLOCKED, "failed")
        except Exception as e:
            self._decide(task, "failed", f"could not mark the task Blocked: {type(e).__name__}: {e}")

    # One cycle.

    def _reachable_tasks(self, tasks):
        machines = {task.project.machine for task in tasks}
        reachable = {m for m in machines if self.runner.reachable(m)}
        self.waiting = [t.key for t in tasks if t.project.machine not in reachable]
        return [t for t in tasks if t.project.machine in reachable]

    def _schedule(self, tasks):
        limit = self.config.settings.supervisor.max_parallel_workers
        pending = list(tasks)
        running = {}
        with ThreadPoolExecutor(max_workers=limit) as pool:
            while pending or running:
                for task in list(pending):
                    if self.halt_reason is not None:
                        break
                    if len(running) >= limit:
                        break
                    in_project = sum(1 for t in running.values() if t.project is task.project)
                    if in_project >= task.project.max_parallel_workers:
                        continue
                    pending.remove(task)
                    running[pool.submit(self._run_task, task)] = task
                if not running:
                    return
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    del running[future]

    def cycle(self):
        """Runs one cycle and returns why it ended."""
        woke = self.now()
        self.store.record("supervisor_woke")
        self._snapshot("awake")
        settings = self.config.settings.supervisor
        self.deadline = woke + timedelta(minutes=settings.max_cycle_minutes)
        reason = self._gate()
        if reason is None:
            reason = self._work_through_board()
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
