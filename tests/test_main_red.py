"""A project whose `main` is red: its merges wait, no new task starts, one fix task opens, the user hears once."""
import json
import unittest

from kandu import mainred
from kandu.config import NOTIFY_EVENTS
from kandu.notify import Notifier
from test_supervisor import KEY, REPO, SHA1, SHA2, Base, report

MAIN, MAIN2 = "d" * 8, "e" * 8
BROKEN = "tests.test_page.PageTest.test_title"


class Sent:
    def __init__(self):
        self.texts = []

    def send(self, text):
        self.texts.append(text)


class MainRedTest(Base):
    PASS = '{"result": "pass", "summary": "seen"}'

    def setUp(self):
        super().setUp()
        self.config.projects[0].auto_merge = True
        self.store.set_project_setting("website", "watch_main", True)
        self.main = MAIN
        self.github.branch_tip = lambda repo, branch: self.main
        self.main_results = []  # for each run of the tests on `main`: does it pass?
        self.main_runs = []
        self.pr_passes = False
        self.runner.failure_names = [BROKEN]
        self.runner.run_test = self.run_test
        self.github.add(1, "In review")
        self.github.open_pr(REPO, 1, SHA1)

    def run_test(self, project, cwd, log_name):
        if cwd.startswith(f"/review/{self.main}"):
            self.main_runs.append(self.main)
            return 0 if self.main_results.pop(0) else 1
        return 0 if self.pr_passes else 1

    def reasons(self):
        return [e["reason"] for e in self.decisions("merge_waits")]

    def project(self):
        return self.config.projects[0]

    def test_main_is_red_after_two_failing_runs_on_one_commit(self):
        self.main_results = [False, False]
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.assertEqual([(e["run"], e["passed"]) for e in self.events("main_tested")], [(1, False), (2, False)])
        red = mainred.red(self.store, self.project(), REPO)
        self.assertEqual((red["commit"], red["failing"]), (MAIN, [BROKEN]))
        self.assertEqual(self.reasons(), ["main's tests fail"])
        self.assertEqual(self.github.merges, [])

    def test_one_failure_and_one_pass_count_as_passing(self):
        self.main_results = [False, True]
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.assertEqual([e["passed"] for e in self.events("main_tested")], [False, True])
        self.assertIsNone(mainred.red(self.store, self.project(), REPO))
        self.assertEqual(self.reasons(), ["the tests fail on this head"])
        self.assertEqual(self.github.created, [])

    def test_a_first_pass_is_the_only_run(self):
        self.main_results = [True]
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.assertEqual(len(self.main_runs), 1)
        self.assertIsNone(mainred.red(self.store, self.project(), REPO))

    def test_one_main_commit_is_tested_at_most_twice(self):
        self.main_results = [False, False]
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.github.add(2, "In review")
        self.github.open_pr(REPO, 2, SHA2)["number"] = 8
        self.runner.steps = [report(SHA2)]
        self.cycle()
        self.assertEqual(self.main_runs, [MAIN, MAIN])

    def test_every_merge_waits_and_no_new_task_starts_while_main_is_red(self):
        self.pr_passes = True
        self.store.record("main_red", project="website", repo=REPO, commit=MAIN, failing=[BROKEN], since="s")
        self.store.record("main_fix_opened", project="website", repo=REPO, commit=MAIN, task=f"{REPO}#9", url="u")
        self.github.add(2, "Ready")  # a new task: it does not start
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.assertEqual(self.reasons(), ["main's tests fail"])
        self.assertEqual(self.github.merges, [])
        self.assertEqual(self.github.status(2), "Ready")
        self.assertEqual(self.runner.runs[0].prompt.count("Task 2"), 0)

    def test_the_fix_task_starts_and_merges_while_main_is_red(self):
        self.pr_passes = True
        self.store.record("main_red", project="website", repo=REPO, commit=MAIN, failing=[BROKEN], since="s")
        self.store.record("main_fix_opened", project="website", repo=REPO, commit=MAIN, task=KEY, url="u")
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.assertEqual(self.github.merges, [SHA1])

    def test_a_ready_fix_task_is_not_held_back(self):
        self.github.items.clear()
        self.github.add(5, "Ready")
        self.store.record("main_red", project="website", repo=REPO, commit=MAIN, failing=[], since="s")
        self.store.record("main_fix_opened", project="website", repo=REPO, commit=MAIN, task=f"{REPO}#5", url="u")
        self.assertEqual([t.key for t in self.sup._not_while_red(supervisor_tasks(self))], [f"{REPO}#5"])

    def test_one_high_bug_fix_task_per_red_commit_naming_the_failing_tests(self):
        self.main_results = [False, False]
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.assertEqual(len(self.github.created), 1)
        repo, title, body = self.github.created[0]
        self.assertIn(BROKEN, body)
        url = f"https://github.com/{REPO}/issues/901"
        self.assertIn((url, "Priority", "High"), self.github.fields)
        self.assertIn((url, "Kind", "Bug"), self.github.fields)
        self.assertEqual(self.github.statuses[url], "Ready")
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.cycle()
        self.assertEqual(len(self.github.created), 1)
        self.assertEqual(len(self.events("main_fix_opened")), 1)

    def test_a_fix_task_that_failed_to_open_is_opened_by_a_later_look(self):
        from unittest import mock
        from kandu import supervisor
        from kandu.tasks import TaskError
        self.main_results = [False, False]
        self.runner.steps = [report(SHA1)]
        with mock.patch.object(supervisor.tasks, "create_task", side_effect=TaskError("gh is down")):
            try:
                self.cycle()
            except Exception:
                pass
        self.assertIsNotNone(mainred.red(self.store, self.project(), REPO))
        self.assertEqual(self.events("main_fix_opened"), [])
        self.assertEqual(self.github.created, [])
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.assertEqual(len(self.github.created), 1)
        self.assertEqual(len(self.events("main_fix_opened")), 1)
        self.cycle()
        self.assertEqual(len(self.github.created), 1)

    def test_a_later_red_commit_gets_its_own_fix_task(self):
        self.main_results = [False, False]
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.main = MAIN2
        self.main_results = [False, False]
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.assertEqual(len(self.github.created), 2)

    def test_main_passing_again_ends_the_red(self):
        self.main_results = [False, False]
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.main = MAIN2
        self.main_results = [True]
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.assertIsNone(mainred.red(self.store, self.project(), REPO))
        self.assertEqual(len(self.events("main_green")), 1)

    def test_the_user_hears_once_while_main_stays_red(self):
        sent = Sent()
        self.store.listeners[:] = [Notifier(self.store, sent, list(NOTIFY_EVENTS), "en").on_event]
        self.main_results = [False, False]
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.main = MAIN2
        self.main_results = [False, False]
        self.runner.steps = [report(SHA1)]
        self.cycle()
        told = [t for t in sent.texts if "fails its tests" in t]
        self.assertEqual(len(told), 1)
        self.assertIn(BROKEN, told[0])

    def test_the_dashboard_state_names_the_red_main_until_it_passes(self):
        self.store.record("main_red", project="website", repo=REPO, commit=MAIN, failing=[BROKEN], since="s")
        from kandu.dashboard import Api
        board = Api.__new__(Api)
        board.config, board.store = self.config, self.store
        self.assertEqual(board._main_red()["website"]["failing"], [BROKEN])
        self.store.record("main_green", project="website", repo=REPO, commit=MAIN2)
        self.assertEqual(board._main_red(), {})

    def go_red_with_a_waiting_pr(self):
        self.pr_passes = True
        self.store.record("main_red", project="website", repo=REPO, commit=MAIN, failing=[BROKEN], since="s")
        self.store.record("main_fix_opened", project="website", repo=REPO, commit=MAIN, task=f"{REPO}#9", url="u")
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.assertEqual(self.reasons(), ["main's tests fail"])

    def main_passes(self):
        self.store.record("main_green", project="website", repo=REPO, commit=MAIN2)
        self.main = MAIN2

    def test_main_is_brought_into_a_pr_that_waited_and_the_new_head_is_reviewed_then_merged(self):
        self.go_red_with_a_waiting_pr()
        self.main_passes()
        self.runner.on_push = lambda: self.github.prs[(REPO, 1)].update(headRefOid=SHA2)
        self.runner.steps = [report(SHA2), self.PASS]
        self.cycle()
        self.assertEqual(self.runner.merged, [("/worktree", "main")])
        self.assertEqual([e["task"] for e in self.events("main_updated")], [KEY])
        self.assertEqual(len(self.runner.runs), 3)  # the first review, the review of the new head, the live check: no worker
        self.assertEqual(self.github.merges, [SHA2])  # only after its own review

    def test_main_is_brought_in_once_for_one_green(self):
        self.go_red_with_a_waiting_pr()
        self.main_passes()
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.cycle()
        self.assertEqual(len(self.runner.merged), 1)

    def test_a_conflict_when_main_is_brought_in_goes_to_the_update_round(self):
        self.go_red_with_a_waiting_pr()
        self.main_passes()
        self.runner.conflicts = ["a.py"]

        def resolves():
            self.github.prs[(REPO, 1)]["headRefOid"] = SHA2
            return '{"status": "pushed"}'
        self.runner.steps = [resolves, report(SHA2), self.PASS]
        self.cycle()
        self.assertIn("stopped at conflicts in `a.py`", self.runner.runs[1].prompt)
        self.assertEqual(self.runner.runs[1].cwd, "/worktree")
        self.assertEqual(self.github.merges, [SHA2])

    def test_a_pr_that_did_not_wait_on_main_is_left_alone(self):
        self.pr_passes = True
        self.store.record("main_red", project="website", repo=REPO, commit=MAIN, failing=[BROKEN], since="s")
        self.store.record("main_fix_opened", project="website", repo=REPO, commit=MAIN, task=f"{REPO}#9", url="u")
        self.main_passes()  # the PR never had a merge check while main was red
        self.runner.steps = [report(SHA1), self.PASS]
        self.cycle()
        self.assertEqual(self.runner.merged, [])
        self.assertEqual(self.events("main_updated"), [])
        self.assertEqual(self.github.merges, [SHA1])

    def test_the_user_hears_once_that_main_passes_again(self):
        sent = Sent()
        self.store.listeners[:] = [Notifier(self.store, sent, list(NOTIFY_EVENTS), "en").on_event]
        self.main_results = [False, False]
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.main = MAIN2
        self.main_results = [True]
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.cycle()
        told = [t for t in sent.texts if "passes its tests again" in t]
        self.assertEqual(len(told), 1)

    def test_with_the_option_off_main_is_not_brought_in(self):
        self.go_red_with_a_waiting_pr()
        self.main_passes()
        self.store.set_project_setting("website", "watch_main", False)
        self.runner.steps = [report(SHA1), self.PASS]
        self.cycle()
        self.assertEqual(self.runner.merged, [])
        self.assertEqual(self.events("main_updated"), [])

    def test_with_the_option_off_nothing_changes(self):
        self.store.set_project_setting("website", "watch_main", False)
        self.store.record("main_red", project="website", repo=REPO, commit=MAIN, failing=[BROKEN], since="s")
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.assertEqual(self.main_runs, [])
        self.assertEqual(self.events("main_tested"), [])
        self.assertEqual(self.reasons(), ["the tests fail on this head"])
        self.assertEqual(self.github.created, [])

    def test_the_option_ships_off(self):
        self.assertFalse(self.project().watch_main)


def supervisor_tasks(test):
    return test.sup._read_tasks()


if __name__ == "__main__":
    unittest.main()
