import json
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from kandu import dashboard, review, split
from kandu.config import load_config
from kandu.dashboard import Api, actions_in, dispatch, make_handler, new_events, recommendation
from kandu.github import BoardItem, GitHubError
from kandu.state import EVENT_FIELDS, StateStore
from kandu.tasks import TaskError

ROOT = Path(__file__).resolve().parent.parent
REPO = "acme-org/website"
ACCOUNT = "your-github-login"
TOKEN = "secret-token"
USAGE = {"session": 5, "week": 6}


def assistant_line(*tools):
    content = [{"type": "tool_use", "name": n, "input": i} for n, i in tools]
    return json.dumps({"type": "assistant", "message": {"content": content}})


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        local = self.root / "local"
        local.mkdir()
        shutil.copy(ROOT / "config.example" / "kandu.example.yaml", local / "kandu.yaml")
        shutil.copy(ROOT / "config.example" / "projects.example.yaml", local / "projects.yaml")
        self.config = load_config(local)
        self.store = StateStore(self.root / "state")
        self.github = mock.Mock()
        self.time = [0]
        self.api = Api(self.config, self.store, self.github, self.root / "logs", TOKEN, clock=lambda: self.time[0],
                       timer_on=lambda: True)

    def get(self, target):
        return dispatch(self.api, "GET", target)


