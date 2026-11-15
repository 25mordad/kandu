"""Cleaning up after a merged task (DESIGN §9), with the runner and GitHub mocked: nothing here deletes anything."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from kandu import clean, flights, notify
from kandu.config import NOTIFY_EVENTS
from kandu.github import GitHubError
from kandu.runner import KEPT_CHECKED_OUT, KEPT_UNCOMMITTED, REMOVED, RunnerError
from kandu.state import StateStore

import tempfile as _tempfile
from kandu import ghmeter as _ghmeter
_ghmeter.LOG_DIR = Path(_tempfile.mkdtemp())  # the meter's lines stay out of state/

REPO = "acme-org/website"
SHIPPED = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
AFTER = "2026-11-02T09:00:00Z"
BEFORE = "2026-10-30T09:00:00Z"
SHA = "a" * 40


def merged(number=5, issue=1, branch=None, at=AFTER, state="MERGED", head=SHA, fork=False):
    return {"number": number, "state": state, "headRefName": branch or f"kandu/{issue}-task", "headRefOid": head,
            "isCrossRepository": fork, "mergedAt": at if state == "MERGED" else None}


class Adapter:
    """A pretend notifier's channel: what would be sent is kept here."""

    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


class FakeGitHub:
    def __init__(self):
        self.prs = {REPO: []}
        self.closures = {}  # (repo, issue) -> (closure, closedAt) once the issue is closed
        self.gone = set()   # issues GitHub no longer has, such as a deleted or moved one
        self.tips = {}
        self.deleted = []
        self.fail_tip = None

    def merged_prs(self, repo):
        return [pr for pr in self.prs.get(repo, []) if pr["state"] == "MERGED"]

    def issue_closure(self, repo, number):
        if number in self.gone:
            raise GitHubError(f"no such issue: {repo}#{number}")
        found = self.closures.get((repo, number))
        return found and found[0]

    def issue_closed_at(self, repo, number):
        if number in self.gone:
            raise GitHubError(f"no such issue: {repo}#{number}")
        found = self.closures.get((repo, number))
        return found and found[1]

    def branch_tip(self, repo, branch):
        if self.fail_tip:
            raise GitHubError(self.fail_tip)
        return self.tips.get((repo, branch))

    def delete_branch(self, repo, branch):
        self.deleted.append((repo, branch))
        self.tips.pop((repo, branch), None)


class FakeRunner:
    """What each folder and branch holds; nothing is removed for real."""

    def __init__(self):
        self.down = set()
        self.calls = []
        self.outcomes = {}  # (repo, target) -> REMOVED or the reason it stays
        self.fail = set()   # machines that go away during the clean

    def reachable(self, machine):
        return machine not in self.down

    def clean_task(self, project, repo, branch, folders, merged_sha):
        self.calls.append((project.machine, repo, branch, tuple(folders), merged_sha))
        if project.machine in self.fail:
            raise RunnerError(f"ssh timed out on {project.machine}")
        found = []
        for what, target in [*(("folder", name) for name in folders), ("branch", branch)]:
            outcome = self.outcomes.get((repo, target))
            if outcome is not None:
                found.append((what, target, outcome))
        return found


def project(name="website", machine="box", repos=(REPO,), paused=False):
    return SimpleNamespace(name=name, machine=machine, repos=list(repos), paused=paused)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = StateStore(Path(tmp.name) / "state")
        self.store.clean_since(SHIPPED)
        self.github = FakeGitHub()
        self.runner = FakeRunner()
        self.projects = [project()]

    def clean(self):
        config = SimpleNamespace(projects=self.projects)
        clean.Cleaner(config, self.store, self.github, self.runner).run(self.store.clean_since(SHIPPED))

    def events(self, kind):
        return [e for day in self.store.days() for e in self.store.events(day) if e["type"] == kind]

    def leave(self, target, outcome=REMOVED, repo=REPO):
        self.runner.outcomes[(repo, target)] = outcome


