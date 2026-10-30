import fcntl
import io
import contextlib
import json
from dataclasses import replace
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kandu import agents, queen, review, supervisor
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


def report(head, findings=(), earlier=(), door="two-way", wait=()):
    return json.dumps({
        "head_sha": head, "summary": "s", "criteria": [{"criterion": "c", "result": "met"}], "earlier": list(earlier),
        "findings": list(findings), "checked_ok": [], "door": door, "blast_radius": "b", "brief": "b", "user_brief": "ب",
        "wait_for_user": list(wait),
    })


class FakeGitHub:
    def __init__(self):
        self.items = []
        self.open_issues = set()
        self.statuses = {}
        self.prs = {}
        self.issue_comments = []
        self.reads = 0
        self.threads = []
        self.merges = []

    def add(self, number, status="Ready", repo=REPO, labels=("agent",), body="", priority="Medium", size="Small", deadline=""):
        url = f"https://github.com/{repo}/issues/{number}"
        self.items.append(BoardItem(repo, number, f"Task {number}", url, status, list(labels), body,
                                    priority, "Chore", size, deadline))
        self.open_issues.add((repo, number))
        self.statuses[url] = status

    def status(self, number, repo=REPO):
        return self.statuses[f"https://github.com/{repo}/issues/{number}"]

    def board_items(self, board):
        self.reads += 1
        return [replace(i, status=self.statuses[i.url]) for i in self.items]

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
              "comments": [], "body": "", "url": "u", "isDraft": False, "mergeable": "MERGEABLE", "statusCheckRollup": []}
        self.prs[(repo, issue)] = pr
        return pr

    def find_pr(self, repo, issue):
        return self.prs.get((repo, issue))

    def pr(self, repo, number):
        return next(p for p in self.prs.values() if p["number"] == number)

    def comment_pr(self, repo, number, body):
        self.pr(repo, number)["comments"].append({"author": {"login": ACCOUNT}, "body": body})

    def review_threads(self, repo, number):
        return self.threads

    def merge_pr(self, repo, number, head_sha):
        self.merges.append(head_sha)
        self.pr(repo, number)["state"] = "MERGED"

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
        self.commands = []
        self.failing = set()
        self.running = 0
        self.most_running = 0
        self.lock = threading.Lock()

    def reachable(self, machine):
        return machine not in self.down

    def prepare_worktree(self, project, repo, branch, base):
        return "/worktree"

    def prepare_review(self, project, repo, branch, head):
        return f"/review/{head}"

    def run_command(self, project, cwd, command, log_name, secrets=False):
        self.commands.append((command, secrets))
        return 1 if command in self.failing else 0

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
        self.config.settings.supervisor.queen = False
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