class ReadTest(Base):
    def test_state(self):
        self.store.write_current({"supervisor": "asleep", "usage": USAGE})
        status, body = self.get("/api/v1/state")
        self.assertEqual((status, body["supervisor"], body["stop_requested"]), (200, "asleep", False))
        self.assertEqual(body["limits"], {"session": 70, "week": 60})
        self.assertEqual(body["language"], "en")

    def test_state_before_any_cycle(self):
        self.assertEqual(self.get("/api/v1/state")[0], 200)

    def test_queue_shows_only_registered_agent_tasks(self):
        def item(repo, number, status, labels=("agent",)):
            return BoardItem(repo, number, "t", "u", status, list(labels), "")
        self.github.board_queue.return_value = [
            item(REPO, 1, "Ready"), item(REPO, 2, "Blocked"), item(REPO, 3, "Ready", ()), item("x/y", 4, "Ready"),
            item(REPO, 5, "Done"),
        ]
        projects = self.get("/api/v1/queue")[1]["projects"]
        self.assertEqual([t["task"] for t in projects["website"]["Ready"]], [f"{REPO}#1"])
        self.assertEqual([t["task"] for t in projects["website"]["Blocked"]], [f"{REPO}#2"])

    def test_an_accepted_split_stops_waiting(self):
        self.github.board_queue.return_value = [BoardItem(REPO, 1, "Big", "u", "Needs split", ["agent"], ""),
                                                BoardItem(REPO, 2, "Bigger", "u", "Needs split", ["agent"], "")]
        self.store.record("task_split", task=f"{REPO}#1", project="website", children=[])
        tasks = self.get("/api/v1/queue")[1]["projects"]["website"]["Needs split"]
        self.assertEqual([t["task"] for t in tasks], [f"{REPO}#2"])

    def test_queue_reads_one_board_once_and_is_cached(self):
        self.github.board_queue.return_value = []
        self.get("/api/v1/queue")
        self.time[0] = 899
        self.get("/api/v1/queue")
        self.assertEqual(self.github.board_queue.call_count, 1)
        self.time[0] = 900
        self.get("/api/v1/queue")
        self.assertEqual(self.github.board_queue.call_count, 2)
        self.github.board_items.assert_not_called()

    def test_pages_sharing_a_cold_queue_read_the_board_once(self):
        started, go = threading.Event(), threading.Event()

        def slow_read(board):
            started.set()
            go.wait(5)
            return []

        self.github.board_queue.side_effect = slow_read
        first = threading.Thread(target=self.get, args=("/api/v1/queue",))
        first.start()
        started.wait(5)
        second = threading.Thread(target=self.get, args=("/api/v1/queue",))
        second.start()
        go.set()
        first.join(5)
        second.join(5)
        self.assertEqual(self.github.board_queue.call_count, 1)

    def test_a_finished_task_reads_the_board_sooner(self):
        self.github.board_queue.return_value = []
        self.get("/api/v1/queue")
        reads = self.github.board_queue.call_count
        self.time[0] += 61
        self.store.record("task_finished", task=f"{REPO}#1", project="website", outcome="merged")
        self.get("/api/v1/queue")
        self.assertGreater(self.github.board_queue.call_count, reads)

    def test_a_finished_task_reads_the_board_once(self):
        self.github.board_queue.return_value = []
        self.store.record("task_finished", task=f"{REPO}#1", project="website", outcome="merged")
        self.get("/api/v1/queue")
        reads = self.github.board_queue.call_count
        self.time[0] += 61
        self.get("/api/v1/queue")
        self.assertEqual(self.github.board_queue.call_count, reads)

    def test_fresh_queue_is_limited_to_one_read_a_minute(self):
        self.github.board_queue.return_value = []
        self.get("/api/v1/queue")
        self.time[0] = 59
        self.get("/api/v1/queue?fresh=1")
        self.assertEqual(self.github.board_queue.call_count, 1)
        self.time[0] = 60
        self.get("/api/v1/queue?fresh=1")
        self.assertEqual(self.github.board_queue.call_count, 2)

    def test_failed_read_keeps_the_last_answer_and_waits(self):
        self.github.board_queue.return_value = []
        self.assertFalse(self.get("/api/v1/queue")[1]["stale"])
        self.github.board_queue.side_effect = GitHubError("limit")
        self.time[0] = 900
        status, body = self.get("/api/v1/queue")
        self.assertEqual((status, body["stale"]), (200, True))
        self.time[0] = 1199
        self.get("/api/v1/queue?fresh=1")
        self.assertEqual(self.github.board_queue.call_count, 2)
        self.github.board_queue.side_effect = None
        self.time[0] = 1200
        self.assertFalse(self.get("/api/v1/queue")[1]["stale"])

    def test_github_failure_says_so_without_details(self):
        self.github.board_queue.side_effect = GitHubError("private detail")
        status, body = self.get("/api/v1/queue")
        self.assertEqual(status, 502)
        self.assertNotIn("private", body["error"])

    def test_days_tokens_and_finished_tasks(self):
        now = datetime(2026, 10, 30, 12, 0).astimezone()
        tokens = {"m": {"input": 1, "output": 2, "cache_read": 0, "cache_creation": 0}}
        self.store.record("run_finished", now=now, task=f"{REPO}#1", project="website", run_id="r-1", role="worker",
                          models=tokens, duration_ms=1, is_error=False)
        self.store.record("task_finished", now=now, task=f"{REPO}#1", project="website", outcome="merged")
        self.assertEqual(self.get("/api/v1/days")[1], {"days": ["2026-10-30"]})
        day = self.get("/api/v1/days/2026-10-30")[1]
        self.assertEqual(day["tasks"][f"{REPO}#1"]["total"], 3)
        self.assertEqual(day["finished"][0]["outcome"], "merged")

    def test_bad_day(self):
        self.assertEqual(self.get("/api/v1/days/..%2Fx")[0], 404)
        self.assertEqual(self.get("/api/v1/events?day=x")[0], 400)
        self.assertEqual(self.get("/api/v1/events?after=x")[0], 400)

    def test_events_are_paged(self):
        now = datetime(2026, 10, 30, 12, 0).astimezone()
        for _ in range(3):
            self.store.record("supervisor_woke", now=now)
        body = self.get("/api/v1/events?after=1")[1]
        self.assertEqual((len(body["events"]), body["next"]), (2, 3))
        self.assertEqual(self.get("/api/v1/events?day=2026-10-30&after=3")[1]["events"], [])

    def test_events_with_no_days(self):
        self.assertEqual(self.get("/api/v1/events")[1]["events"], [])

    def test_run_shows_actions_and_no_tool_results(self):
        (self.root / "logs").mkdir()
        lines = [assistant_line(("Read", {"file_path": "a.py"}), ("Bash", {"command": "npm test"})),
                 json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": "SECRET"}]}}),
                 "not json"]
        (self.root / "logs" / "r-1.jsonl").write_text("\n".join(lines))
        status, body = self.get("/api/v1/runs/r-1")
        self.assertEqual(status, 200)
        self.assertEqual(body["actions"], [{"tool": "Read", "detail": "a.py"}, {"tool": "Bash", "detail": "npm test"}])
        self.assertNotIn("SECRET", json.dumps(body))

    def test_run_id_cannot_leave_the_logs_folder(self):
        self.assertEqual(self.get("/api/v1/runs/..%2F..%2Fsecret")[0], 404)
        self.assertEqual(self.get("/api/v1/runs/r-none")[0], 404)

    def test_actions_of_odd_lines(self):
        self.assertEqual(actions_in("[]"), [])
        self.assertEqual(actions_in('{"type": "assistant"}'), [])
        self.assertEqual(actions_in('{"type": "assistant", "message": {"content": "text"}}'), [])
        self.assertEqual(len(actions_in(assistant_line(("Bash", {"command": "x" * 999})))[0]["detail"]), 200)


