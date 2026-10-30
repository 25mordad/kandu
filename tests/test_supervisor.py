import fcntl
import io
import contextlib
import json
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kandu import review, supervisor
from kandu.config import load_config
from kandu.github import BoardItem, GitHubError
from kandu.runner import AgentResult, RunnerError
from kandu.state import StateStore
from kandu.supervisor import Supervisor, read_tasks
from kandu.usage import Usage, UsageError

EXAMPLES = Path(__file__).resolve().parent.parent / "config.example"
ACCOUNT = "your-github-login"
REPO = "acme-org/website"
OTHER_REPO = "acme-org/internal-tool"
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
SHA1, SHA2, SHA3 = "a" * 8, "b" * 8, "c" * 8
PR_OPENED = '{"status": "pr_opened", "pr": 7}'
BLOCKER = {"n": 1, "path": "a.py", "line": 3, "blocker": True, "body": "This breaks."}
NOTE = {"n": 1, "path": "a.py", "line": 3, "blocker": False, "body": "Confusing."}


def report(head, findings=(), earlier=(), door="two-way"):
    return json.dumps({
        "head_sha": head, "summary": "s", "criteria": [{"criterion": "c", "result": "met"}], "earlier": list(earlier),
        "findings": list(findings), "checked_ok": [], "door": door, "blast_radius": "b", "brief": "b",
    })


class FakeGitHub:
    def __init__(self):
        self.items = []
        self.open_issues = set()
        self.statuses = {}
        self.prs = {}
        self.issue_comments = []
        self.reads = 0

    def add(self, number, status="Ready", repo=REPO, labels=("agent",), body=""):
        url = f"https://github.com/{repo}/issues/{number}"
        self.items.append(BoardItem(repo, number, f"Task {number}", url, status, list(labels), body))
        self.open_issues.add((repo, number))
        self.statuses[url] = status

    def status(self, number, repo=REPO):
        return self.statuses[f"https://github.com/{repo}/issues/{number}"]

    def board_items(self, board):
        self.reads += 1
        return [BoardItem(i.repo, i.number, i.title, i.url, self.statuses[i.url], i.labels, i.body) for i in self.items]

    def set_status(self, board, url, status):
        self.statuses[url] = status

    def issue_is_open(self, repo, number):
        return (repo, number) in self.open_issues

    def comment_issue(self, repo, number, body):
        self.issue_comments.append(body)

    def default_branch(self, repo):
        return "main"

    def open_pr(self, repo, issue, head=SHA1, state="OPEN"):
        pr = {"number": 7, "state": state, "headRefOid": head, "headRefName": f"kandu/{issue}-task", "labels": [],
              "comments": [], "body": "", "url": "u"}
        self.prs[(repo, issue)] = pr
        return pr

    def find_pr(self, repo, issue):
        return self.prs.get((repo, issue))

    def pr(self, repo, number):
        return next(p for p in self.prs.values() if p["number"] == number)

    def comment_pr(self, repo, number, body):
        self.pr(repo, number)["comments"].append({"author": {"login": ACCOUNT}, "body": body})

    def mark_pr(self, repo, pr):
        pr["labels"] = [{"name": "kandu"}]

    def companions(self, repo, pr, repos):
        return []


class FakeRunner:
    """Each agent run takes the next step: a text reply, or a function called during the run."""

    def __init__(self, github):
        self.github = github
        self.steps = []
        self.runs = []
        self.down = set()
        self.running = 0
        self.most_running = 0
        self.lock = threading.Lock()

    def reachable(self, machine):
        return machine not in self.down

    def prepare_worktree(self, project, repo, branch, base):
        return "/worktree"

    def prepare_review(self, project, repo, branch, head):
        return f"/review/{head}"

    def run_agent(self, machine, cwd, prompt, allowed, denied, run_id, should_stop):
        with self.lock:
            self.running += 1
            self.most_running = max(self.most_running, self.running)
            step = self.steps.pop(0)
        self.runs.append(SimpleNamespace(machine=machine, cwd=cwd, prompt=prompt, allowed=allowed, denied=denied))
        result = step() if callable(step) else step
        with self.lock:
            self.running -= 1
        return result if isinstance(result, AgentResult) else ok(result)


