import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kandu import didban, flights
from kandu.config import DidbanBrain, DidbanConfig
from kandu.flights import DIDBAN, Flights, task_key
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

    def test_a_task_flight_prompt_holds_no_issue_text(self):
        key = self.open_task()
        self.assertNotIn("Title", self.prompt(key))

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