class FlowerMarksTest(Base):
    def task_event(self, kind, **fields):
        self.store.record(kind, task=f"{REPO}#1", project="website", **fields)

    def marks(self):
        return self.get("/api/v1/state")[1]["flowers"].get(f"{REPO}#1")

    def review(self, blockers=0, door="two-way", wait_for_user=()):
        self.task_event("review_finished", run_id="r-1", round=1, head_sha="abc", findings=blockers, blockers=blockers,
                        door=door, criteria_met=True, wait_for_user=list(wait_for_user), user_brief=None)

    def test_size_and_door_come_from_the_log(self):
        self.task_event("task_sized", run_id="r-1", size="Large", files=20, door="two-way", criteria=9, slices=[])
        self.assertEqual(self.marks()["size"], "Large")
        self.review(door="one-way")
        self.assertEqual((self.marks()["size"], self.marks()["door"]), ("Large", "one-way"))

    def test_a_merge_that_waits_is_marked_until_the_task_is_picked_again(self):
        self.task_event("decision", decision="merge_waits", reason="this is a one-way door")
        self.assertEqual(self.marks()["pr"], "waits")
        self.task_event("task_picked")
        self.assertIsNone(self.marks()["pr"])

    def test_a_review_left_to_the_user_waits_too(self):
        self.task_event("decision", decision="approved", reason=dashboard.USER_DECIDES)
        self.assertEqual(self.marks()["pr"], "waits")

    def test_another_decision_does_not_wait(self):
        self.task_event("decision", decision="review_voided", reason="the head moved during the review")
        self.assertIsNone(self.marks()["pr"])

    def test_the_users_yes_shows(self):
        self.task_event("decision", decision="merge_waits", reason="x")
        self.task_event("user_approved", head_sha="abc")
        self.assertEqual(self.marks()["pr"], "approved")

    def test_a_clean_review_ends_sent_back(self):
        self.review(blockers=1)
        self.assertTrue(self.marks()["sent_back"])
        self.review()
        self.assertFalse(self.marks()["sent_back"])

    def test_open_questions_are_counted(self):
        self.review(wait_for_user=["one?", "two?"])
        self.assertEqual(self.marks()["questions"], 2)

    def test_a_slice_is_marked(self):
        self.task_event("slice_created", parent=f"{REPO}#9")
        self.assertTrue(self.marks()["slice"])

    def test_a_task_that_ended_is_left_out(self):
        self.task_event("task_sized", run_id="r-1", size="Small", files=1, door="two-way", criteria=1, slices=[])
        self.task_event("task_finished", outcome="merged")
        self.assertIsNone(self.marks())

    def test_a_task_that_can_come_back_keeps_its_marks(self):
        self.task_event("task_sized", run_id="r-1", size="Small", files=1, door="two-way", criteria=1, slices=[])
        self.task_event("decision", decision="merge_waits", reason="x")
        self.task_event("task_finished", outcome="blocked")
        self.assertEqual((self.marks()["size"], self.marks()["pr"]), ("Small", None))