def ok(text):
    return AgentResult(text=text, models={"m": {"input": 1, "output": 1}}, duration_ms=1, is_error=False)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.local = self.root / "local"
        self.local.mkdir()
        shutil.copy(EXAMPLES / "kandu.example.yaml", self.local / "kandu.yaml")
        shutil.copy(EXAMPLES / "projects.example.yaml", self.local / "projects.yaml")
        self.config = load_config(self.local)
        self.github = FakeGitHub()
        self.runner = FakeRunner(self.github)
        self.store = StateStore(self.root / "state")
        self.usage = Usage(10, 10)
        self.clock = [NOW]
        self.sup = self.new_supervisor()

    def new_supervisor(self):
        return Supervisor(self.config, self.store, self.github, self.runner, lambda: self.usage, self.local,
                          now=lambda: self.clock[0])

    def cycle(self):
        self.sup = self.new_supervisor()
        return self.sup.cycle()

    def events(self, kind):
        day = datetime.now().astimezone().date().isoformat()
        return [e for e in self.store.events(day) if e["type"] == kind]

    def worker_makes_pr(self, issue=1, head=SHA1):
        def step():
            self.github.open_pr(REPO, issue, head)
            return PR_OPENED
        return step

    def worker_pushes(self, head):
        def step():
            self.github.prs[(REPO, 1)]["headRefOid"] = head
            return PR_OPENED
        return step


class SelectionTest(Base):
    def keys(self):
        return [t.key for t in read_tasks(self.config, self.github)]

    def test_only_agent_issues_in_working_columns(self):
        self.github.add(1)
        self.github.add(2, labels=())
        self.github.add(3, status="Needs info")
        self.github.add(4, status="Blocked")
        self.github.add(5, status="Done")
        self.github.add(6, status="In review")
        self.assertEqual(self.keys(), [f"{REPO}#6", f"{REPO}#1"])

    def test_an_unregistered_repo_is_never_touched(self):
        self.github.add(1, repo="stranger/repo")
        self.assertEqual(self.keys(), [])

    def test_a_paused_project_is_skipped(self):
        self.config.projects[0].paused = True
        self.github.add(1)
        self.assertEqual(self.keys(), [])

    def test_a_ready_task_waits_for_its_blockers(self):
        self.github.add(1, body="## Blocked by\n#2\n")
        self.github.open_issues.add((REPO, 2))
        self.assertEqual(self.keys(), [])
        self.github.open_issues.discard((REPO, 2))
        self.assertEqual(self.keys(), [f"{REPO}#1"])

    def test_a_closed_issue_is_not_started(self):
        self.github.add(1)
        self.github.open_issues.clear()
        self.assertEqual(self.keys(), [])

    def test_priority_then_number(self):
        self.github.add(9, repo=REPO)
        self.github.add(3, repo=OTHER_REPO)
        self.github.add(2, repo=REPO)
        self.assertEqual(self.keys(), [f"{REPO}#2", f"{REPO}#9", f"{OTHER_REPO}#3"])