class MergedTaskTest(Base):
    def test_a_merged_task_loses_its_folders_and_both_branches(self):
        self.github.prs[REPO] = [merged()]
        self.github.tips[(REPO, "kandu/1-task")] = SHA
        for target in ("1-task", "1-task-review", "check-1-review", "kandu/1-task"):
            self.leave(target)
        self.clean()
        self.assertEqual(self.runner.calls, [("box", REPO, "kandu/1-task",
                                              ("1-task", "1-task-review", "check-1-review"), SHA)])
        self.assertEqual(self.github.deleted, [(REPO, "kandu/1-task")])
        removed = [(e["task"], e["what"], e["target"]) for e in self.events("cleaned")]
        self.assertEqual(removed, [(f"{REPO}#1", "folder", "1-task"), (f"{REPO}#1", "folder", "1-task-review"),
                                   (f"{REPO}#1", "folder", "check-1-review"), (f"{REPO}#1", "branch", "kandu/1-task"),
                                   (f"{REPO}#1", "remote_branch", "kandu/1-task")])

    def test_it_is_done_once(self):
        self.github.prs[REPO] = [merged()]
        self.leave("1-task")
        self.clean()
        self.clean()
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(len(self.events("clean_finished")), 1)

    def test_a_branch_github_already_removed_is_left_as_it_is(self):
        self.github.prs[REPO] = [merged()]
        self.clean()
        self.assertEqual(self.github.deleted, [])
        self.assertEqual(self.events("cleaned"), [])
        self.assertEqual(len(self.events("clean_finished")), 1)

    def test_a_branch_pushed_to_after_the_merge_stays_on_github(self):
        self.github.prs[REPO] = [merged()]
        self.github.tips[(REPO, "kandu/1-task")] = "b" * 40
        self.clean()
        self.assertEqual(self.github.deleted, [])
        kept = self.events("clean_kept")
        self.assertEqual([(e["what"], e["target"], e["reason"]) for e in kept],
                         [("remote_branch", "kandu/1-task", clean.KEPT_PUSHED_AFTER)])

    def test_a_pr_closed_without_merge_leaves_everything(self):
        self.github.prs[REPO] = [merged(state="CLOSED")]
        self.clean()
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.github.deleted, [])

    def test_a_pr_merged_before_this_shipped_leaves_everything(self):
        self.github.prs[REPO] = [merged(at=BEFORE)]
        self.clean()
        self.assertEqual(self.runner.calls, [])

    def test_a_pr_with_no_merge_time_is_an_old_one(self):
        self.github.prs[REPO] = [merged(at=None)]
        self.clean()
        self.assertEqual(self.runner.calls, [])

    def test_only_kandu_branches_are_touched(self):
        self.github.prs[REPO] = [merged(branch="feature/login"), merged(number=6, branch="kandu/bazras"),
                                 merged(number=7, branch="kandu/1-../../x"), merged(number=8, fork=True)]
        self.clean()
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.github.deleted, [])

    def test_a_paused_project_waits(self):
        self.projects = [project(paused=True)]
        self.github.prs[REPO] = [merged()]
        self.clean()
        self.assertEqual(self.runner.calls, [])
        self.store.pause_project("website")
        self.projects = [project()]
        self.clean()
        self.assertEqual(self.runner.calls, [])


class KeptTest(Base):
    def test_a_folder_with_changes_stays_and_the_rest_goes(self):
        self.github.prs[REPO] = [merged()]
        self.github.tips[(REPO, "kandu/1-task")] = SHA
        self.leave("1-task", KEPT_UNCOMMITTED)
        self.leave("1-task-review")
        self.leave("kandu/1-task", KEPT_CHECKED_OUT)
        self.clean()
        kept = [(e["what"], e["target"], e["reason"]) for e in self.events("clean_kept")]
        self.assertEqual(kept, [("folder", "1-task", KEPT_UNCOMMITTED),
                                ("branch", "kandu/1-task", KEPT_CHECKED_OUT)])
        self.assertEqual([e["target"] for e in self.events("cleaned")], ["1-task-review", "kandu/1-task"])
        self.assertEqual(self.github.deleted, [(REPO, "kandu/1-task")])
        self.assertEqual(clean.kept(self.store, f"{REPO}#1"),
                         [{"what": "folder", "target": "1-task", "reason": KEPT_UNCOMMITTED},
                          {"what": "branch", "target": "kandu/1-task", "reason": KEPT_CHECKED_OUT}])

    def test_nothing_kept_says_nothing(self):
        self.assertEqual(clean.kept(self.store, f"{REPO}#1"), [])