class QueenReportTest(Base):
    def test_state_holds_her_newest_brief_and_questions(self):
        self.assertIsNone(self.api.state({})["queen"])
        self.store.record("queen_finished", run_id="r-1", brief="old", backlog=[])
        self.store.record("queen_decided", task=f"{REPO}#1", project="website", action="ask_user", note="Which?")
        self.store.record("queen_finished", run_id="r-2", brief="new", backlog=[])
        queen = self.api.state({})["queen"]
        self.assertEqual(queen["brief"], "new")
        self.assertEqual(queen["questions"], [{"task": f"{REPO}#1", "question": "Which?"}])


    def asked(self):
        self.store.record("queen_decided", task=f"{REPO}#1", project="website", action="ask_user", note="Which?")
        self.store.record("queen_finished", run_id="r-1", brief="b", backlog=[])

    def test_the_users_yes_ends_her_question(self):
        self.asked()
        self.store.record("user_approved", task=f"{REPO}#1", project="website", head_sha="abc")
        self.assertEqual(self.api.state({})["queen"]["questions"], [])

    def test_a_cycle_that_moved_the_task_on_ends_her_question(self):
        self.asked()
        self.store.record("task_picked", task=f"{REPO}#1", project="website")
        self.assertEqual(self.api.state({})["queen"]["questions"], [])

    def test_another_tasks_news_leaves_her_question(self):
        self.asked()
        self.store.record("task_picked", task=f"{REPO}#2", project="website")
        self.assertEqual(len(self.api.state({})["queen"]["questions"]), 1)


class TimerStateTest(Base):
    def test_state_says_whether_the_timer_is_on(self):
        self.assertIs(self.api.state({})["timer_on"], True)
        self.api.timer_on = lambda: False
        self.assertIs(self.api.state({})["timer_on"], False)


class ReviewPageTest(Base):
    def pr(self, *comments):
        return {"number": 7, "title": "T", "url": "u", "state": "OPEN", "headRefOid": "abc",
                "comments": [{"author": {"login": ACCOUNT}, "body": b} for b in comments]}

    def test_rounds_in_plain_words(self):
        report = {"head_sha": "abc", "criteria": [], "brief": "Short.", "door": "two-way", "blast_radius": "b", "wait_for_user": [],
                  "summary": "s"}
        self.store.record("review_finished", task=f"{REPO}#1", project="site", run_id="r", round=1, head_sha="abc",
                          findings=0, blockers=0, door="two-way", criteria_met=True, wait_for_user=[], user_brief="کوتاه")
        self.github.board_queue.return_value = [BoardItem(REPO, 1, "t", "u", "In review", ["agent"], "")]
        self.github.find_pr.return_value = self.pr(review.comment_for(report, 1, []))
        body = self.get(f"/api/v1/reviews/{REPO}/1")[1]
        self.assertEqual(body["pr"]["number"], 7)
        self.assertEqual(body["rounds"][0]["brief"], "Short.")
        self.assertEqual(body["rounds"][0]["user_brief"], "کوتاه")
        self.assertEqual(body["rounds"][0]["recommendation"], "ready to merge")

    def test_no_pr_yet(self):
        self.github.find_pr.return_value = None
        self.assertEqual(self.get(f"/api/v1/reviews/{REPO}/1")[1]["pr"], None)

    def test_unregistered_repo_is_not_read(self):
        self.assertEqual(self.get("/api/v1/reviews/other/repo/1")[0], 404)
        self.github.find_pr.assert_not_called()

    def test_recommendation(self):
        blocker = {"blocker": True}
        note = {"blocker": False}
        self.assertIn("blocker", recommendation({"open": [blocker], "door": "one-way"}))
        self.assertIn("hard to undo", recommendation({"open": [], "door": "one-way"}))
        self.assertEqual(recommendation({"open": [], "door": "two-way", "wait_for_user": ["Keep it?"]}),
                         "your decision: the review has a question")
        self.assertIn("notes", recommendation({"open": [note], "door": "two-way"}))
        self.assertEqual(recommendation({"open": [], "door": "two-way"}), "ready to merge")


