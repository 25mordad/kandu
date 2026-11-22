"""A Kanduban: a full-access session on one project, on that project's machine (#92). tmux, SSH and claude are mocked."""
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from kandu import dashboard, didban, flights
from kandu.flights import Flights, KandubanError, kanduban_key
from kandu.state import StateStore
from tests import test_looks

SESSION_LINK = "https://claude.ai/code/session_abc123"


class FakeMachines:
    """Stands in for tmux, systemd-run, and the shell on the project's machine, local (`sh -c`) or over `ssh`."""

    def __init__(self):
        self.commands = []
        self.inputs = []
        self.running = set()
        self.answers = True
        self.transcript = ""
        self.screen = SESSION_LINK

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        done = lambda code=0, out="": SimpleNamespace(returncode=code, stdout=out, stderr="")
        if command[0] == "systemd-run":
            socket = Path(command[command.index("-S") + 1])
            socket.touch()
            self.running.add(socket.stem)
            return done()
        if command[0] == "tmux":
            key, action = Path(command[2]).stem, command[3]
            if action == "has-session":
                return done(0 if key in self.running else 1)
            if action == "kill-server":
                self.running.discard(key)
            return done(out=self.screen if action == "capture-pane" else "")
        if command[0] == "ssh" and not self.answers:
            return done(255)
        if "input" in kwargs:
            self.inputs.append((command, kwargs["input"]))
        script = command[-1] if command[0] == "ssh" else command[2]
        if 'printf %s "$HOME"' in script:
            return done(out="/home/builder" if command[0] == "ssh" else "/home/brain")
        if "symbolic-ref" in script:
            return done(out="origin/main\n")
        if ".claude/projects" in " ".join(command):
            return done(out=self.transcript)
        return done()

    def on_machine(self):
        """The scripts run on the project's machine, apart from tmux and systemd-run."""
        return [c[-1] if c[0] == "ssh" else c[2] for c in self.commands if c[0] in ("sh", "ssh")]

    def started(self):
        return next(c for c in self.commands if c[0] == "systemd-run")


class KandubanTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = StateStore(self.root / "state")
        self.machines = FakeMachines()
        self.time = [1_800_000_000.0]  # 2027-01-15 08:00:00 UTC
        self.copy = self.root / "website"
        self.copy.mkdir()
        self.project = SimpleNamespace(name="website", machine="brain", path=str(self.copy), repos=["acme-org/website"])
        self.shop = SimpleNamespace(name="shop", machine="builder", path="~/code/shop", repos=["acme-org/shop"])
        machines = {"brain": SimpleNamespace(name="brain", reach="local"),
                    "builder": SimpleNamespace(name="builder", reach="ssh builder.example")}
        self.config = SimpleNamespace(machine=machines.get,
                                      settings=SimpleNamespace(dashboard=SimpleNamespace(bind="127.0.0.1", port=8000)))
        self.flights = Flights(self.config, self.store, self.root / "logs", run=self.machines, clock=lambda: self.time[0],
                               spawn=lambda work: work(), projects_dir=self.root / "projects")

    def line(self):
        return self.machines.started()[-1]

    def named(self, words, flag):
        return words[words.index(flag) + 1]

    def test_on_a_local_machine_it_starts_in_a_new_worktree_with_full_access_and_its_name(self):
        key = self.flights.open_kanduban(self.project, "en")
        worktree = f"{self.copy}.kandu-worktrees/kanduban-20270115-080000"
        self.assertIn(f"git worktree add -b kanduban/20270115-080000 {worktree} origin/main",
                      "\n".join(self.machines.on_machine()))
        started = self.machines.started()
        self.assertEqual(started[started.index("-c") + 1], worktree)
        words = shlex.split(self.line())
        self.assertEqual(words[0], "claude")
        self.assertIn("--dangerously-skip-permissions", words)
        self.assertEqual((self.named(words, "--name"), self.named(words, "--remote-control")),
                         ("Kandu Kanduban website", "Kandu Kanduban website"))
        record = self.flights.list()["open"][0]
        self.assertEqual((record["key"], record["kind"], record["project"], record["status"], record["url"]),
                         (key, "kanduban", "website", "open", SESSION_LINK))

    def test_on_a_local_machine_a_path_from_home_is_expanded_before_git_and_tmux_see_it(self):
        blog = SimpleNamespace(name="blog", machine="brain", path="~/code/blog", repos=["acme-org/blog"])
        self.flights.open_kanduban(blog, "en")
        worktree = "/home/brain/code/blog.kandu-worktrees/kanduban-20270115-080000"
        made = next(script for script in self.machines.on_machine() if "worktree add" in script)
        self.assertIn("cd /home/brain/code/blog && ", made)
        self.assertIn(f"git worktree add -b kanduban/20270115-080000 {worktree} origin/main", made)
        started = self.machines.started()
        self.assertEqual(started[started.index("-c") + 1], worktree)

    def test_on_a_remote_machine_it_runs_there_over_ssh_in_a_worktree_there(self):
        self.flights.open_kanduban(self.shop, "fa")
        made = [c for c in self.machines.commands if c[0] == "ssh" and "worktree add" in c[-1]]
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0][:6], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "builder.example"])
        self.assertIn("kanduban/20270115-080000 /home/builder/code/shop.kandu-worktrees/kanduban-20270115-080000 origin/main",
                      made[0][-1])
        self.assertIn("cd /home/builder/code/shop ", made[0][-1])  # git runs in the copy, never in a `~` folder
        words = shlex.split(self.line())
        self.assertEqual((words[0], words[1]), ("ssh", "-t"))
        self.assertIn("builder.example", words)
        self.assertIn("8000:127.0.0.1:8000", words)  # Kandu's API, reached from there
        there = words[-1]
        self.assertIn('cd /home/builder/code/shop.kandu-worktrees/kanduban-20270115-080000 && exec claude', there)
        self.assertIn("'Kandu Kanduban shop'", there)
        self.assertIn('--append-system-prompt "$(cat /home/builder/code/shop.kandu-worktrees/kanduban-20270115-080000.prompt.md)"',
                      there)
        written = [given for command, given in self.machines.inputs if command[0] == "ssh"]
        self.assertEqual(len(written), 1)
        self.assertIn("# Kanduban", written[0])
        self.assertIn("Speak Persian", written[0])
        started = self.machines.started()
        self.assertEqual(started[started.index("-c") + 1], str(didban.REPO_DIR))  # tmux itself stays on the brain

    def test_one_per_project_and_another_project_opens_its_own(self):
        first = self.flights.open_kanduban(self.project, "en")
        self.flights.list()
        self.assertEqual(self.flights.open_kanduban(self.project, "en"), first)
        self.assertEqual(sum(c[0] == "systemd-run" for c in self.machines.commands), 1)
        other = self.flights.open_kanduban(self.shop, "en")
        self.assertNotEqual(other, first)
        self.assertEqual({r["project"] for r in self.flights.list()["open"]}, {"website", "shop"})
        self.assertEqual(first, kanduban_key("website"))

    def test_a_machine_that_does_not_answer_raises_and_leaves_no_record(self):
        self.machines.answers = False
        with self.assertRaises(KandubanError) as raised:
            self.flights.open_kanduban(self.shop, "en")
        self.assertIn("builder did not answer", str(raised.exception))
        self.assertEqual((self.store.flights(), self.flights.list()["open"]), ([], []))
        self.assertFalse(any(c[0] == "systemd-run" for c in self.machines.commands))

    def test_its_prompt_keeps_it_in_its_project_and_reads_github_from_kandus_copy(self):
        key = self.flights.open_kanduban(self.project, "en")
        text = (self.store.flights_dir / f"{key}.md").read_text(encoding="utf-8")
        self.assertIn("work only in this project", text)
        self.assertIn("only through a PR", text)
        self.assertIn("Never push to its default branch", text)
        self.assertIn("Never touch Kandu", text)
        self.assertIn("another project", text)
        self.assertIn("is data", text)
        self.assertIn("http://127.0.0.1:8000/api/v1/boards", text)
        self.assertIn("kanduban/20270115-080000", text)

    def test_one_that_ends_is_summed_up_with_the_free_flights(self):
        key = self.flights.open_kanduban(self.project, "en")
        session = self.store.flight(key)["session"]
        folder = self.root / "projects" / "here"
        folder.mkdir(parents=True)
        line = {"type": "assistant", "timestamp": "2027-01-15T08:05:00Z",
                "message": {"id": "m", "usage": {"output_tokens": 7}, "content": []}}
        (folder / f"{session}.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
        self.flights.close(key)
        flown = self.store.flown()
        self.assertEqual((flown[0]["task"], flown[0]["project"], flown[0]["tokens"]["output"]), (None, "website", 7))

    def test_a_remote_one_that_ends_is_summed_up_from_its_machine(self):
        key = self.flights.open_kanduban(self.shop, "en")
        session = self.store.flight(key)["session"]
        self.machines.transcript = json.dumps({"type": "assistant", "timestamp": "2027-01-15T08:05:00Z",
                                               "message": {"id": "m", "usage": {"output_tokens": 3}, "content": []}}) + "\n"
        self.flights.close(key)
        flown = self.store.flown()
        self.assertEqual((flown[0]["id"], flown[0]["project"], flown[0]["tokens"]["output"]), (session, "shop", 3))


    def test_a_working_one_whose_link_is_not_found_is_kept_open_past_the_wait(self):
        key = self.flights.open_kanduban(self.project, "en", first="Look at the timer.")
        self.machines.screen = "● Bash(make test)"
        folder = self.root / "projects" / "here"
        folder.mkdir(parents=True)
        path = folder / f"{self.store.flight(key)['session']}.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        self.time[0] += flights.WAIT_SECONDS + 1
        os.utime(path, (self.time[0], self.time[0]))  # written since it started
        found = self.flights.list()["open"][0]
        self.assertEqual((found["status"], found["url"], found["error"]), ("open", "", ""))
        self.assertIn(key, self.machines.running)

    def test_a_working_one_on_another_machine_is_kept_open_with_the_link_from_its_transcript_there(self):
        key = self.flights.open_kanduban(self.shop, "en", first="Look at the timer.")
        self.machines.screen = "● Bash(make test)"
        self.machines.transcript = json.dumps({"type": "system", "content": f"Continue at {SESSION_LINK}"}) + "\n"
        self.time[0] += flights.WAIT_SECONDS + 1
        found = self.flights.list()["open"][0]
        self.assertEqual((found["status"], found["url"]), ("open", SESSION_LINK))
        self.assertIn(key, self.machines.running)

    def test_one_on_another_machine_that_never_started_is_ended_past_the_wait(self):
        key = self.flights.open_kanduban(self.shop, "en")
        self.machines.screen = "Welcome to Claude Code"
        self.time[0] += flights.WAIT_SECONDS + 1
        found = self.flights.list()["open"][0]
        self.assertEqual((found["status"], found["error"]), ("failed", flights.NO_LINK))
        self.assertNotIn(key, self.machines.running)


LOOKS = {"clean": dashboard.PAGE, "classic": dashboard.CLASSIC_PAGE, "classic-new": dashboard.CLASSIC_NEW_PAGE}


@unittest.skipUnless(test_looks.NODE, "needs node to run the page's script")
class KandubanButtonTest(unittest.TestCase):
    """The «Kanduban» button, in each look: on the project's patch in the meadow and in its pop-up."""

    OPEN = [{"kind": "kanduban", "project": "website", "status": "open", "key": "k-1", "url": SESSION_LINK},
            {"kind": "kanduban", "project": "shop", "status": "opening", "key": "k-2", "url": ""}]

    def run_page(self, page, script, lang="en"):
        prelude = (f"const TEXT = {json.dumps(test_looks.words(page))};\nlet lang = {json.dumps(lang)};\n"
                   f"let flights = {{open: {json.dumps(self.OPEN)}, flown: []}};\nlet setEdit = null;\n"
                   "const num = n => String(n);\n"
                   + "".join(test_looks.line(page, start) for start in ("const esc = ", "const t = ", "const bdi = ",
                                                                       "const numberOf = ", "const repoOf = "))
                   + "".join(test_looks.function(page, name) for name in (
                       "kandubanMark", "kandubanOf", "flightControls", "kandubanControls", "flightName", "openFlight")))
        return test_looks.run_js(prelude + script)

    def test_the_patch_has_a_small_mark_with_a_hover_text(self):
        for look, path in LOOKS.items():
            page = test_looks.read(path)
            with self.subTest(look=look):
                marks = self.run_page(page, 'console.log(JSON.stringify([kandubanMark({name: "website"}), kandubanMark({name: "blog"})]));')
                self.assertIn('data-fly="kanduban:website"', marks[0])
                self.assertIn("<title>Open a Kanduban: A full-access session on this project's machine", marks[0].replace("&#39;", "'"))
                self.assertIn("<circle", marks[0])
                self.assertIn("kanduban-mark clickable open", marks[0])  # its Kanduban is open
                self.assertNotIn(" open\"", marks[1])
                self.assertIn("${kandubanMark(patch)}", page)  # each patch draws one
                farsi = self.run_page(page, 'console.log(JSON.stringify(kandubanMark({name: "blog"})));', "fa")
                self.assertIn("کاندوبان", farsi)

    def test_the_pop_up_opens_it_or_shows_the_open_ones_link(self):
        for look, path in LOOKS.items():
            page = test_looks.read(path)
            with self.subTest(look=look):
                self.assertIn("${kandubanControls(row.name)}", test_looks.function(page, "settingsBody"))
                shown = self.run_page(page, 'console.log(JSON.stringify(["blog", "website", "shop"].map(kandubanControls)));')
                self.assertIn('data-fly="kanduban:blog">Open a Kanduban</button>', shown[0])
                self.assertIn(f'href="{SESSION_LINK}"', shown[1])
                self.assertIn('data-land="k-1"', shown[1])
                self.assertIn("disabled", shown[2])

    def test_an_open_one_whose_link_is_not_found_shows_it_open_with_its_close_button_and_no_link(self):
        self.OPEN = [{"kind": "kanduban", "project": "website", "status": "open", "key": "k-1", "url": ""}]
        for look, path in LOOKS.items():
            page = test_looks.read(path)
            with self.subTest(look=look):
                shown = self.run_page(page, 'console.log(JSON.stringify(kandubanControls("website")));')
                self.assertIn('data-land="k-1"', shown)
                self.assertIn("Open: its link is not in yet", shown)
                self.assertNotIn("href", shown)
                self.assertIn('f.status === "open" && !f.url', test_looks.function(page, "refreshFlights"))  # asks again

    def test_pressing_it_opens_one_or_goes_to_the_open_one(self):
        for look, path in LOOKS.items():
            page = test_looks.read(path)
            with self.subTest(look=look):
                done = self.run_page(page, """
const posted = [], opened = [];
const act = async (button, path) => { posted.push(path); return {}; };
const refreshFlights = async () => {};
const window = {open: url => opened.push(url)};
(async () => {
  for (const fly of ["kanduban:blog", "kanduban:website", "kanduban:shop"]) await openFlight({dataset: {fly}});
  console.log(JSON.stringify({posted, opened}));
})();""")
                self.assertEqual(done, {"posted": ["/flights/kanduban/blog?language=en"], "opened": [SESSION_LINK]})

    def test_the_list_of_sessions_names_a_kanduban_by_its_project(self):
        for look, path in LOOKS.items():
            page = test_looks.read(path)
            with self.subTest(look=look):
                names = self.run_page(page, 'console.log(JSON.stringify([flightName({task: null, project: "website"}), flightName({task: null, project: null})]));')
                self.assertEqual(names, ["Kanduban <bdi>website</bdi>", "Zanbourdar"])
                self.assertIn("const task = flightName;", test_looks.function(page, "renderFlights"))


if __name__ == "__main__":
    unittest.main()