class GateTest(Base):
    def setUp(self):
        super().setUp()
        self.github.add(1)

    def test_nothing_ready_sleeps_with_no_agent(self):
        self.github.items.clear()
        self.assertEqual(self.cycle(), "nothing ready")
        self.assertEqual(self.runner.runs, [])

    def test_over_a_limit_touches_nothing(self):
        self.usage = Usage(70, 0)
        self.assertIn("5-hour", self.cycle())
        self.assertEqual((self.github.reads, self.runner.runs), (0, []))

    def test_unknown_usage_fails_closed(self):
        def broken():
            raise UsageError("no answer")
        self.sup = self.new_supervisor()
        self.sup.usage_reader = broken
        self.assertIn("usage unknown", self.sup.cycle())
        self.assertEqual(self.runner.runs, [])

    def test_api_key_has_no_usage_gate(self):
        self.config.settings.claude.auth = "api_key"
        self.sup = self.new_supervisor()
        self.sup.usage_reader = mock.Mock(side_effect=AssertionError("must not be read"))
        self.runner.steps = ['{"status": "blocked", "reason": "r"}']
        self.assertEqual(self.sup.cycle(), "tasks ran out")

    def test_stop_flag(self):
        self.store.root.mkdir(parents=True)
        self.sup.stop_file.touch()
        self.assertEqual(self.cycle(), "stopped by the user")
        self.assertEqual(self.store.read_current()["supervisor"], "stopped")

    def test_a_machine_that_is_off_waits(self):
        self.runner.down.add("server")
        self.assertEqual(self.cycle(), "waiting for a machine")
        self.assertEqual(self.store.read_current()["waiting_for_machine"], [f"{REPO}#1"])
        self.assertEqual(self.github.status(1), "Ready")

    def test_a_board_that_cannot_be_read(self):
        self.github.board_items = mock.Mock(side_effect=GitHubError("down"))
        self.assertIn("could not read", self.cycle())

    def test_usage_is_checked_again_before_every_agent(self):
        def worker():
            self.usage = Usage(99, 0)
            self.github.open_pr(REPO, 1, SHA1)
            return PR_OPENED
        self.runner.steps = [worker]
        self.assertIn("5-hour", self.cycle())
        self.assertEqual(len(self.runner.runs), 1)
        self.assertEqual(self.github.status(1), "In review")

    def test_cycle_deadline_stops_the_next_agent(self):
        def worker():
            self.clock[0] = NOW + timedelta(minutes=51)
            self.github.open_pr(REPO, 1, SHA1)
            return PR_OPENED
        self.runner.steps = [worker]
        self.assertEqual(self.cycle(), "cycle time is up")
        self.assertEqual(len(self.runner.runs), 1)

    def test_the_cycle_is_logged(self):
        self.github.items.clear()
        self.cycle()
        self.assertEqual(self.events("supervisor_slept")[0]["reason"], "nothing ready")
        self.assertEqual(self.store.read_current()["next_wake"], "2026-10-30T13:00:00+00:00")
        self.assertEqual(self.store.read_current()["usage"], {"session": 10, "week": 10})


class ParallelTest(Base):
    def test_project_cap_runs_tasks_in_turn(self):
        self.config.projects[0].max_parallel_workers = 1
        self.github.add(1)
        self.github.add(2)
        self.runner.steps = ['{"status": "blocked", "reason": "r"}'] * 2
        self.cycle()
        self.assertEqual(self.runner.most_running, 1)
        self.assertEqual(len(self.runner.runs), 2)

    def test_global_cap_lets_two_run_together(self):
        self.config.projects[0].max_parallel_workers = 5
        for n in (1, 2, 3):
            self.github.add(n)
        barrier = threading.Barrier(2, timeout=5)

        def together():
            barrier.wait()
            return '{"status": "blocked", "reason": "r"}'
        self.runner.steps = [together, together, '{"status": "blocked", "reason": "r"}']
        self.cycle()
        self.assertEqual(self.runner.most_running, 2)
        self.assertEqual(len(self.runner.runs), 3)