class QueenTest(Base):
    def setUp(self):
        super().setUp()
        self.config.settings.supervisor.queen = True
        self.github.add(1)

    def reply(self, *decisions, brief="What happened."):
        return json.dumps({"brief": brief, "decisions": list(decisions)})

    def retry(self, note="The test needs a fixture."):
        return {"task": f"{REPO}#1", "action": "retry", "note": note}

    def test_a_quiet_cycle_never_wakes_her(self):
        self.github.items.clear()
        self.cycle()
        self.assertEqual(self.runner.runs, [])

    def test_a_merged_task_never_wakes_her(self):
        self.config.projects[0].auto_merge = True
        self.runner.steps = [self.worker_makes_pr(), report(SHA1), '{"result": "pass", "summary": "seen"}']
        self.cycle()
        self.assertEqual(self.github.status(1), "Done")
        self.assertEqual(len(self.runner.runs), 3)

    def test_she_sends_a_stopped_task_back_and_the_next_worker_reads_why(self):
        self.runner.steps = ["not json", self.reply(self.retry("The test needs a fixture."))]
        self.cycle()
        self.assertEqual(self.github.status(1), "Ready")
        self.assertEqual(self.events("queen_decided")[0]["action"], "retry")
        self.assertIn("Blocked", self.runner.runs[1].prompt)
        self.assertNotIn("Edit", self.runner.runs[1].allowed)
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]
        self.cycle()
        self.assertIn("The test needs a fixture.", self.runner.runs[2].prompt)

    def test_her_run_counts_as_a_queen_run_with_no_task(self):
        self.runner.steps = ["not json", self.reply()]
        self.cycle()
        run = [e for e in self.events("run_finished") if e["role"] == "queen"]
        self.assertEqual(len(run), 1)
        self.assertIsNone(run[0]["task"])

    def test_she_cannot_send_back_what_the_user_stopped(self):
        def user_stops():
            self.store.request_worker_stop(self.runner_run_id())
            return "stopped"
        self.runner.steps = [user_stops]
        self.runner_run_id = lambda: self.sup.workers[f"{REPO}#1"]["run_id"]
        self.cycle()
        self.assertEqual(len(self.runner.runs), 1)
        self.github.add(2)
        self.runner.steps = ["not json", self.reply(self.retry())]
        self.cycle()
        self.assertEqual(self.github.status(1), "Blocked")
        self.assertEqual(self.events("queen_decided"), [])
        self.assertIn("Refused", self.events("queen_finished")[0]["brief"])

    def test_she_cannot_send_back_what_is_already_merged(self):
        self.config.projects[0].auto_merge = True
        ask = {"task": f"{REPO}#1", "action": "ask_user", "note": "Should this be fixed by hand?"}
        self.runner.steps = [self.worker_makes_pr(), report(SHA1), '{"result": "fail", "summary": "not there"}',
                             self.reply(self.retry(), ask)]
        self.cycle()
        self.assertEqual(self.github.merges, [SHA1])
        self.assertEqual(self.github.status(1), "Blocked")
        self.assertEqual([e["action"] for e in self.events("queen_decided")], ["ask_user"])
        self.assertIn("already merged", self.events("queen_finished")[0]["brief"])

    def test_a_failure_after_the_merge_cannot_go_back_either(self):
        self.github.open_pr(REPO, 1, SHA1, state="MERGED")
        self.runner.steps = ["not json", self.reply(self.retry())]
        self.cycle()
        self.assertEqual(self.github.status(1), "Blocked")
        self.assertIn("already merged", self.events("queen_finished")[0]["brief"])

    def test_retries_stop_at_the_limit(self):
        self.config.settings.supervisor.queen_max_retries = 1
        self.runner.steps = ["not json", self.reply(self.retry())]
        self.cycle()
        self.runner.steps = ["not json", self.reply(self.retry())]
        self.cycle()
        self.assertEqual(self.github.status(1), "Blocked")
        self.assertIn("retries are used up", self.events("queen_finished")[1]["brief"])

    def test_she_cannot_touch_a_task_outside_the_registry(self):
        self.runner.steps = ["not json", self.reply({"task": "stranger/repo#1", "action": "retry", "note": "n"})]
        self.github.add(1, repo="stranger/repo", status="Blocked")
        self.cycle()
        self.assertEqual(self.github.status(1, repo="stranger/repo"), "Blocked")
        self.assertIn("not a task", self.events("queen_finished")[0]["brief"])

    def test_she_only_retries_a_blocked_task(self):
        self.runner.steps = ["not json", lambda: self.step_done_task()]
        self.cycle()
        self.assertEqual(self.github.status(1), "Done")
        self.assertIn("not Blocked", self.events("queen_finished")[0]["brief"])

    def step_done_task(self):
        self.github.statuses[f"https://github.com/{REPO}/issues/1"] = "Done"
        return self.reply(self.retry())

    def test_a_question_reaches_the_dashboard_and_changes_nothing(self):
        ask = {"task": f"{REPO}#1", "action": "ask_user", "note": "Which reading of the task is right?"}
        self.runner.steps = ["not json", self.reply(ask)]
        self.cycle()
        self.assertEqual(self.github.status(1), "Blocked")
        self.assertEqual(queen.last_report(self.store)["questions"],
                         [{"task": f"{REPO}#1", "question": "Which reading of the task is right?"}])

    def test_over_a_limit_she_waits_for_a_later_cycle(self):
        def spend():
            self.usage = Usage(70, 0)
            return "not json"
        self.runner.steps = [spend]
        self.cycle()
        self.assertEqual(self.events("queen_finished"), [])
        self.usage = Usage(10, 10)
        self.runner.steps = [self.reply()]
        self.cycle()
        self.assertEqual(len(self.events("queen_finished")), 1)

    def test_stop_keeps_her_asleep(self):
        def stop():
            self.store.request_stop()
            return "not json"
        self.runner.steps = [stop]
        self.cycle()
        self.assertEqual(len(self.runner.runs), 1)

    def test_a_bad_reply_changes_nothing_and_is_not_paid_for_again(self):
        self.runner.steps = ["not json", "still not json"]
        self.cycle()
        self.assertEqual(self.github.status(1), "Blocked")
        self.assertIn("could not finish", self.events("queen_finished")[0]["brief"])
        self.cycle()
        self.assertEqual(len(self.runner.runs), 2)

    def test_the_same_merge_wait_is_judged_once(self):
        self.config.projects[0].auto_merge = True
        self.runner.steps = [self.worker_makes_pr(), report(SHA1, wait=["Is this right?"]), self.reply()]
        self.cycle()
        self.assertEqual(len(self.runner.runs), 3)
        self.cycle()
        self.assertEqual(len(self.runner.runs), 3)

    def test_turned_off_she_never_runs(self):
        self.config.settings.supervisor.queen = False
        self.runner.steps = ["not json"]
        self.cycle()
        self.assertEqual(len(self.runner.runs), 1)

    def test_she_merges_nothing(self):
        self.assertFalse({"Edit", "Write"} & set(agents.queen_tools()[0]))
        self.assertFalse(any("merge" in tool or "push" in tool for tool in agents.queen_tools()[0]))


