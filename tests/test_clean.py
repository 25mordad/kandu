"""Cleaning up after a merged task (DESIGN §9), with the runner and GitHub mocked: nothing here deletes anything."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from kandu import clean, flights
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


class FakeGitHub:
    def __init__(self):
        self.prs = {REPO: []}
        self.tips = {}
        self.deleted = []
        self.fail_tip = None

    def merged_prs(self, repo):
        return [pr for pr in self.prs.get(repo, []) if pr["state"] == "MERGED"]

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
                                 "session": "s", "started": "t", "epoch": 0, "url": "", "error": error})
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


class StartMarkerTest(unittest.TestCase):
    def test_the_first_time_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state")
            first = store.clean_since(SHIPPED)
            later = store.clean_since(datetime(2026, 12, 1, tzinfo=timezone.utc))
            self.assertEqual((first, later), (SHIPPED, SHIPPED))


if __name__ == "__main__":
    unittest.main()