class MachinesTest(Base):
    def test_a_machine_that_cannot_be_reached_is_tried_next_cycle(self):
        self.projects = [project(), project("tool", "far", ["acme-org/tool"])]
        self.github.prs["acme-org/tool"] = [merged(issue=2)]
        self.github.prs[REPO] = [merged()]
        self.runner.down = {"far"}
        self.clean()
        self.assertEqual([c[1] for c in self.runner.calls], [REPO])
        self.runner.down = set()
        self.clean()
        self.assertEqual([c[1] for c in self.runner.calls], [REPO, "acme-org/tool"])

    def test_a_machine_lost_midway_holds_nothing_else_back(self):
        self.projects = [project("tool", "far", ["acme-org/tool"]), project()]
        self.github.prs["acme-org/tool"] = [merged(issue=2), merged(number=6, issue=3)]
        self.github.prs[REPO] = [merged()]
        self.runner.fail = {"far"}
        self.clean()
        self.assertEqual([c[1] for c in self.runner.calls], ["acme-org/tool", REPO])  # its second task waits too
        self.assertEqual([e["task"] for e in self.events("clean_finished")], [f"{REPO}#1"])
        self.runner.fail = set()
        self.clean()
        self.assertEqual(len(self.events("clean_finished")), 3)

    def test_github_refusing_ends_only_that_task_for_this_cycle(self):
        self.github.prs[REPO] = [merged()]
        self.github.fail_tip = "HTTP 502"
        self.clean()
        self.assertEqual(self.events("clean_finished"), [])
        self.github.fail_tip = None
        self.clean()
        self.assertEqual(len(self.events("clean_finished")), 1)


class FlightTest(Base):
    def open_flight(self, error=""):
        key = flights.task_key(f"{REPO}#1")
        self.store.write_flight({"key": key, "kind": "task", "task": f"{REPO}#1", "project": "website",
                                 "session": "s", "started": "t", "url": "", "error": error,
                                 "epoch": datetime.fromisoformat(AFTER).timestamp() + 60})  # opened after the merge
        return key

    def test_while_a_flight_is_open_everything_stays_until_it_closes(self):
        self.github.prs[REPO] = [merged()]
        key = self.open_flight()
        self.clean()
        self.assertEqual(self.runner.calls, [])
        self.store.forget_flight(key)
        self.clean()
        self.assertEqual(len(self.runner.calls), 1)

    def test_a_flight_that_failed_to_start_holds_nothing(self):
        self.github.prs[REPO] = [merged()]
        self.open_flight(error="the session did not start")
        self.clean()
        self.assertEqual(len(self.runner.calls), 1)


class FakeHost:
    """Stands in for tmux: a session runs while its key is in `running`."""

    def __init__(self):
        self.running = set()
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        key, action = Path(command[2]).stem, command[3]
        if action == "has-session":
            return SimpleNamespace(returncode=0 if key in self.running else 1, stdout="", stderr="")
        if action == "kill-server":
            self.running.discard(key)
        return SimpleNamespace(returncode=0, stdout="", stderr="")


MERGED_AT = datetime.fromisoformat(AFTER).timestamp()
MINUTE = 60