class QueenPacesTest(Base):
    def setUp(self):
        super().setUp()
        self.config.settings.supervisor.queen = True

    def reply(self, *decisions):
        return json.dumps({"brief": "Done.", "decisions": list(decisions)})

    def release(self, number=1, note="Nothing special."):
        return {"task": f"{REPO}#{number}", "action": "release", "note": note}

    def spend(self, tokens, project="website", role="worker"):
        self.store.record("run_finished", task=f"{REPO}#9", project=project, run_id="r-0", role=role,
                          models={"m": {"input": tokens, "output": 0, "cache_read": 10 ** 9, "cache_creation": 0}},
                          duration_ms=1, is_error=False)

    def test_she_wakes_first_and_what_she_releases_is_worked_on_at_once(self):
        self.github.add(1, "Backlog")
        self.config.projects[0].auto_merge = False
        self.runner.steps = [self.reply(self.release(note="Mind the old links.")), self.worker_makes_pr(), report(SHA1)]
        self.cycle()
        self.assertIn("Backlog", self.runner.runs[0].prompt)
        self.assertEqual(self.events("queen_decided")[0]["action"], "release")
        self.assertIn("Mind the old links.", self.runner.runs[1].prompt)

    def test_she_sees_fields_and_never_a_task_body(self):
        self.github.add(1, "Backlog", body="SECRET BODY", priority="High", deadline="2030-01-02")
        self.runner.steps = [self.reply()]
        self.cycle()
        prompt = self.runner.runs[0].prompt
        self.assertIn("2030-01-02", prompt)
        self.assertNotIn("SECRET BODY", prompt)

    def test_a_hundred_tasks_start_only_as_she_releases_them(self):
        for number in range(1, 101):
            self.github.add(number, "Backlog")
        self.config.projects[0].auto_merge = False
        self.runner.steps = [self.reply(self.release(1)), self.worker_makes_pr(1), report(SHA1), self.reply()]
        self.cycle()
        self.assertEqual(sum(self.github.status(n) == "Backlog" for n in range(1, 101)), 99)

    def test_a_large_task_never_leaves_backlog_through_her(self):
        self.github.add(1, "Backlog", size="Large")
        self.runner.steps = [self.reply(self.release())]
        self.cycle()
        self.assertEqual(self.github.status(1), "Backlog")
        self.assertIn("must be split", self.events("queen_finished")[0]["brief"])

    def test_a_large_task_the_user_moved_runs(self):
        self.github.add(1, "Ready", size="Large")
        self.config.projects[0].auto_merge = False
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]
        self.cycle()
        self.assertEqual(len(self.runner.runs), 2)

    def test_she_cannot_release_a_blocked_task(self):
        self.github.add(1, "Backlog")
        self.github.add(2, "Backlog", body="## Blocked by\n#1\n")
        self.runner.steps = [self.reply(self.release(2))]
        self.cycle()
        self.assertEqual(self.github.status(2), "Backlog")
        self.assertIn("Refused", self.events("queen_finished")[0]["brief"])

    def test_she_cannot_release_past_the_project_limit(self):
        self.github.add(1, "Backlog")
        self.config.projects[0].daily_token_limit = 100
        self.spend(100)
        self.runner.steps = [self.reply(self.release())]
        self.cycle()
        self.assertEqual(self.github.status(1), "Backlog")
        self.assertIn("daily token limit", self.events("queen_finished")[0]["brief"])

    def test_the_limit_holds_for_a_task_the_user_moved(self):
        self.github.add(1)
        self.config.projects[0].daily_token_limit = 100
        self.spend(100)
        self.cycle()
        self.assertEqual(self.runner.runs, [])
        self.assertEqual(self.events("decision")[-1]["decision"], "over_budget")

    def test_cache_reads_do_not_count_against_a_limit(self):
        self.github.add(1)
        self.config.projects[0].daily_token_limit = 100
        self.config.projects[0].auto_merge = False
        self.spend(99)
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]
        self.cycle()
        self.assertEqual(len(self.runner.runs), 2)

    def test_at_her_own_limit_nothing_new_starts_and_she_sleeps(self):
        self.github.add(1)
        self.config.settings.supervisor.queen_daily_token_limit = 100
        self.spend(100, project=None, role="queen")
        self.cycle()
        self.assertEqual(self.runner.runs, [])
        self.assertIn("her daily token limit", self.events("supervisor_slept")[0]["reason"])

    def test_she_sees_the_same_backlog_only_once(self):
        self.github.add(1, "Backlog")
        self.runner.steps = [self.reply()]
        self.cycle()
        self.cycle()
        self.assertEqual(len(self.runner.runs), 1)

    def test_a_new_task_in_backlog_wakes_her(self):
        self.github.add(1, "Backlog")
        self.runner.steps = [self.reply()]
        self.cycle()
        self.github.add(2, "Backlog")
        self.runner.steps = [self.reply()]
        self.cycle()
        self.assertEqual(len(self.runner.runs), 2)

    def test_a_finished_task_wakes_her_while_backlog_waits(self):
        self.github.add(1)
        self.github.add(2, "Backlog", size="Large")
        self.config.projects[0].auto_merge = True
        self.runner.steps = [self.reply(), self.worker_makes_pr(), report(SHA1), '{"result": "pass", "summary": "seen"}',
                             self.reply()]
        self.cycle()
        self.assertEqual(len(self.events("queen_finished")), 2)

    def test_one_look_covers_every_project(self):
        self.github.add(1, "Backlog")
        self.github.add(2, "Backlog", repo="acme-org/app")
        self.runner.steps = [self.reply()]
        self.cycle()
        self.assertEqual(len(self.runner.runs), 1)

    def test_the_page_can_see_her_awake_and_asleep(self):
        seen = []
        self.github.add(1, "Backlog")
        def watch():
            seen.append(self.store.read_current()["queen_run"])
            return self.reply()
        self.runner.steps = [watch]
        self.cycle()
        self.assertTrue(seen[0].startswith("r-"))
        self.assertIsNone(self.store.read_current()["queen_run"])

    def test_each_look_shows_what_it_cost(self):
        self.github.add(1, "Backlog")
        self.runner.steps = [self.reply()]
        self.cycle()
        look = next(e for e in self.events("run_finished") if e["role"] == "queen")
        self.assertEqual(look["run_id"], self.events("queen_finished")[0]["run_id"])
        self.assertEqual(queen.last_report(self.store)["tokens"], 2)


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

    def test_a_merged_pr_with_no_deploy_moves_to_done(self):
        self.config.projects[0].deploy = None
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