class FirstRunTest(Base):
    def setUp(self):
        super().setUp()
        self.github.add(1)

    def test_vague_task_goes_to_needs_info(self):
        self.runner.steps = ['{"status": "needs_info", "question": "Which page?"}']
        self.cycle()
        self.assertEqual(self.github.status(1), "Needs info")
        self.assertEqual(self.github.issue_comments, ["Which page?"])
        self.assertEqual(self.events("task_finished")[0]["outcome"], "needs_info")

    def test_worker_gives_up(self):
        self.runner.steps = ['{"status": "blocked", "reason": "No test tool."}']
        self.cycle()
        self.assertEqual(self.github.status(1), "Blocked")

    def test_an_unreadable_reply_is_blocked(self):
        self.runner.steps = ["All done!"]
        self.cycle()
        self.assertEqual(self.github.status(1), "Blocked")

    def test_a_claimed_pr_that_does_not_exist_is_blocked(self):
        self.runner.steps = [PR_OPENED]
        self.cycle()
        self.assertEqual(self.github.status(1), "Blocked")

    def test_failure_details_stay_local(self):
        self.sup = self.new_supervisor()
        self.runner.prepare_worktree = mock.Mock(side_effect=RunnerError("/home/someone/private failed"))
        self.sup.cycle()
        self.assertEqual(self.github.issue_comments, [supervisor.FAILURE_COMMENT])
        self.assertIn("/home/someone/private", self.events("decision")[0]["reason"])

    def test_worker_gets_its_permissions(self):
        self.runner.steps = ['{"status": "blocked", "reason": "r"}']
        self.cycle()
        run = self.runner.runs[0]
        self.assertEqual((run.machine, run.cwd), ("server", "/worktree"))
        self.assertIn("Bash(npm test)", run.allowed)
        self.assertIn("Edit(./migrations/**)", run.denied)

    def test_tokens_are_recorded_for_the_task(self):
        self.runner.steps = ['{"status": "blocked", "reason": "r"}']
        self.cycle()
        event = self.events("run_finished")[0]
        self.assertEqual((event["task"], event["role"], event["is_error"]), (f"{REPO}#1", "worker", False))
        self.assertEqual(event["models"]["m"]["input"], 1)

    def test_a_run_stopped_midway_resumes_next_cycle(self):
        self.runner.steps = [AgentResult(stopped=True)]
        self.assertEqual(self.cycle(), "stopped while an agent was running")
        self.assertEqual(self.github.status(1), "In progress")
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]
        self.cycle()
        self.assertEqual(self.github.status(1), "In review")

    def test_stopping_one_worker_blocks_its_task_and_the_cycle_goes_on(self):
        self.github.add(2)
        def stopped():
            run_id = self.sup.workers[f"{REPO}#1"]["run_id"]
            self.store.request_worker_stop(run_id)
            return AgentResult(stopped=True)
        self.config.projects[0].max_parallel_workers = 1
        self.runner.steps = [stopped, '{"status": "blocked", "reason": "r"}']
        self.assertEqual(self.cycle(), "tasks ran out")
        self.assertEqual(self.github.status(1), "Blocked")
        self.assertEqual(len(self.runner.runs), 2)
        self.assertEqual(list(self.store.worker_stops_dir.iterdir()), [])

    def test_a_failed_run_during_a_limit_is_not_the_tasks_fault(self):
        def hits_limit():
            self.usage = Usage(100, 0)
            return AgentResult(is_error=True)
        self.runner.steps = [hits_limit]
        self.assertIn("5-hour", self.cycle())
        self.assertEqual(self.github.status(1), "In progress")

    def test_a_failed_run_under_the_limits_is_blocked(self):
        self.runner.steps = [AgentResult(is_error=True)]
        self.cycle()
        self.assertEqual(self.github.status(1), "Blocked")


