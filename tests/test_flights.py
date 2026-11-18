import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kandu import didban, flights
from kandu.config import DidbanBrain, DidbanConfig
from kandu.flights import DIDBAN, ZANBOURDAR, Flights, task_key
from kandu.state import StateStore

SESSION_LINK = "https://claude.ai/code/session_abc123"


class FakeHost:
    """Stands in for tmux and systemd-run."""

    def __init__(self, store):
        self.store = store
        self.commands = []
        self.running = set()
        self.screen = ""
        self.start_fails = False

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if command[0] == "systemd-run":
            if self.start_fails:
                return SimpleNamespace(returncode=1, stdout="", stderr="no bus")
            socket = Path(command[command.index("-S") + 1])
            socket.touch()
            self.running.add(socket.stem)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        key = Path(command[2]).stem
        action = command[3]
        if action == "has-session":
            return SimpleNamespace(returncode=0 if key in self.running else 1, stdout="", stderr="")
        if action == "kill-server":
            self.running.discard(key)
        return SimpleNamespace(returncode=0, stdout=self.screen if action == "capture-pane" else "", stderr="")

    def started_line(self):
        return next(c for c in self.commands if c[0] == "systemd-run")[-1]


def first_said(line):
    """The first message the started line gives the session, read from its file, or None if it waits for the user."""
    words = shlex.split(line)
    after = words[words.index("--append-system-prompt") + 2:]
    if not after:
        return None
    found = re.fullmatch(r'\$\(cat (.+)\)', after[0])
    return Path(shlex.split(found.group(1))[0]).read_text(encoding="utf-8") if found else after[0]


class FlightsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = StateStore(self.root / "state")
        self.host = FakeHost(self.store)
        self.time = [1000.0]
        project_copy = self.root / "website"
        project_copy.mkdir()
        self.project = SimpleNamespace(name="website", machine="brain", path=str(project_copy), repos=["acme-org/website"])
        machine = SimpleNamespace(name="brain", reach="local")
        self.config = SimpleNamespace(machine=lambda name: machine,
                                      settings=SimpleNamespace(dashboard=SimpleNamespace(bind="127.0.0.1", port=8000)))
        self.flights = Flights(self.config, self.store, self.root / "logs", run=self.host, clock=lambda: self.time[0],
                               spawn=lambda work: work(), projects_dir=self.root / "projects")

    def open_task(self, language="fa"):
        self.flights.open_task(self.project, "acme-org/website", 6, language)
        return task_key("acme-org/website#6")

    def prompt(self, key):
        return (self.store.flights_dir / f"{key}.md").read_text(encoding="utf-8")

    def transcript(self, session, usage=None):
        folder = self.root / "projects" / "here"
        folder.mkdir(parents=True, exist_ok=True)
        line = {"type": "assistant", "timestamp": "2026-10-31T10:00:00Z",
                "message": {"id": "m", "usage": usage or {}, "content": []}}
        (folder / f"{session}.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")

    def test_a_task_flight_has_full_access_and_its_language(self):
        key = self.open_task()
        self.assertIn("--dangerously-skip-permissions", self.host.started_line())
        self.assertIn("Speak Persian", self.prompt(key))
        self.flights.close(key)
        self.flights.open_task(self.project, "acme-org/website", 6, "en")
        self.assertIn("Speak English", self.prompt(key))

    def test_didban_keeps_its_limits_and_is_told_the_language(self):
        with mock.patch("kandu.didban.check_brain", return_value=("ok", "config ok")), \
             mock.patch("kandu.didban.waiting", return_value={}):
            self.flights.open_didban("en")
        line = self.host.started_line()
        self.assertIn(didban.BLOCKED_TOOLS, line)
        self.assertNotIn("--dangerously-skip-permissions", line)
        text = self.prompt(DIDBAN)
        self.assertIn("speak English with them", text)
        self.assertIn("without SSH", text)

    def named(self, flag):
        words = shlex.split(self.host.started_line())
        return words[words.index(flag) + 1]

    def test_didban_from_the_dashboard_is_named_kandu_didban(self):
        with mock.patch("kandu.didban.check_brain", return_value=("ok", "config ok")), \
             mock.patch("kandu.didban.waiting", return_value={}):
            self.flights.open_didban("en")
        self.assertEqual((self.named("--name"), self.named("--remote-control")), ("Kandu Didban", "Kandu Didban"))

    def test_zanbourdar_keeps_its_name(self):
        self.flights.open_zanbourdar("en")
        self.assertEqual((self.named("--name"), self.named("--remote-control")),
                         ("Kandu Zanbourdar", "Kandu Zanbourdar"))

    def test_zanbourdar_has_full_access_starts_in_kandu_and_lands_with_the_flights(self):
        self.flights.open_zanbourdar("fa")
        line = self.host.started_line()
        self.assertIn("--dangerously-skip-permissions", line)
        started = next(c for c in self.host.commands if c[0] == "systemd-run")
        self.assertEqual(started[started.index("-c") + 1], str(didban.REPO_DIR))
        text = self.prompt(ZANBOURDAR)
        self.assertIn("# Zanbourdar", text)
        self.assertIn("Speak Persian", text)
        self.transcript(self.store.flight(ZANBOURDAR)["session"], {"output_tokens": 5})
        self.flights.close(ZANBOURDAR)
        self.assertEqual((self.store.flown()[0]["task"], self.store.didban_sessions()), (None, []))

    def test_a_task_flight_prompt_holds_no_issue_text(self):
        key = self.open_task()
        self.assertNotIn("Title", self.prompt(key))

    def test_a_task_flight_reads_its_task_and_boards_from_kandus_api(self):
        text = self.prompt(self.open_task())
        api = "http://127.0.0.1:8000/api/v1"
        self.assertIn(f"{api}/tasks/acme-org/website/6", text)
        self.assertIn(f"{api}/reviews/acme-org/website/6", text)
        self.assertIn(f"{api}/boards", text)
        self.assertNotIn("gh issue view", text)
        self.assertIn("gh project item-edit", text)  # a card change still goes to GitHub

    def test_didban_and_zanbourdar_read_boards_from_kandus_api(self):
        with mock.patch("kandu.didban.check_brain", return_value=("ok", "config ok")), \
             mock.patch("kandu.didban.waiting", return_value={}):
            self.flights.open_didban("en")
        self.flights.open_zanbourdar("en")
        for key in (DIDBAN, ZANBOURDAR):
            self.assertIn("http://127.0.0.1:8000/api/v1/boards", self.prompt(key))

    def test_didban_reads_boards_but_is_not_told_to_change_cards(self):
        with mock.patch("kandu.didban.check_brain", return_value=("ok", "config ok")), \
             mock.patch("kandu.didban.waiting", return_value={}):
            self.flights.open_didban("en")
        text = self.prompt(DIDBAN)
        self.assertIn("http://127.0.0.1:8000/api/v1/boards", text)
        self.assertNotIn("item-edit", text)  # Didban stays off board state

    def test_zanbourdar_may_change_a_card_with_the_ids_from_boards(self):
        self.flights.open_zanbourdar("en")
        self.assertIn("gh project item-edit", self.prompt(ZANBOURDAR))

    def test_the_task_worktree_is_where_a_flight_starts(self):
        worktree = Path(f"{self.project.path}.kandu-worktrees") / "6-fix-it"
        worktree.mkdir(parents=True)
        (worktree.parent / "6-fix-it-review").mkdir()
        self.open_task()
        started = next(c for c in self.host.commands if c[0] == "systemd-run")
        self.assertEqual(started[started.index("-c") + 1], str(worktree))

    def test_a_task_without_a_worktree_starts_in_the_project(self):
        self.open_task()
        started = next(c for c in self.host.commands if c[0] == "systemd-run")
        self.assertEqual(started[started.index("-c") + 1], self.project.path)

    def test_the_link_is_read_from_the_screen(self):
        key = self.open_task()
        self.assertEqual(self.flights.list()["open"][0]["status"], "opening")
        self.host.screen = f"/remote-control is active · Continue at {SESSION_LINK}"
        found = self.flights.list()["open"][0]
        self.assertEqual((found["key"], found["status"], found["url"]), (key, "open", SESSION_LINK))

    def test_the_folder_question_is_answered_yes(self):
        self.open_task()
        self.host.screen = f"❯ No, exit\n  {flights.TRUST}"
        self.flights.list()
        self.assertIn(["Down", "Enter"], [c[-2:] for c in self.host.commands])

    def test_a_second_press_keeps_the_open_session(self):
        self.open_task()
        self.open_task()
        self.assertEqual(sum(c[0] == "systemd-run" for c in self.host.commands), 1)

    def test_no_link_in_time_ends_the_session_and_says_so(self):
        key = self.open_task()
        self.time[0] += flights.WAIT_SECONDS + 1
        found = self.flights.list()["open"][0]
        self.assertEqual((found["status"], found["error"]), ("failed", flights.NO_LINK))
        self.assertNotIn(key, self.host.running)

    def test_a_start_that_fails_says_why_and_can_be_tried_again(self):
        self.host.start_fails = True
        key = self.open_task()
        found = self.flights.list()["open"][0]
        self.assertEqual((found["status"], found["error"]), ("failed", "no bus"))
        self.host.start_fails = False
        self.open_task()
        self.assertEqual(self.flights.list()["open"][0]["status"], "opening")
        self.assertIn(key, self.host.running)

    def test_a_flight_nobody_spoke_to_leaves_no_row(self):
        key = self.open_task()
        self.assertTrue(self.flights.close(key))
        self.assertEqual(self.flights.list(), {"open": [], "flown": []})

    def test_an_ended_flight_goes_to_its_own_list_with_its_tokens(self):
        key = self.open_task()
        self.transcript(self.store.flight(key)["session"], {"output_tokens": 7})
        self.host.running.discard(key)
        listed = self.flights.list()
        self.assertEqual(listed["open"], [])
        self.assertEqual((listed["flown"][0]["task"], listed["flown"][0]["tokens"]["output"]), ("acme-org/website#6", 7))
        self.assertEqual(self.store.didban_sessions(), [])

    def test_an_ended_didban_goes_to_didbans_list(self):
        with mock.patch("kandu.didban.check_brain", return_value=("ok", "config ok")), \
             mock.patch("kandu.didban.waiting", return_value={}):
            self.flights.open_didban("fa")
        self.transcript(self.store.flight(DIDBAN)["session"], {"output_tokens": 7})
        self.flights.close(DIDBAN)
        self.assertEqual(self.flights.list()["flown"], [])
        self.assertEqual(self.store.didban_sessions()[0]["tokens"]["output"], 7)

    def test_closing_what_is_not_there_says_so(self):
        self.assertFalse(self.flights.close("t-none"))

    def test_a_start_error_does_not_leave_the_key_locked(self):
        def broken(command, **kwargs):
            if command[0] == "systemd-run":
                raise subprocess.TimeoutExpired(command, 30)
            return self.host(command, **kwargs)
        self.flights.run = broken
        self.open_task()
        self.assertEqual(self.flights.list()["open"][0]["status"], "failed")