class FreeFlightBase(Base):
    """The clock, the session and tmux are pretend, and so is the notifier: no message is sent."""

    def setUp(self):
        super().setUp()
        self.root = Path(self.store.root).parent
        self.host = FakeHost()
        self.time = [MERGED_AT]
        self.flights = flights.Flights(SimpleNamespace(), self.store, self.root / "logs", run=self.host,
                                       clock=lambda: self.time[0], projects_dir=self.root / "projects")
        self.adapter = Adapter()
        self.store.listeners.append(notify.Notifier(self.store, self.adapter, list(NOTIFY_EVENTS), "en").on_event)

    def clean(self):
        config = SimpleNamespace(projects=self.projects)
        clean.Cleaner(config, self.store, self.github, self.runner, flights=self.flights,
                      clock=lambda: self.time[0]).run(self.store.clean_since(SHIPPED))

    def open_flight(self, opened, issue=1):
        task = f"{REPO}#{issue}"
        key = flights.task_key(task)
        self.store.write_flight({"key": key, "kind": "task", "task": task, "project": "website",
                                 "session": f"session-{issue}", "started": "t", "epoch": opened, "url": "u",
                                 "error": ""})
        self.store.flight_socket(key).touch()
        self.host.running.add(key)
        return key

    def write_in_session(self, key, at, output_tokens=7):
        """Something is written in the session's transcript at this time."""
        folder = self.root / "projects" / "here"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{self.store.flight(key)['session']}.jsonl"
        line = {"type": "assistant", "timestamp": "2026-11-02T09:10:00Z",
                "message": {"id": "m", "usage": {"output_tokens": output_tokens}, "content": []}}
        path.write_text(json.dumps(line) + "\n", encoding="utf-8")
        os.utime(path, (at, at))

class FreeFlightTest(FreeFlightBase):
    """A free flight on a merged task ends by itself once its session is quiet."""

    def test_a_flight_opened_before_the_merge_ends_once_nothing_was_written_for_120_minutes(self):
        self.github.prs[REPO] = [merged()]
        key = self.open_flight(MERGED_AT - 60 * MINUTE)
        self.write_in_session(key, MERGED_AT + 10 * MINUTE)
        self.time[0] = MERGED_AT + 130 * MINUTE
        self.clean()
        self.assertIsNone(self.store.flight(key))
        self.assertNotIn(key, self.host.running)
        flown = self.flights.list()["flown"]
        self.assertEqual([(f["task"], f["tokens"]["output"]) for f in flown], [(f"{REPO}#1", 7)])
        ended = [(e["task"], e["reason"]) for e in self.events("flight_ended")]
        self.assertEqual(ended, [(f"{REPO}#1", clean.FLIGHT_QUIET)])
        self.assertEqual(self.adapter.sent, [])

    def test_the_cycle_after_the_flight_ends_cleans_up_the_task(self):
        self.github.prs[REPO] = [merged()]
        self.open_flight(MERGED_AT - 60 * MINUTE)
        self.time[0] = MERGED_AT + 121 * MINUTE
        self.clean()
        self.assertEqual(self.runner.calls, [])
        self.clean()
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(len(self.events("clean_finished")), 1)

    def test_a_flight_written_to_in_the_last_120_minutes_stays_and_is_checked_again(self):
        self.github.prs[REPO] = [merged()]
        key = self.open_flight(MERGED_AT - 60 * MINUTE)
        self.write_in_session(key, MERGED_AT + 30 * MINUTE)
        self.time[0] = MERGED_AT + 149 * MINUTE
        self.clean()
        self.assertIsNotNone(self.store.flight(key))
        self.assertIn(key, self.host.running)
        self.assertEqual((self.events("flight_ended"), self.runner.calls), ([], []))
        self.time[0] = MERGED_AT + 150 * MINUTE
        self.clean()
        self.assertIsNone(self.store.flight(key))
        self.assertEqual(len(self.events("flight_ended")), 1)

    def test_a_flight_opened_after_the_merge_never_ends_by_itself(self):
        self.github.prs[REPO] = [merged()]
        key = self.open_flight(MERGED_AT + MINUTE)
        self.time[0] = MERGED_AT + 30 * 24 * 60 * MINUTE
        self.clean()
        self.assertIsNotNone(self.store.flight(key))
        self.assertIn(key, self.host.running)
        self.assertEqual((self.events("flight_ended"), self.runner.calls), ([], []))

    def test_a_flight_on_a_task_merged_before_the_cleanup_began_is_left_alone(self):
        self.github.prs[REPO] = [merged(at=BEFORE)]
        key = self.open_flight(datetime.fromisoformat(BEFORE).timestamp() - 60 * MINUTE)
        self.time[0] = MERGED_AT + 300 * MINUTE
        self.clean()
        self.assertIsNotNone(self.store.flight(key))