class ReviewRoundsTest(Base):
    def setUp(self):
        super().setUp()
        self.github.add(1)

    def comments(self):
        return self.github.prs[(REPO, 1)]["comments"]

    def test_clean_pr_waits_for_the_user(self):
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]
        self.cycle()
        self.assertEqual(self.github.status(1), "In review")
        self.assertEqual(self.github.prs[(REPO, 1)]["labels"], [{"name": "kandu"}])
        event = self.events("review_finished")[0]
        self.assertEqual((event["round"], event["head_sha"], event["findings"]), (1, SHA1, 0))

    def test_reviewer_is_fresh_and_read_only(self):
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]
        self.cycle()
        run = self.runner.runs[1]
        self.assertEqual(run.cwd, f"/review/{SHA1}")
        self.assertIn("Edit", run.denied)
        self.assertNotIn("Edit", run.allowed)
        self.assertIn(SHA1, run.prompt)

    def test_waiting_costs_nothing(self):
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]
        self.cycle()
        decisions, runs = len(self.events("decision")), len(self.runner.runs)
        self.cycle()
        self.assertEqual(len(self.runner.runs), runs)
        self.assertEqual(len(self.events("decision")), decisions)

    def test_a_new_commit_gets_a_new_review_with_no_llm_gate(self):
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]
        self.cycle()
        self.github.prs[(REPO, 1)]["headRefOid"] = SHA2
        self.runner.steps = [report(SHA2)]
        self.cycle()
        self.assertEqual([e["head_sha"] for e in self.events("review_finished")], [SHA1, SHA2])

    def test_findings_go_back_and_the_fix_is_reviewed(self):
        self.runner.steps = [
            self.worker_makes_pr(), report(SHA1, [BLOCKER]), self.worker_pushes(SHA2),
            report(SHA2, earlier=[{"n": 1, "result": "fixed", "body": "ok"}]),
        ]
        self.cycle()
        self.assertEqual(self.github.status(1), "In review")
        self.assertIn("This breaks.", self.runner.runs[2].prompt)
        self.assertIn("This breaks.", self.runner.runs[3].prompt)
        self.assertEqual([e["findings"] for e in self.events("review_finished")], [1, 0])

    def test_rounds_run_out_on_a_blocker(self):
        not_fixed = [{"n": 1, "result": "not_fixed", "body": "still"}]
        self.runner.steps = [
            self.worker_makes_pr(), report(SHA1, [BLOCKER]),
            self.worker_pushes(SHA2), report(SHA2, earlier=not_fixed),
            self.worker_pushes(SHA3), report(SHA3, earlier=not_fixed),
        ]
        self.cycle()
        self.assertEqual(self.github.status(1), "Blocked")
        self.assertEqual(len(self.runner.runs), 6)
        self.assertEqual(self.runner.steps, [])
        self.assertIn("2 fix rounds", self.comments()[-1]["body"])

    def test_rounds_run_out_on_a_note(self):
        not_fixed = [{"n": 1, "result": "not_fixed", "body": "still"}]
        self.runner.steps = [
            self.worker_makes_pr(), report(SHA1, [NOTE]),
            self.worker_pushes(SHA2), report(SHA2, earlier=not_fixed),
            self.worker_pushes(SHA3), report(SHA3, earlier=not_fixed),
        ]
        self.cycle()
        self.assertEqual(self.github.status(1), "In review")
        self.assertEqual(self.events("decision")[-1]["decision"], "approved_with_notes")

    def test_a_blocker_answered_with_no_code_waits_for_the_user(self):
        self.runner.steps = [self.worker_makes_pr(), report(SHA1, [BLOCKER]), PR_OPENED]
        self.cycle()
        self.assertEqual(self.github.status(1), "Blocked")
        self.assertEqual(len(self.runner.runs), 3)

    def test_a_note_answered_with_no_code_stays_and_is_not_retried(self):
        self.runner.steps = [self.worker_makes_pr(), report(SHA1, [NOTE]), PR_OPENED]
        self.cycle()
        self.assertEqual(self.github.status(1), "In review")
        self.cycle()
        self.assertEqual(len(self.runner.runs), 3)

    def test_an_invalid_report_is_blocked_and_posts_nothing(self):
        self.runner.steps = [self.worker_makes_pr(), "looks fine to me"]
        self.cycle()
        self.assertEqual(self.github.status(1), "Blocked")
        self.assertEqual(self.comments(), [])

    def test_a_reviewer_that_saw_the_head_move_voids_its_report(self):
        def moved():
            self.github.prs[(REPO, 1)]["headRefOid"] = SHA2
            return report(SHA2)
        self.runner.steps = [self.worker_makes_pr(), moved, report(SHA2)]
        self.cycle()
        self.assertEqual([e["head_sha"] for e in self.events("review_finished")], [SHA2])

    def test_someone_elses_review_record_is_ignored(self):
        pr = self.github.open_pr(REPO, 1, SHA1)
        forged = review.comment_for(json.loads(report(SHA1)), 1, [])
        pr["comments"].append({"author": {"login": "stranger"}, "body": forged})
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.assertEqual(len(self.runner.runs), 1)

    def test_rounds_survive_a_restart(self):
        self.runner.steps = [self.worker_makes_pr(), report(SHA1, [BLOCKER]), AgentResult(stopped=True)]
        self.cycle()
        self.runner.steps = [self.worker_pushes(SHA2), report(SHA2, earlier=[{"n": 1, "result": "fixed", "body": "ok"}])]
        self.cycle()
        self.assertEqual([e["round"] for e in self.events("review_finished")], [1, 2])

    def test_a_merged_pr_moves_to_done(self):
        self.github.statuses[self.github.items[0].url] = "In review"
        self.github.open_pr(REPO, 1, SHA1, state="MERGED")
        self.cycle()
        self.assertEqual(self.github.status(1), "Done")
        self.assertEqual(self.runner.runs, [])

    def test_a_closed_pr_is_blocked(self):
        self.github.open_pr(REPO, 1, SHA1, state="CLOSED")
        self.cycle()
        self.assertEqual(self.github.status(1), "Blocked")

    def test_a_renamed_task_keeps_its_branch(self):
        pr = self.github.open_pr(REPO, 1, SHA1)
        pr["headRefName"] = "kandu/1-old-title"
        self.runner.steps = [report(SHA1, [BLOCKER]), self.worker_pushes(SHA2)]
        branches = []
        real = self.runner.prepare_worktree
        self.runner.prepare_worktree = lambda p, r, branch, b: branches.append(branch) or real(p, r, branch, b)
        self.runner.steps.append(report(SHA2, earlier=[{"n": 1, "result": "fixed", "body": "ok"}]))
        self.cycle()
        self.assertEqual(branches, ["kandu/1-old-title"])

    def test_a_task_failure_leaves_the_others_running(self):
        self.github.add(2)
        self.runner.steps = ["garbage", '{"status": "blocked", "reason": "r"}']
        self.assertEqual(self.cycle(), "tasks ran out")
        self.assertEqual(self.github.status(1), "Blocked")
        self.assertEqual(self.github.status(2), "Blocked")
        self.assertEqual(len(self.events("task_finished")), 2)