class ControlTest(Base):
    def post(self, target, token=TOKEN):
        return dispatch(self.api, "POST", target, token)

    def reviewed(self, head="abc", blockers=0, criteria_met=True, status="In review"):
        self.store.record("review_finished", task=f"{REPO}#1", project="website", run_id="r", round=1, head_sha=head,
                          findings=0, blockers=blockers, door="one-way", criteria_met=criteria_met,
                          wait_for_user=["Merge?"], user_brief=None)
        self.github.find_pr.return_value = {"number": 7, "title": "T", "url": "u", "state": "OPEN", "headRefOid": "abc",
                                            "comments": []}
        self.github.board_queue.return_value = [BoardItem(REPO, 1, "t", "u", status, ["agent"], "")]

    def test_a_task_no_cycle_would_pick_up_is_not_approved(self):
        self.reviewed(status="Blocked")
        status, body = self.post(f"/api/v1/reviews/{REPO}/1/approve?head=abc")
        self.assertEqual(status, 409)
        self.assertIn("Blocked", body["error"])
        self.assertFalse(self.get(f"/api/v1/reviews/{REPO}/1")[1]["pr"]["can_approve"])
        self.assertIsNone(self.store.last_event("user_approved"))

    def test_the_page_is_told_why_it_may_not_approve(self):
        self.reviewed()
        self.store.pause_project("website")
        pr = self.get(f"/api/v1/reviews/{REPO}/1")[1]["pr"]
        self.assertEqual((pr["can_approve"], pr["why_not"]), (False, "this project is paused: resume it first"))
        self.store.resume_project("website")
        self.assertIsNone(self.get(f"/api/v1/reviews/{REPO}/1")[1]["pr"]["why_not"])

    def test_approve_saves_the_yes_for_that_head_and_starts_a_cycle(self):
        self.reviewed()
        self.api.start_cycle = mock.Mock(return_value=True)
        status, body = self.post(f"/api/v1/reviews/{REPO}/1/approve?head=abc")
        self.assertEqual((status, body), (200, {"approved": "abc", "cycle_started": True}))
        self.assertIsNotNone(self.store.last_event("user_approved", task=f"{REPO}#1", head_sha="abc"))
        pr = self.get(f"/api/v1/reviews/{REPO}/1")[1]["pr"]
        self.assertTrue(pr["approved"])

    def test_approve_needs_the_token(self):
        self.reviewed()
        self.assertEqual(self.post(f"/api/v1/reviews/{REPO}/1/approve?head=abc", "wrong")[0], 403)
        self.assertIsNone(self.store.last_event("user_approved"))

    def test_a_head_the_user_did_not_see_is_not_approved(self):
        self.reviewed()
        self.assertEqual(self.post(f"/api/v1/reviews/{REPO}/1/approve?head=old")[0], 409)
        self.assertEqual(self.post(f"/api/v1/reviews/{REPO}/1/approve")[0], 409)
        self.assertIsNone(self.store.last_event("user_approved"))

    def test_what_would_stop_the_merge_is_refused_now(self):
        for review_of in ({"head": "other"}, {"blockers": 1}, {"criteria_met": False}):
            self.store = self.api.store = StateStore(self.root / f"state-{len(str(review_of))}")
            self.reviewed(**review_of)
            self.assertEqual(self.post(f"/api/v1/reviews/{REPO}/1/approve?head=abc")[0], 409)
            self.assertFalse(self.get(f"/api/v1/reviews/{REPO}/1")[1]["pr"]["can_approve"])
            self.assertIsNone(self.store.last_event("user_approved"))

    def test_approve_of_an_unregistered_repo(self):
        self.assertEqual(self.post("/api/v1/reviews/other/repo/1/approve?head=abc")[0], 404)

    def test_pause_and_resume_a_project(self):
        self.assertEqual(self.post("/api/v1/projects/website/pause")[0], 200)
        self.assertEqual(self.get("/api/v1/state")[1]["paused_projects"], ["website"])
        self.assertEqual(self.store.last_event("project_paused")["project"], "website")
        self.assertEqual(self.post("/api/v1/projects/website/resume")[0], 200)
        self.assertEqual(self.get("/api/v1/state")[1]["paused_projects"], [])
        self.assertEqual(self.store.last_event("project_resumed")["project"], "website")

    def test_pausing_needs_the_token_and_a_real_project(self):
        self.assertEqual(self.post("/api/v1/projects/website/pause", "")[0], 403)
        self.assertEqual(self.store.paused_projects(), set())
        self.assertEqual(self.post("/api/v1/projects/ghost/pause")[0], 404)

    def test_a_pause_in_the_config_cannot_be_lifted_here(self):
        self.api.config.projects[0].paused = True
        self.assertEqual(self.post("/api/v1/projects/website/resume")[0], 409)
        self.assertEqual(self.get("/api/v1/state")[1]["paused_projects"], ["website"])

    def proposal(self):
        self.store.write_split(f"{REPO}#1", {"title": "t", "reason": "Too big.", "slices": []})

    def test_a_proposal_is_shown_until_it_is_approved(self):
        self.assertIsNone(self.get(f"/api/v1/splits/{REPO}/1")[1]["split"])
        self.proposal()
        self.assertEqual(self.get(f"/api/v1/splits/{REPO}/1")[1]["split"]["reason"], "Too big.")
        self.store.record("task_split", task=f"{REPO}#1", project="website", children=[])
        self.assertIsNone(self.get(f"/api/v1/splits/{REPO}/1")[1]["split"])

    def test_accepting_a_proposal_needs_the_token(self):
        self.proposal()
        with mock.patch("kandu.dashboard.split.approve", return_value=[f"{REPO}#2"]) as approve:
            self.assertEqual(self.post(f"/api/v1/splits/{REPO}/1/approve", "")[0], 403)
            approve.assert_not_called()
            seen = self.get(f"/api/v1/splits/{REPO}/1")[1]["split"]["fingerprint"]
            status, body = self.post(f"/api/v1/splits/{REPO}/1/approve?proposal={seen}")
        self.assertEqual((status, body["children"]), (200, [f"{REPO}#2"]))
        self.assertEqual(approve.call_args.kwargs["seen"], seen)

    def test_a_proposal_tells_its_versions_apart(self):
        self.proposal()
        first = self.get(f"/api/v1/splits/{REPO}/1")[1]["split"]["fingerprint"]
        self.store.write_split(f"{REPO}#1", {"title": "t", "reason": "Changed.", "slices": []})
        self.assertNotEqual(self.get(f"/api/v1/splits/{REPO}/1")[1]["split"]["fingerprint"], first)

    def test_a_refused_split_says_so(self):
        with mock.patch("kandu.dashboard.split.approve", side_effect=TaskError("not waiting")):
            self.assertEqual(self.post(f"/api/v1/splits/{REPO}/1/approve")[0], 409)

    def test_a_proposal_that_fails_its_checks_says_so(self):
        with mock.patch("kandu.dashboard.split.approve", side_effect=split.ReportError("too few slices")):
            self.assertEqual(self.post(f"/api/v1/splits/{REPO}/1/approve")[0], 409)

    def test_a_project_name_may_need_encoding(self):
        self.api.config.projects[0].name = "my site"
        self.assertEqual(self.post("/api/v1/projects/my%20site/pause")[0], 200)
        self.assertEqual(self.store.paused_projects(), {"my site"})

    def test_an_unregistered_repo_has_no_split(self):
        self.assertEqual(self.get("/api/v1/splits/other/repo/1")[0], 404)
        self.assertEqual(self.post("/api/v1/splits/other/repo/1/approve")[0], 404)

    def test_stop_and_resume(self):
        self.assertEqual(self.post("/api/v1/stop")[0], 200)
        self.assertTrue(self.store.stop_file.exists())
        self.assertEqual(self.post("/api/v1/resume")[0], 200)
        self.assertFalse(self.store.stop_file.exists())

    def test_a_wrong_or_missing_token_changes_nothing(self):
        for token in (None, "", "wrong"):
            self.assertEqual(self.post("/api/v1/stop", token)[0], 403)
        self.assertFalse(self.store.stop_file.exists())

    def test_with_no_token_set_controls_are_refused(self):
        self.api.token = None
        self.assertEqual(self.post("/api/v1/stop", "")[0], 403)
        self.assertEqual(self.post("/api/v1/stop", None)[0], 403)

    def test_stop_one_worker(self):
        self.store.write_current({"workers": [{"run_id": "r-1"}]})
        self.assertEqual(self.post("/api/v1/workers/r-1/stop")[0], 200)
        self.assertTrue(self.store.worker_stop_file("r-1").exists())

    def test_stop_only_a_running_worker(self):
        self.assertEqual(self.post("/api/v1/workers/r-1/stop")[0], 404)

    def test_get_cannot_stop(self):
        self.assertEqual(dispatch(self.api, "GET", "/api/v1/stop")[0], 405)
        self.assertFalse(self.store.stop_file.exists())

    def test_unknown_path(self):
        self.assertEqual(self.get("/api/v1/nothing")[0], 404)