class ReachTest(unittest.TestCase):
    def test_didban_on_the_brain_uses_no_ssh(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="ok", stderr="")
            didban.on_brain(DidbanBrain(reach="local", install_dir="/opt/kandu"), "kandu.config")
        self.assertEqual(run.call_args.args[0][:2], ["sh", "-c"])

    def test_nothing_says_a_language_unless_it_was_chosen(self):
        text = didban.system_prompt(DidbanConfig(brain=DidbanBrain(reach="ssh server", install_dir="~/k")), "ok")
        self.assertNotIn("from the dashboard in", text.split("## This setup")[1])
        self.assertIn("`ssh server`", text)


if __name__ == "__main__":
    unittest.main()


class FakeCopy:
    """Stands in for Kandu's copy of GitHub: the board's cards and the PRs."""

    def __init__(self):
        self.column = "In review"
        self.pr = None
        self.open_issue = True  # None: the copy does not hold the issue

    def board_items(self, board):
        return [SimpleNamespace(repo="acme-org/website", number=6, status=self.column)]

    def find_pr(self, repo, issue):
        return self.pr

    def copied_issue_open(self, repo, number):
        return self.open_issue


def open_pr(**fields):
    return {"number": 12, "state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": "abc",
            "statusCheckRollup": [{"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}],
            "title": "ISSUE-TITLE-TEXT", "body": "PR-BODY-TEXT", "comments": [{"body": "COMMENT-TEXT"}]} | fields


API = "http://127.0.0.1:8000/api/v1"
READ_ISSUE = f"curl -s {API}/tasks/acme-org/website/6"
READ_PR = f"curl -s {API}/reviews/acme-org/website/6"


class FlightOnATaskTest(unittest.TestCase):
    """A flight on a task opens with its state and a summary, and closes by itself once its task is done."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = StateStore(self.root / "state")
        self.host = FakeHost(self.store)
        self.time = [1000.0]
        project_copy = self.root / "website"
        project_copy.mkdir()
        self.project = SimpleNamespace(name="website", machine="brain", path=str(project_copy),
                                       repos=["acme-org/website"], board=SimpleNamespace(owner="acme-org", number=1))
        machine = SimpleNamespace(name="brain", reach="local")
        self.config = SimpleNamespace(machine=lambda name: machine,
                                      settings=SimpleNamespace(dashboard=SimpleNamespace(bind="127.0.0.1", port=8000)))
        self.copy = FakeCopy()
        self.flights = Flights(self.config, self.store, self.root / "logs", run=self.host, clock=lambda: self.time[0],
                               spawn=lambda work: work(), projects_dir=self.root / "projects", github=self.copy)

    def first_message(self):
        """What the session is started with, or None if it waits for the user."""
        return first_said(self.host.started_line())

    def record(self, event_type, **fields):
        self.store.record(event_type, task="acme-org/website#6", project="website", **fields)

    def review(self, blockers=0, criteria_met=True, wait_for_user=(), door="two-way"):
        self.record("review_finished", run_id="r-1", round=1, head_sha="abc", findings=blockers, blockers=blockers,
                    door=door, criteria_met=criteria_met, wait_for_user=list(wait_for_user), user_brief="")

    def open_task(self, language="fa"):
        self.flights.open_task(self.project, "acme-org/website", 6, language)
        return task_key("acme-org/website#6")

    def prompt(self, key):
        return (self.store.flights_dir / f"{key}.md").read_text(encoding="utf-8")

    def transcript(self, session, usage=None):
        folder = self.root / "projects" / "here"
        folder.mkdir(parents=True, exist_ok=True)
        line = {"type": "assistant", "timestamp": "2026-10-31T10:00:00Z",
                "message": {"id": "m", "usage": usage or {}, "content": []}}
        (folder / f"{session}.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")

    def idle(self, session, seconds):
        """The session's last activity was `seconds` before now."""
        path = self.root / "projects" / "here" / f"{session}.jsonl"
        os.utime(path, (self.time[0] - seconds, self.time[0] - seconds))

    def started(self, key):
        """Opens the session for real: past the time its start may take."""
        self.host.screen = SESSION_LINK
        self.flights.list()
        self.time[0] += flights.WAIT_SECONDS + 1
        return self.store.flight(key)["session"]

    # Closing by itself.

    def test_a_task_session_whose_issue_closes_is_left_to_the_cleanup_and_the_cycle(self):
        key = self.open_task()
        session = self.started(key)
        self.transcript(session, {"output_tokens": 7})
        self.idle(session, 0)
        self.copy.open_issue = False
        self.assertEqual(self.flights.close_finished(), [])
        self.time[0] += 3 * 24 * 3600
        self.assertEqual(self.flights.close_finished(), [])
        self.assertIn(key, self.host.running)
        self.assertEqual([r["key"] for r in self.flights.list()["open"]], [key])

    def test_a_task_session_opened_on_a_closed_issue_never_closes_by_itself(self):
        self.copy.open_issue = False
        key = self.open_task()
        session = self.started(key)
        self.transcript(session, {"output_tokens": 7})
        self.idle(session, 0)
        self.time[0] += 3 * 24 * 3600
        self.assertEqual(self.flights.close_finished(), [])
        self.assertIn(key, self.host.running)

    def test_a_task_session_closes_by_itself_after_twelve_idle_hours(self):
        key = self.open_task()
        session = self.started(key)
        self.transcript(session, {"output_tokens": 3})
        self.idle(session, 0)
        self.time[0] += 12 * 3600 - 60
        self.assertEqual(self.flights.close_finished(), [])
        self.time[0] += 120
        self.assertEqual(self.flights.close_finished(), [key])
        self.assertEqual(self.flights.list()["flown"][0]["tokens"]["output"], 3)

    def test_a_busy_task_session_stays_open(self):
        key = self.open_task()
        session = self.started(key)
        self.transcript(session, {"output_tokens": 3})
        for _ in range(5):  # a day and more, with activity every few hours
            self.time[0] += 5 * 3600
            self.idle(session, 0)
            self.assertEqual(self.flights.close_finished(), [])
        self.assertIn(key, self.host.running)

    def test_an_issue_the_copy_does_not_hold_keeps_its_session(self):
        key = self.open_task()
        self.started(key)
        self.copy.open_issue = None
        self.assertEqual(self.flights.close_finished(), [])
        self.assertIn(key, self.host.running)

    def test_a_failed_task_session_stays_listed_as_failed(self):
        self.host.start_fails = True
        key = self.open_task()
        self.copy.open_issue = False
        self.time[0] += 13 * 3600
        self.assertEqual(self.flights.close_finished(), [])
        found = self.flights.list()["open"][0]
        self.assertEqual((found["key"], found["status"]), (key, "failed"))

    def test_a_session_opened_under_two_minutes_ago_is_not_closed(self):
        key = self.open_task()
        self.host.screen = SESSION_LINK
        self.flights.list()
        self.copy.open_issue = False
        self.time[0] += flights.WAIT_SECONDS - 5
        self.assertEqual(self.flights.close_finished(), [])
        self.assertIn(key, self.host.running)

    def test_a_copy_that_fails_leaves_the_idle_rule_to_decide(self):
        def broken(repo, number):
            raise KeyError(repo)
        self.copy.copied_issue_open = broken
        key = self.open_task()
        session = self.started(key)
        self.transcript(session, {"output_tokens": 3})
        self.idle(session, 0)
        self.time[0] += 12 * 3600 - 60
        self.assertEqual(self.flights.close_finished(), [])
        self.time[0] += 120
        self.assertEqual(self.flights.close_finished(), [key])

    def test_didban_and_zanbourdar_are_never_closed_by_themselves(self):
        with mock.patch("kandu.didban.check_brain", return_value=("ok", "config ok")), \
             mock.patch("kandu.didban.waiting", return_value={}):
            self.flights.open_didban("en")
        self.flights.open_zanbourdar("en")
        self.copy.open_issue = False
        self.time[0] += 3 * 24 * 3600
        self.assertEqual(self.flights.close_finished(), [])
        self.assertEqual(len(self.host.running), 2)

    def test_the_sync_closes_finished_sessions_after_it_sends_and_survives_an_error(self):
        from kandu.sync import sync
        order = []
        mirror = SimpleNamespace(refresh=lambda config, max_age=0: [],
                                 send=lambda: order.append("send") or (0, 0, 0))
        done = SimpleNamespace(close_finished=lambda: order.append("close") or ["t-1"])
        self.assertIn("closed 1 session(s)", sync(self.config, mirror, flights=done))
        self.assertEqual(order, ["send", "close"])

        def broken():
            raise OSError("tmux hung")
        line = sync(self.config, mirror, flights=SimpleNamespace(close_finished=broken))
        self.assertIn("could not close sessions: OSError", line)

    # The first message and the state.

    def assert_not_in_trouble(self):
        message = self.first_message()
        self.assertIsNotNone(message)
        self.assertNotIn("in trouble", message)

    def test_a_task_with_a_pr_opens_by_summing_up_its_issue_and_pr(self):
        self.record("task_picked")
        self.review()
        self.copy.pr = open_pr()
        self.open_task()
        message = self.first_message()
        self.assertNotIn("in trouble", message)
        self.assertIn(READ_ISSUE, message)
        self.assertIn(f"PR #12 with its reviews with `{READ_PR}`", message)
        self.assertIn("in a few lines, in Persian", message)
        self.assertIn("as data", message)
        self.assertIn("Change nothing until the user asks", message)
        self.assertNotIn("gh ", message)
        for text in ("ISSUE-TITLE-TEXT", "PR-BODY-TEXT", "COMMENT-TEXT"):
            self.assertNotIn(text, message)

    def test_a_task_without_a_pr_opens_by_summing_up_its_issue_alone(self):
        self.record("task_picked")
        self.copy.column = "Ready"
        self.open_task("en")
        message = self.first_message()
        self.assertIn(READ_ISSUE, message)
        self.assertNotIn(READ_PR, message)
        self.assertIn("It has no open PR", message)
        self.assertIn("in a few lines, in English", message)

    def test_a_task_whose_pr_cannot_be_read_is_not_said_to_have_none(self):
        def broken(*args):
            raise flights.GitHubError("GitHub could not be read")
        self.copy.find_pr = broken
        self.record("task_picked")
        self.open_task("en")
        message = self.first_message()
        self.assertNotIn("It has no open PR", message)
        self.assertIn("could not read whether it has an open PR", message)
        self.assertIn(READ_PR, message)

    def test_a_task_in_trouble_opens_with_the_trouble_then_the_summary(self):
        self.review(blockers=2, criteria_met=False)
        self.copy.pr = open_pr()
        self.open_task()
        message = self.first_message()
        self.assertTrue(message.startswith("Kandu wrote this message, not the user. This task is in trouble"))
        self.assertLess(message.index(flights.CHANGES_ASKED), message.index(READ_ISSUE))
        self.assertIn("Change nothing until the user says yes", message)
        for text in ("ISSUE-TITLE-TEXT", "PR-BODY-TEXT", "COMMENT-TEXT"):
            self.assertNotIn(text, message)
        self.assertNotIn("TEXT", self.prompt(task_key("acme-org/website#6")))

    def test_a_healthy_task_still_knows_its_state(self):
        self.record("task_picked")
        self.review()
        self.copy.pr = open_pr()
        prompt = self.prompt(self.open_task())
        self.assertIn("## Its state, by Kandu's records", prompt)
        self.assertIn("Column: In review", prompt)
        self.assertIn("round 1: passed", prompt)
        self.assertIn("a two-way door", prompt)
        self.assertIn("PR #12: checks ok", prompt)
        self.assertIn("Nothing is wrong with it", prompt)

    def test_a_one_way_door_that_passed_review_waits_for_the_user_until_a_yes(self):
        self.review(door="one-way")
        self.copy.pr = open_pr()
        self.open_task()
        self.assertIn("one-way door, so it merges only on the user's yes", self.first_message())
        self.flights.close(task_key("acme-org/website#6"))
        self.record("user_approved", head_sha="abc")
        key = self.open_task()
        self.assertIn("a yes is given for its head", self.prompt(key))
        self.assertNotIn("in trouble", self.first_message_of_last_start())

    def first_message_of_last_start(self):
        return first_said([c for c in self.host.commands if c[0] == "systemd-run"][-1][-1])

    def test_each_kind_of_trouble_is_named(self):
        cases = [
            (lambda: self.record("task_finished", outcome="failed"), flights.FAILED_RUN),
            (lambda: setattr(self.copy, "pr", open_pr(statusCheckRollup=[
                {"name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"}])), "failing: lint"),
            (lambda: setattr(self.copy, "pr", open_pr(mergeable="CONFLICTING")), flights.CONFLICT),
            (lambda: setattr(self.copy, "column", "Needs info"), "a worker asked for information"),
            (lambda: self.review(wait_for_user=["QUESTION-TEXT?"]), "1 review question(s)"),
            (lambda: self.record("loop_stopped", action="retry", reason="went round"), "stopped it for going round"),
        ]
        for setup, named in cases:
            with self.subTest(named=named):
                self.setUp()
                setup()
                self.open_task()
                message = self.first_message()
                self.assertIn("in trouble", message)
                self.assertIn(named, message)
                self.assertNotIn("QUESTION-TEXT", message)

    def test_a_merged_task_and_one_running_again_are_not_in_trouble(self):
        for events in ((("review_finished", {}), ("task_finished", {"outcome": "merged"})),
                       (("task_finished", {"outcome": "failed"}), ("task_picked", {}))):
            with self.subTest(events=events):
                self.setUp()
                for kind, fields in events:
                    if kind == "review_finished":
                        self.review(blockers=1, criteria_met=False)
                    else:
                        self.record(kind, **fields)
                self.open_task()
                self.assert_not_in_trouble()

    def test_didban_and_zanbourdar_never_start_on_their_own(self):
        self.record("task_finished", outcome="failed")
        with mock.patch("kandu.didban.check_brain", return_value=("ok", "config ok")), \
             mock.patch("kandu.didban.waiting", return_value={}):
            self.flights.open_didban("en")
        self.assertIsNone(self.first_message())
        self.flights.open_zanbourdar("en")
        self.assertIsNone(self.first_message_of_last_start_or_none())

    def first_message_of_last_start_or_none(self):
        return first_said([c for c in self.host.commands if c[0] == "systemd-run"][-1][-1])

    def test_a_flight_that_started_on_its_own_keeps_its_tokens(self):
        self.record("task_finished", outcome="failed")
        key = self.open_task()
        self.transcript(self.store.flight(key)["session"], {"output_tokens": 40})
        self.host.running.discard(key)
        flown = self.flights.list()["flown"]
        self.assertEqual((flown[0]["task"], flown[0]["tokens"]["output"]), ("acme-org/website#6", 40))