class ClosedTaskFlightTest(FreeFlightBase):
    """A task closed without a merge while its free flight is open: the user hears once, and the flight stays."""

    CLOSED_AT = "2026-11-02T09:00:00Z"

    def setUp(self):
        super().setUp()
        self.github.closures = {}

    def close_issue(self, issue=1, by=None, at=CLOSED_AT):
        self.github.closures[(REPO, issue)] = ({"reason": "NOT_PLANNED", "by": by}, at)

    def tell(self):
        clean.tell_open_flights(self.store, self.github, self.flights)

    def told(self):
        return [e["task"] for e in self.events("flight_left_open")]

    def test_the_user_hears_once_that_the_free_flight_is_still_open(self):
        key = self.open_flight(MERGED_AT - 60 * MINUTE)
        self.close_issue()
        self.time[0] = MERGED_AT + 600 * MINUTE
        self.tell()
        self.clean()
        self.tell()
        self.assertEqual(len(self.adapter.sent), 1)
        message = self.adapter.sent[0]
        self.assertTrue(message.startswith("🟡"))
        self.assertIn(f"{REPO}#1", message)
        self.assertIn("free flight is still open", message)
        self.assertIsNotNone(self.store.flight(key))
        self.assertIn(key, self.host.running)
        self.assertEqual(len(self.events("flight_left_open")), 1)

    def test_a_flight_opened_after_the_close_brings_no_message(self):
        self.open_flight(MERGED_AT + MINUTE)
        self.close_issue()
        self.tell()
        self.assertEqual((self.adapter.sent, self.events("flight_left_open")), ([], []))

    def test_an_open_task_brings_no_message(self):
        self.open_flight(MERGED_AT - 60 * MINUTE)
        self.tell()
        self.assertEqual(self.adapter.sent, [])

    def test_a_task_whose_pr_merged_brings_no_message(self):
        self.github.prs[REPO] = [merged()]
        self.open_flight(MERGED_AT - 60 * MINUTE)
        self.close_issue(by={"repo": REPO, "number": 5, "merged": True})
        self.tell()
        self.assertEqual(self.adapter.sent, [])

    def test_an_issue_github_no_longer_has_brings_no_message_and_the_others_are_still_told(self):
        self.open_flight(MERGED_AT - 60 * MINUTE, issue=1)
        self.open_flight(MERGED_AT - 60 * MINUTE, issue=2)
        self.github.gone.add(1)
        self.close_issue(issue=2)
        self.tell()
        self.assertEqual(self.told(), [f"{REPO}#2"])
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertIn(f"{REPO}#2", self.adapter.sent[0])

    def test_a_closed_issue_without_a_close_time_brings_no_message_and_the_others_are_still_told(self):
        self.open_flight(MERGED_AT - 60 * MINUTE, issue=1)
        self.open_flight(MERGED_AT - 60 * MINUTE, issue=2)
        self.close_issue(issue=1, at=None)
        self.close_issue(issue=2)
        self.tell()
        self.assertEqual(self.told(), [f"{REPO}#2"])
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertIn(f"{REPO}#2", self.adapter.sent[0])

    def test_a_flight_whose_session_has_ended_brings_no_message(self):
        key = self.open_flight(MERGED_AT - 60 * MINUTE)
        self.host.running.discard(key)  # the user left the session; its record stays until the page lands it
        self.close_issue()
        self.tell()
        self.assertEqual((self.adapter.sent, self.events("flight_left_open")), ([], []))


class StartMarkerTest(unittest.TestCase):
    def test_the_first_time_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state")
            first = store.clean_since(SHIPPED)
            later = store.clean_since(datetime(2026, 12, 1, tzinfo=timezone.utc))
            self.assertEqual((first, later), (SHIPPED, SHIPPED))


if __name__ == "__main__":
    unittest.main()