class DocsTest(unittest.TestCase):
    def test_every_endpoint_is_documented_and_every_documented_one_exists(self):
        text = (ROOT / "docs" / "API.md").read_text(encoding="utf-8")
        documented = set(re.findall(r"^\| (GET|POST) \| `/api/v1([^`]+)` \|", text, re.MULTILINE))
        self.assertEqual(documented, {(method, path) for method, path, _ in dashboard.ROUTES})

    def test_the_page_uses_only_the_public_api(self):
        page = dashboard.PAGE.read_text(encoding="utf-8")
        self.assertNotIn("state/", page)
        self.assertIn('fetch("/api/v1"', page)


class HiveTest(unittest.TestCase):
    def setUp(self):
        self.page = dashboard.PAGE.read_text(encoding="utf-8")

    def test_every_queue_column_has_a_flower(self):
        flowers = re.search(r"const FLOWERS = \{(.*?)\n\};", self.page, re.DOTALL).group(1)
        for column in dashboard.QUEUE_COLUMNS:
            self.assertIn(f'"{column}":', flowers)

    def test_the_meadow_is_the_first_tab(self):
        tabs = re.findall(r'data-tab="(\w+)"', self.page)
        self.assertEqual(tabs[0], "meadow")
        self.assertIn('tab = "meadow"', self.page)

    def test_every_flower_has_a_sign_and_a_place_in_the_legend(self):
        signs = re.search(r"const SIGNS = \{(.*?)\n\};", self.page, re.DOTALL).group(1)
        kinds = re.search(r"const kinds = \[(.*?)\];", self.page).group(1)
        flowers = re.findall(r'^  "([\w ]+)":', re.search(r"const FLOWERS = \{(.*?)\n\};", self.page, re.DOTALL).group(1), re.MULTILINE)
        for column in flowers:
            self.assertIn(f'"{column}"', kinds)
            if column != "Blocked":  # It wilts, and that is its sign.
                self.assertIn(f'"{column}":', signs)

    def test_technical_debt_is_counted_and_its_weeds_are_not_called_unsized(self):
        self.assertIn('columns["Technical debt"].length', self.page)
        self.assertIn('size || item.column === "Technical debt"', self.page)

    def test_every_action_shows_it_is_working(self):
        self.assertEqual(self.page.count("post("), 2)  # Its definition, and the one call in `act`.
        self.assertIn("() => post(path)", self.page)
        self.assertIn('t("split_creating")', self.page)

    def test_an_unread_board_is_not_called_empty(self):
        self.assertIn('t(queue ? "empty_meadow" : "reading_board")', self.page)

    def test_the_page_shows_the_queen_and_a_task_sent_back(self):
        self.assertIn("state.queen_run", self.page)
        self.assertIn("isFix(worker.phase)", self.page)
        self.assertIn('"sent_back"', self.page)
        self.assertIn("bee_queen", self.page)

    def test_an_open_run_keeps_updating(self):
        self.assertIn("if (shownRun) showRun(shownRun)", self.page)

    def test_the_page_loads_nothing_from_outside(self):
        self.assertEqual(re.findall(r'(?:src|href)="https?://', self.page), [])
        self.assertNotIn("@import", self.page)

    def test_the_font_ships_with_kandu(self):
        self.assertIn(f'url("/{dashboard.FONT.name}")', self.page)
        self.assertTrue(dashboard.FONT.exists())

    def test_a_task_number_stays_whole_in_persian_text(self):
        self.assertIn("\\u2066#${num(", self.page)

    def test_controls_ask_nothing_through_the_browser(self):
        self.assertNotRegex(self.page, r"\b(confirm|alert|prompt)\(")

    def test_motion_stops_for_those_who_ask(self):
        self.assertIn("prefers-reduced-motion: reduce", self.page)