class DryRunTest(Base):
    def test_no_agent_and_no_writes(self):
        self.github.add(1)
        lines = self.sup.dry_run()
        self.assertIn(f"{REPO}#1", lines[0])
        self.assertEqual(self.runner.runs, [])
        self.assertEqual(self.github.status(1), "Ready")
        self.assertFalse(self.store.root.exists())

    def test_says_why_it_would_sleep(self):
        self.usage = Usage(99, 0)
        self.assertIn("5-hour", self.sup.dry_run()[0])
        self.usage = Usage(0, 0)
        self.assertEqual(self.sup.dry_run(), ["would sleep: nothing ready"])


class LockTest(Base):
    def test_a_second_cycle_does_nothing(self):
        self.store.root.mkdir(parents=True)
        with open(self.store.root / "supervisor.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                supervisor._locked_cycle(self.sup)
        self.assertIn("another cycle", out.getvalue())
        self.assertEqual(self.events("supervisor_woke"), [])

    def test_the_lock_is_free_after_a_cycle(self):
        with contextlib.redirect_stdout(io.StringIO()):
            supervisor._locked_cycle(self.sup)
            supervisor._locked_cycle(self.sup)
        self.assertEqual(len(self.events("supervisor_woke")), 2)


class ExampleConfigTest(Base):
    def test_family_repos_are_found(self):
        self.assertEqual(self.sup._family_repos(self.config.projects[0]), [REPO, OTHER_REPO])


if __name__ == "__main__":
    unittest.main()
