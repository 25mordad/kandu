import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kandu import queen, tries
from kandu.config import load_config
from kandu.dashboard import USER_DECIDES, Api, dispatch, task_statuses
from kandu.github import BoardItem
from kandu.state import StateStore

ROOT = Path(__file__).resolve().parent.parent
REPO = "acme-org/website"
TASK = f"{REPO}#1"
TOKEN = "secret-token"


def event(kind, **fields):
    return {"type": kind, "task": TASK, "project": "website"} | fields


def review(blockers=0, criteria_met=True):
    return event("review_finished", blockers=blockers, criteria_met=criteria_met)


def decision(name, reason="r"):
    return event("decision", decision=name, reason=reason)


def status(*events):
    return task_statuses(list(events))[TASK]


class StatusTest(unittest.TestCase):
    def test_a_passed_review_is_fine_and_blockers_are_a_problem(self):
        self.assertEqual((status(review())["state"], status(review())["why"]), ("fine", "review_passed"))
        found = status(review(blockers=2))
        self.assertEqual((found["state"], found["why"], found["detail"]), ("problem", "review_blockers", "2"))
        self.assertEqual(status(review(criteria_met=False))["state"], "problem")

    def test_a_round_that_pushed_nothing_is_stuck_until_something_moves_it(self):
        found = status(review(blockers=1), decision("round_no_commit", "fix round 1: no key"))
        self.assertEqual((found["state"], found["why"], found["detail"]), ("stuck", "round_no_commit", "fix round 1: no key"))
        self.assertEqual(status(review(blockers=1), decision("round_no_commit"), decision("merge_waits", "CI is failing"))["why"],
                         "round_no_commit")  # nothing but the user, the queen or a new head moves it
        self.assertEqual(status(review(blockers=1), decision("round_no_commit"), review())["state"], "fine")
        settled = status(review(blockers=1), decision("round_no_commit"), decision("approved", USER_DECIDES))
        self.assertEqual((settled["state"], settled["why"]), ("fine", "waits_for_you"))

    def test_a_merge_held_by_a_fix_is_a_problem_then_stuck(self):
        once = status(review(), decision("merge_waits", "the PR has conflicts; CI is running"))
        self.assertEqual((once["state"], once["why"], once["detail"]), ("problem", "merge_waits", "the PR has conflicts"))
        twice = status(review(), decision("merge_waits", "the PR has conflicts"), decision("merge_waits", "the PR has conflicts"))
        self.assertEqual((twice["state"], twice["why"]), ("stuck", "merge_held"))
        after_yes = status(review(), event("user_approved"), decision("merge_waits", "CI is failing"))
        self.assertEqual(after_yes["why"], "merge_held")

    def test_what_passes_by_itself_or_waits_for_the_user_is_fine(self):
        pending = status(review(), decision("merge_waits", "CI is running; blocker acme-org/website#2 is not merged yet"))
        self.assertEqual((pending["state"], pending["why"]), ("fine", "merge_pending"))
        door = status(review(), decision("merge_waits", "this is a one-way door"))
        self.assertEqual((door["state"], door["why"]), ("fine", "waits_for_you"))
        self.assertEqual(status(review(), event("user_approved"))["why"], "approved")

    def test_kandus_own_error_counts_its_tries_and_a_stop_is_stuck(self):
        found = status(decision("internal_error", "boom"), decision("internal_error", "boom again"))
        self.assertEqual((found["state"], found["why"], found["detail"]), ("error", "internal_error", "boom again"))
        self.assertEqual(found["tries"], {"this": 2, "of": tries.INTERNAL_TRIES})
        stopped = status(decision("internal_error"), decision("failed", "boom"), event("task_finished", outcome="failed"))
        self.assertEqual((stopped["state"], stopped["why"], stopped["detail"]), ("stuck", "failed", "boom"))
        self.assertEqual(status(decision("internal_error"), review())["tries"], None)

    def test_a_machine_that_dropped_the_connection_costs_no_try(self):
        dropped = decision("internal_error", f"{tries.UNREACHABLE}: server did not answer over SSH")
        found = status(decision("internal_error", "boom"), dropped, dropped)
        self.assertEqual(found["tries"], {"this": 1, "of": tries.INTERNAL_TRIES})

    def test_a_stopped_task_and_a_loop_stop_are_stuck_and_a_resume_clears_them(self):
        for outcome in ("blocked", "needs_info", "sizing_failed", "deploy_failed", "live_check_failed"):
            self.assertEqual(status(event("task_finished", outcome=outcome))["why"], outcome)
        self.assertEqual(status(event("loop_stopped", action="retry", reason="3 retries"))["why"], "loop_stopped")
        resumed = status(event("task_finished", outcome="blocked"), event("user_resumed", to="Ready"))
        self.assertEqual(resumed["state"], "fine")

    def test_a_cleaned_folder_is_said_until_the_task_moves_on(self):
        said = "cancelled a merge an earlier run left unfinished in the task's folder"
        self.assertEqual(status(decision("worktree_cleaned", said))["cleaned"], said)
        self.assertIsNone(status(decision("worktree_cleaned", said), review())["cleaned"])


class ToQueenTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        local = self.root / "local"
        local.mkdir()
        shutil.copy(ROOT / "config.example" / "kandu.example.yaml", local / "kandu.yaml")
        shutil.copy(ROOT / "config.example" / "projects.example.yaml", local / "projects.yaml")
        self.store = StateStore(self.root / "state")
        self.github = mock.Mock()
        self.github.board_items.return_value = [BoardItem(REPO, 1, "t", "u", "In progress", ["agent"], "")]
        self.api = Api(load_config(local), self.store, self.github, self.root / "logs", TOKEN, timer_on=lambda: True,
                       local_dir=local)
        self.api.start_cycle = mock.Mock(return_value=True)

    def post(self, token=TOKEN):
        return dispatch(self.api, "POST", f"/api/v1/tasks/{REPO}/1/to-queen", token)

    def error(self, sized=True):
        if sized:
            self.store.record("task_sized", task=TASK, project="website", size="Small", door="two-way", run_id="r",
                              criteria=1, files=1, slices=[])
        self.store.record("decision", task=TASK, project="website", decision="internal_error", reason="boom")

    def test_it_stops_the_tries_as_a_failure_and_wakes_the_queen(self):
        self.error()
        self.assertEqual(self.post("")[0], 403)
        status, body = self.post()
        self.assertEqual((status, body), (200, {"task": TASK, "to": "Blocked", "cycle_started": True}))
        self.assertEqual(self.store.last_event("task_finished")["outcome"], "failed")
        self.assertIn(tries.STOPPED_BY_USER, self.store.last_event("decision")["reason"])
        self.github.comment_issue.assert_called_once_with(REPO, 1, tries.FAILURE_COMMENT)
        self.assertEqual(self.github.set_status.call_args.args[1:], ("u", "Blocked"))
        flowers = dispatch(self.api, "GET", "/api/v1/state")[1]["flowers"]
        self.assertEqual(flowers[TASK]["status"]["state"], "stuck")

    def test_it_ends_her_pending_step_so_no_cycle_takes_it_again(self):
        self.store.record("queen_decided", task=TASK, project="website", action="rework", note="Rename the helper.")
        self.error()
        self.assertEqual(self.post()[0], 200)
        done = self.store.last_event("queen_step_done")
        self.assertEqual((done["action"], done["result"]), ("rework", "failed"))
        self.assertIn("boom", done["summary"])
        self.assertIsNone(queen.pending_step(queen.all_events(self.store), TASK))

    def test_a_task_never_sized_stops_as_a_sizing_that_failed(self):
        self.error(sized=False)
        self.assertEqual(self.post()[1]["to"], "Needs info")
        self.assertEqual(self.store.last_event("task_finished")["outcome"], "sizing_failed")

    def test_it_is_refused_outside_kandus_own_error_and_while_a_try_runs(self):
        self.assertEqual(self.post()[0], 409)
        self.error()
        self.store.write_current({"workers": [{"task": TASK, "project": "website", "run_id": "r-1"}]})
        status, body = self.post()
        self.assertEqual(status, 409)
        self.assertIn("right now", body["error"])
        self.assertIsNone(self.store.last_event("task_finished"))
        self.github.comment_issue.assert_not_called()


class PageTest(unittest.TestCase):
    def setUp(self):
        self.page = (ROOT / "kandu" / "dashboard.html").read_text(encoding="utf-8")

    def test_every_reason_has_a_sentence_in_both_languages(self):
        whys = ["fine", "problem", "error", "stuck", "review_passed", "review_blockers", "waits_for_you", "approved",
                "merge_pending", "merge_waits", "merge_held", "round_no_commit", "internal_error", "failed", "blocked",
                "needs_info", "sizing_failed", "deploy_failed", "live_check_failed", "loop_stopped", "no_detail"]
        for key in [f"status_{why}" for why in whys] + [f"state_{s}" for s in ("fine", "problem", "error", "stuck")] + [
                "status_tries", "to_queen", "to_queen_hint", "to_queen_done", "cleaned_folder"]:
            self.assertEqual(self.page.count(f'"{key}": '), 2, key)

    def test_the_card_shows_the_status_and_a_stuck_task_is_drawn_as_stuck(self):
        sheet = self.page.split("function sheetBody(")[1].split("\n}\n")[0]
        self.assertIn("statusText(v.status)", sheet)  # with a PR and without one
        self.assertIn('data-to-queen="${esc(v.task)}"', self.page.split("function sheetFoot(")[1].split("\n}\n")[0])
        self.assertIn('if (hit("to-queen")) { toQueen(hit("to-queen")); return; }', self.page)
        self.assertIn('item.mark.status?.state === "stuck" && !working.has(item.task)', self.page)
        for table in ("const COLUMN_ORDER = [\"Stuck\"", '  "Stuck": {swatch:'):
            self.assertIn(table, self.page)


if __name__ == "__main__":
    unittest.main()