class LanguageTest(unittest.TestCase):
    def setUp(self):
        self.page = dashboard.PAGE.read_text(encoding="utf-8")
        text = re.search(r'<script type="application/json" id="text">(.*?)</script>', self.page, re.DOTALL)
        self.text = json.loads(text.group(1))

    def translate(self, value):
        if value in self.text["values_fa"]:
            return True
        return any(re.search(pattern, value) for pattern, _ in self.text["patterns_fa"])

    def test_both_languages_have_the_same_keys(self):
        self.assertEqual(set(self.text["en"]), set(self.text["fa"]))

    def test_every_key_the_page_uses_exists(self):
        used = set(re.findall(r'data-t="(\w+)"', self.page)) | set(re.findall(r'\bt\("(\w+)"\)', self.page))
        for pair in re.findall(r'\bt\([^()]*\? "(\w+)" : "(\w+)"\)', self.page):
            used |= set(pair)
        self.assertEqual(used - set(self.text["en"]), set())

    def test_english_is_the_default(self):
        self.assertIn('let lang = "en"', self.page)

    def test_what_the_api_says_has_a_persian_form(self):
        door = {"open": [], "door": "one-way"}
        values = [recommendation(door), recommendation({"open": [], "door": "two-way"}),
                  recommendation({"open": [], "door": "two-way", "wait_for_user": ["q"]}), *dashboard.QUEUE_COLUMNS,
                  "review round 2", "fix round 1", "weekly usage at 93%, limit 60%", "5-hour usage at 9%, limit 8%",
                  *EVENT_FIELDS, *review.DOORS]
        source = (ROOT / "kandu" / "supervisor.py").read_text(encoding="utf-8")
        values += re.findall(r'(?:self\._finish\(task, \w+, |self\._decide\(task, )"(\w+)"', source)
        values += re.findall(r'return "([a-z][^"]*)"', source)
        reasons = (ROOT / "kandu" / "dashboard.py").read_text(encoding="utf-8").split("def _why_no_approval")[1].split("    def approve")[0]
        values += [r for r in re.findall(r'return "([a-z][^"]*)"', reasons) if r != "this PR is not open"]
        values.append("its task is in Blocked, so no cycle would pick it up")
        missing = [v for v in values if not self.translate(v)]
        self.assertEqual(missing, [])