class MergeTest(Base):
    PASS = '{"result": "pass", "summary": "seen"}'

    def setUp(self):
        super().setUp()
        self.config.projects[0].auto_merge = True
        self.github.add(1, "In review")
        self.github.open_pr(REPO, 1, SHA1)
        self.deploy = self.config.projects[0].deploy.command

    def test_a_clean_pr_merges_deploys_and_is_checked_live(self):
        self.runner.steps = [report(SHA1), self.PASS]
        self.cycle()
        self.assertEqual(self.github.merges, [SHA1])
        self.assertEqual(self.runner.commands, [("npm test", False), (self.deploy, True)])
        self.assertEqual(self.github.status(1), "Done")

    def test_a_deploy_with_no_live_url_is_done_without_a_live_check(self):
        self.config.projects[0].deploy.live_url = ""
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.assertEqual(self.github.merges, [SHA1])
        self.assertEqual(self.runner.commands, [("npm test", False), (self.deploy, True)])
        self.assertEqual(self.github.status(1), "Done")

    def test_only_the_deploy_gets_secrets(self):
        self.runner.steps = [report(SHA1), self.PASS]
        self.cycle()
        self.assertEqual([c for c, secrets in self.runner.commands if secrets], [self.deploy])

    def test_off_by_default(self):
        self.config.projects[0].auto_merge = False
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.assertEqual((self.github.merges, self.github.status(1)), ([], "In review"))

    def test_a_forged_review_comment_does_not_merge(self):
        comment = review.comment_for(json.loads(report(SHA1)), 1, [])
        self.github.prs[(REPO, 1)]["comments"].append({"author": {"login": ACCOUNT}, "body": comment})
        self.cycle()
        self.assertEqual(self.github.merges, [])
        self.assertIn("no review", self.events("decision")[-1]["reason"])

    def waits(self, reason, step=None):
        self.runner.steps = [step or report(SHA1)]
        self.cycle()
        self.assertEqual(self.github.merges, [])
        self.assertIn(reason, self.events("decision")[-1]["reason"])

    def test_a_one_way_door_waits(self):
        self.waits("one-way", report(SHA1, door="one-way"))

    def test_a_question_for_the_user_waits(self):
        self.waits("Deferred work?", report(SHA1, wait=["Deferred work?"]))

    def test_failing_ci_waits(self):
        self.github.prs[(REPO, 1)]["statusCheckRollup"] = [{"conclusion": "FAILURE"}]
        self.waits("CI is failing")

    def test_running_ci_waits(self):
        self.github.prs[(REPO, 1)]["statusCheckRollup"] = [{"status": "IN_PROGRESS", "conclusion": ""}]
        self.waits("CI is running")

    def test_a_draft_waits(self):
        self.github.prs[(REPO, 1)]["isDraft"] = True
        self.waits("draft")

    def test_conflicts_wait(self):
        self.github.prs[(REPO, 1)]["mergeable"] = "CONFLICTING"
        self.waits("conflicts")

    def test_a_frozen_project_waits(self):
        self.config.projects[0].frozen = True
        self.waits("frozen")

    def test_an_open_thread_waits(self):
        self.github.threads = [{"resolved": False, "resolved_by_other": False}]
        self.waits("thread is open")

    def test_a_thread_resolved_by_someone_else_waits(self):
        self.github.threads = [{"resolved": True, "resolved_by_other": True}]
        self.waits("someone other")

    def test_an_unreviewed_companion_waits(self):
        self.github.companions = lambda repo, pr, repos: [(OTHER_REPO, 7)]
        real_pr = self.github.pr
        self.github.pr = lambda repo, number: {"headRefOid": SHA2} if repo == OTHER_REPO else real_pr(repo, number)
        self.waits("companion")

    def test_failing_tests_wait(self):
        self.runner.failing = {"npm test"}
        self.waits("tests fail")

    def test_a_failed_deploy_blocks_and_reverts_nothing(self):
        self.runner.failing = {self.deploy}
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.assertEqual(self.github.status(1), "Blocked")
        self.assertIn("Nothing was reverted", self.github.issue_comments[-1])

    def test_a_failed_live_check_blocks(self):
        self.runner.steps = [report(SHA1), '{"result": "fail", "summary": "The page is missing."}']
        self.cycle()
        self.assertEqual(self.github.status(1), "Blocked")
        self.assertIn("The page is missing.", self.github.issue_comments[-1])

    def test_an_unreadable_live_check_is_blocked(self):
        self.runner.steps = [report(SHA1), "looks fine"]
        self.cycle()
        self.assertEqual(self.github.status(1), "Blocked")

    def test_a_pr_merged_by_the_user_is_deployed_too(self):
        self.config.projects[0].auto_merge = False
        self.github.prs[(REPO, 1)]["state"] = "MERGED"
        self.runner.steps = [self.PASS]
        self.cycle()
        self.assertEqual(self.runner.commands, [(self.deploy, True)])
        self.assertEqual(self.github.status(1), "Done")

    def test_a_restart_after_deploy_does_not_deploy_again(self):
        self.github.prs[(REPO, 1)]["state"] = "MERGED"
        self.usage = Usage(99, 99)
        self.runner.steps = [self.PASS]
        self.cycle()
        self.usage = Usage(10, 10)
        self.store.record("deployed", task=f"{REPO}#1", project="website", run_id="r-1")
        self.cycle()
        self.assertEqual(self.runner.commands, [])
        self.assertEqual(self.github.status(1), "Done")

    def test_a_project_with_no_deploy_is_done_at_merge(self):
        self.config.projects[0].deploy = None
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.assertEqual(self.github.status(1), "Done")

    def test_two_merges_earn_a_check_of_the_project(self):
        self.github.add(2, "In review")
        pr = self.github.open_pr(REPO, 2, SHA2)
        pr["number"] = 8
        self.github.prs[(REPO, 1)]["state"] = self.github.prs[(REPO, 2)]["state"] = "MERGED"
        self.runner.steps = [self.PASS, self.PASS]
        self.runner.failing = {"npm test"}
        self.cycle()
        event = self.events("project_checked")[-1]
        self.assertFalse(event["passed"])
        self.assertIn("tests fail", event["detail"])

    def test_one_merge_earns_no_check(self):
        self.github.prs[(REPO, 1)]["state"] = "MERGED"
        self.runner.steps = [self.PASS]
        self.cycle()
        self.assertEqual(self.events("project_checked"), [])


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
