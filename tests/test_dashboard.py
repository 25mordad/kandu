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

from kandu import dashboard, review
from kandu.config import load_config
from kandu.dashboard import Api, actions_in, dispatch, make_handler, new_events, recommendation
from kandu.github import BoardItem, GitHubError
from kandu.state import EVENT_FIELDS, StateStore

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
        self.api = Api(self.config, self.store, self.github, self.root / "logs", TOKEN, clock=lambda: self.time[0])

    def get(self, target):
        return dispatch(self.api, "GET", target)


class ReadTest(Base):
    def test_state(self):
        self.store.write_current({"supervisor": "asleep", "usage": USAGE})
        status, body = self.get("/api/v1/state")
        self.assertEqual((status, body["supervisor"], body["stop_requested"]), (200, "asleep", False))
        self.assertEqual(body["limits"], {"session": 70, "week": 60})

    def test_state_before_any_cycle(self):
        self.assertEqual(self.get("/api/v1/state")[0], 200)

    def test_queue_shows_only_registered_agent_tasks(self):
        def item(repo, number, status, labels=("agent",)):
            return BoardItem(repo, number, "t", "u", status, list(labels), "")
        self.github.board_items.return_value = [
            item(REPO, 1, "Ready"), item(REPO, 2, "Blocked"), item(REPO, 3, "Ready", ()), item("x/y", 4, "Ready"),
            item(REPO, 5, "Done"),
        ]
        projects = self.get("/api/v1/queue")[1]["projects"]
        self.assertEqual([t["task"] for t in projects["website"]["Ready"]], [f"{REPO}#1"])
        self.assertEqual([t["task"] for t in projects["website"]["Blocked"]], [f"{REPO}#2"])

    def test_queue_reads_one_board_once_and_is_cached(self):
        self.github.board_items.return_value = []
        self.get("/api/v1/queue")
        self.get("/api/v1/queue")
        self.assertEqual(self.github.board_items.call_count, 1)
        self.time[0] = 61
        self.get("/api/v1/queue")
        self.assertEqual(self.github.board_items.call_count, 2)

    def test_github_failure_says_so_without_details(self):
        self.github.board_items.side_effect = GitHubError("private detail")
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


class QueenReportTest(Base):
    def test_state_holds_her_newest_brief_and_questions(self):
        self.assertIsNone(self.api.state({})["queen"])
        self.store.record("queen_finished", run_id="r-1", brief="old", backlog=[])
        self.store.record("queen_decided", task=f"{REPO}#1", project="website", action="ask_user", note="Which?")
        self.store.record("queen_finished", run_id="r-2", brief="new", backlog=[])
        queen = self.api.state({})["queen"]
        self.assertEqual(queen["brief"], "new")
        self.assertEqual(queen["questions"], [{"task": f"{REPO}#1", "question": "Which?"}])


class ReviewPageTest(Base):
    def pr(self, *comments):
        return {"number": 7, "title": "T", "url": "u", "state": "OPEN", "headRefOid": "abc",
                "comments": [{"author": {"login": ACCOUNT}, "body": b} for b in comments]}

    def test_rounds_in_plain_words(self):
        report = {"head_sha": "abc", "criteria": [], "brief": "Short.", "door": "two-way", "blast_radius": "b", "wait_for_user": [],
                  "summary": "s"}
        self.store.record("review_finished", task=f"{REPO}#1", project="site", run_id="r", round=1, head_sha="abc",
                          findings=0, blockers=0, door="two-way", criteria_met=True, wait_for_user=[], user_brief="کوتاه")
        self.github.board_items.return_value = [BoardItem(REPO, 1, "t", "u", "In review", ["agent"], "")]
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
        self.github.board_items.return_value = [BoardItem(REPO, 1, "t", "u", status, ["agent"], "")]

    def test_a_task_no_cycle_would_pick_up_is_not_approved(self):
        self.reviewed(status="Blocked")
        status, body = self.post(f"/api/v1/reviews/{REPO}/1/approve?head=abc")
        self.assertEqual(status, 409)
        self.assertIn("Blocked", body["error"])
        self.assertFalse(self.get(f"/api/v1/reviews/{REPO}/1")[1]["pr"]["can_approve"])
        self.assertIsNone(self.store.last_event("user_approved"))

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
        values = [recommendation(door), recommendation({"open": [], "door": "two-way"}), *dashboard.QUEUE_COLUMNS,
                  "review round 2", "fix round 1", "weekly usage at 93%, limit 60%", "5-hour usage at 9%, limit 8%",
                  *EVENT_FIELDS, *review.DOORS]
        source = (ROOT / "kandu" / "supervisor.py").read_text(encoding="utf-8")
        values += re.findall(r'(?:self\._finish\(task, \w+, |self\._decide\(task, )"(\w+)"', source)
        values += re.findall(r'return "([a-z][^"]*)"', source)
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