class StreamTest(Base):
    def test_new_events_follow_days(self):
        first = datetime(2026, 10, 29, 12, 0).astimezone()
        self.store.record("supervisor_woke", now=first)
        events, position = new_events(self.store, ("2026-10-29", 0))
        self.assertEqual(len(events), 1)
        self.store.record("supervisor_woke", now=datetime(2026, 10, 30, 12, 0).astimezone())
        _, position = new_events(self.store, position)
        events, position = new_events(self.store, position)
        self.assertEqual((len(events), position), (1, ("2026-10-30", 1)))


class ServerTest(Base):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.api))
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_page_and_api_over_real_http(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn("text/html", r.headers["Content-Type"])
            self.assertIn(b"<title>Kandu</title>", r.read())
        with urllib.request.urlopen(self.base + "/api/v1/days") as r:
            self.assertEqual(json.load(r), {"days": []})

    def test_the_font_is_served(self):
        with urllib.request.urlopen(f"{self.base}/{dashboard.FONT.name}") as r:
            self.assertEqual(r.headers["Content-Type"], "font/woff2")
            self.assertEqual(r.read(4), b"wOF2")

    def test_token_header(self):
        request = urllib.request.Request(self.base + "/api/v1/stop", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 403)
        request.add_header("Authorization", f"Bearer {TOKEN}")
        with urllib.request.urlopen(request) as r:
            self.assertEqual(json.load(r), {"stop_requested": True})

    def test_stream_sends_new_events(self):
        with mock.patch("kandu.dashboard.STREAM_POLL_SECONDS", 0.01):
            with socket.create_connection(self.server.server_address, timeout=5) as sock:
                sock.sendall(b"GET /api/v1/stream HTTP/1.1\r\nHost: x\r\n\r\n")
                self.assertIn(b"text/event-stream", sock.recv(1024))
                self.store.record("supervisor_woke")
                data = b""
                deadline = time.monotonic() + 5
                while b"supervisor_woke" not in data and time.monotonic() < deadline:
                    data += sock.recv(4096)
                self.assertIn(b"supervisor_woke", data)
        self.assertIn(b"data: {", data)


if __name__ == "__main__":
    unittest.main()
