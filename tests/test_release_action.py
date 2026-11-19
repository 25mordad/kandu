"""«Cut a release» on the dashboard, in every look, and the release flow in Didban's prompt (issue #106).

The release command itself, `python -m kandu.release`, is stood in for by a fake that prints what it prints; the
dashboard only ever runs it, so the fake is the one thing outside the dashboard.
"""
import contextlib
import io
import json
import re
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from kandu import dashboard, release
from kandu.dashboard import Api, dispatch, make_handler
from kandu.github import GitHub
from tests.test_dashboard import Base, TOKEN
from tests.test_release import MIX, MIX_PRS, FakeGh, remotes
from tests.test_looks import NODE, browser

ROOT = Path(__file__).resolve().parent.parent
TEXT = re.compile(r'<script type="application/json" id="text">(.*?)</script>', re.DOTALL)
LOOKS = {"clean": dashboard.PAGE, "classic": dashboard.CLASSIC_PAGE, "classic-new": dashboard.LOOKS["classic-new"]}

ENTRY = "## 1.1.0 - 2026-11-19\n\n### Features\n- Show the queen's brief (#12)\n\n### Fixes\n- Keep the meadow still (#13)\n"
PROPOSAL = ("Changes since 1.0.0:\n  #12 Show the queen's brief (Feature)\n  #13 Keep the meadow still (Bug)\n\n"
            f"Proposed version: 1.1.0\n\n{ENTRY}\n")
PR_URL = "https://github.com/acme-org/kandu/pull/77"


class FakeRelease:
    """Prints as `python -m kandu.release` does: with no terminal it proposes, says «Nothing changed.» and stops;
    with `--yes` it proposes and opens the PR."""

    def __init__(self, proposal=PROPOSAL):
        self.proposal = proposal
        self.calls = []
        self.fails = None

    def __call__(self, args):
        self.calls.append(list(args))
        if self.fails:
            return 1, "", f"release: {self.fails}\n"
        if self.proposal is None:
            return 0, "No change since the last release (1.0.0): nothing to propose.\n", ""
        if "--yes" in args:
            return 0, f"{self.proposal}Release PR: {PR_URL}\n", ""
        return 0, f"{self.proposal}Nothing changed.\n", ""

    def opened(self):
        return [call for call in self.calls if "--yes" in call]


class ReleaseRouteTest(Base):
    def setUp(self):
        super().setUp()
        self.fake = FakeRelease()
        self.api.release_command = self.fake

    def post(self, target, token=TOKEN):
        return dispatch(self.api, "POST", target, token)

    def test_the_proposal_shows_the_next_version_and_the_changelog_entry_and_changes_nothing(self):
        status, body = self.get("/api/v1/release")
        self.assertEqual(status, 200)
        self.assertEqual((body["version"], body["next"]), ("1.0.0", "1.1.0"))
        self.assertIn(ENTRY.strip(), body["text"])
        self.assertIn("#12 Show the queen's brief (Feature)", body["text"])
        self.assertNotIn("Nothing changed", body["text"])
        self.assertTrue(body["proposal"])
        self.assertEqual(self.fake.calls, [[]])  # never with --yes

    def test_with_no_change_it_says_so_and_offers_nothing_to_confirm(self):
        self.fake.proposal = None
        status, body = self.get("/api/v1/release")
        self.assertEqual(status, 200)
        self.assertIsNone(body["next"])
        self.assertEqual(body["version"], "1.0.0")
        self.assertIn("No change since the last release (1.0.0)", body["text"])
        status, body = self.post(f"/api/v1/release?proposal={body['proposal']}")
        self.assertEqual(status, 409)
        self.assertEqual(self.fake.opened(), [])

    def test_the_confirm_opens_the_release_pr_for_the_proposal_the_user_saw(self):
        shown = self.get("/api/v1/release")[1]
        status, body = self.post(f"/api/v1/release?proposal={shown['proposal']}")
        self.assertEqual((status, body), (200, {"version": "1.1.0", "url": PR_URL}))
        self.assertEqual(self.fake.opened(), [["--yes"]])

    def test_the_confirm_needs_the_token(self):
        shown = self.get("/api/v1/release")[1]
        self.assertEqual(self.post(f"/api/v1/release?proposal={shown['proposal']}", token=None)[0], 403)
        self.assertEqual(self.post(f"/api/v1/release?proposal={shown['proposal']}", token="wrong")[0], 403)
        self.assertEqual(self.fake.opened(), [])

    def test_a_confirm_without_the_proposal_it_answers_opens_nothing(self):
        self.assertEqual(self.post("/api/v1/release")[0], 409)
        self.assertEqual(self.fake.opened(), [])

    def test_when_main_moved_since_the_proposal_nothing_opens_and_the_new_one_comes_back(self):
        shown = self.get("/api/v1/release")[1]
        self.fake.proposal = PROPOSAL.replace("1.1.0", "2.0.0")
        status, body = self.post(f"/api/v1/release?proposal={shown['proposal']}")
        self.assertEqual(status, 409)
        self.assertEqual(body["next"], "2.0.0")
        self.assertEqual(self.fake.opened(), [])

    def test_a_failed_command_says_why(self):
        self.fake.fails = "acme-org/kandu is the public upstream; a release PR goes only to the private copy"
        status, body = self.get("/api/v1/release")
        self.assertEqual(status, 502)
        self.assertEqual(body["error"], self.fake.fails)

    def test_the_dashboard_runs_the_installs_own_command_with_no_terminal(self):
        with tempfile.TemporaryDirectory() as install:
            with unittest.mock.patch("kandu.dashboard.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, "No change since the last release (1.0.0).\n", "")
                api = Api(self.config, self.store, self.github, self.root / "logs", TOKEN, timer_on=lambda: True,
                          local_dir=self.root / "local", install_dir=Path(install))
                self.assertIsNone(dispatch(api, "GET", "/api/v1/release")[1]["next"])
        command, kwargs = run.call_args.args[0], run.call_args.kwargs
        self.assertEqual(command[1:], ["-m", "kandu.release"])
        self.assertEqual(kwargs["cwd"], Path(install))
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)


    def test_a_release_command_that_takes_too_long_is_a_502_and_opens_nothing(self):
        with tempfile.TemporaryDirectory() as install:
            with unittest.mock.patch("kandu.dashboard.subprocess.run") as run:
                run.side_effect = subprocess.TimeoutExpired(["python", "-m", "kandu.release"], dashboard.RELEASE_SECONDS)
                api = Api(self.config, self.store, self.github, self.root / "logs", TOKEN, timer_on=lambda: True,
                          local_dir=self.root / "local", install_dir=Path(install))
                status, body = dispatch(api, "GET", "/api/v1/release")
                self.assertEqual((status, body["error"]), (502, "the release command took too long"))
                status, body = dispatch(api, "POST", "/api/v1/release?proposal=0123456789abcdef", TOKEN)
                self.assertEqual((status, body["error"]), (502, "the release command took too long"))
        self.assertEqual([c for c in run.call_args_list if "--yes" in c.args[0]], [])


class RealRelease:
    """The real `python -m kandu.release`, run in-process against a stub GitHub, with no terminal as the dashboard
    runs it: so a change to what the release command prints breaks these tests, not the dashboard in silence."""

    def __init__(self, gh):
        self.gh = gh
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch("sys.stdin", io.StringIO("")), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = release.main(list(args), github=GitHub(run=self.gh), remotes=remotes)
        return code, out.getvalue(), err.getvalue()


class RealReleaseOutputTest(Base):
    """The dashboard's parser fed what `kandu.release` itself prints."""

    def use(self, gh):
        self.api.release_command = RealRelease(gh)
        return gh

    def test_a_proposal_reads_as_the_next_version_with_its_changes_and_entry(self):
        gh = self.use(FakeGh(MIX, MIX_PRS))
        status, body = self.get("/api/v1/release")
        self.assertEqual(status, 200)
        self.assertEqual((body["version"], body["next"]), ("1.0.0", "1.1.0"))
        self.assertIn("#12 Add a calm switch (Feature)", body["text"])
        self.assertIn("### Features\n- Add a calm switch (#12)", body["text"])
        self.assertNotIn("Nothing changed", body["text"])
        self.assertEqual(gh.writes, [])

    def test_no_change_reads_as_nothing_to_release(self):
        gh = self.use(FakeGh([], {}))
        status, body = self.get("/api/v1/release")
        self.assertEqual(status, 200)
        self.assertEqual((body["version"], body["next"]), ("1.0.0", None))
        self.assertIn("No change since the last release (1.0.0)", body["text"])
        self.assertEqual(gh.writes, [])

    def test_the_yes_reads_the_release_pr_link(self):
        gh = self.use(FakeGh(MIX, MIX_PRS))
        shown = self.get("/api/v1/release")[1]
        status, body = dispatch(self.api, "POST", f"/api/v1/release?proposal={shown['proposal']}", TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"version": "1.1.0", "url": "https://github.com/acme-org/kandu-copy/pull/200"})
        self.assertEqual(self.api.release_command.calls, [[], [], ["--yes"]])
        self.assertTrue(gh.writes)


class PagesTest(unittest.TestCase):
    def test_every_look_has_the_action_its_dialog_and_its_words_in_both_languages(self):
        for look, path in LOOKS.items():
            page = path.read_text(encoding="utf-8")
            words = json.loads(TEXT.search(page).group(1))
            for key in ("release_cut", "release_ask", "release_none", "release_open", "release_opened"):
                self.assertTrue(words["en"].get(key), f"{look}: {key}")
                self.assertTrue(words["fa"].get(key), f"{look}: fa {key}")
            self.assertEqual(words["en"]["release_cut"], "Cut a release", look)
            self.assertIn("data-release", page, look)
            self.assertIn('id="releasebox"', page, look)
            self.assertIn('id="releasenow"', page, look)


class DidbanPromptTest(unittest.TestCase):
    def test_didban_shows_the_proposal_and_runs_the_release_only_on_the_users_yes(self):
        text = (ROOT / "prompts" / "didban.md").read_text(encoding="utf-8")
        section = text.split("## Cutting a release")[1].split("\n## ")[0]
        steps = section.split("\n1. ")[1]
        self.assertIn("`python -m kandu.release`", steps.split("\n2. ")[0])
        self.assertIn("Show the user", steps.split("\n2. ")[0])
        self.assertIn("Only on that yes", steps.split("\n2. ")[1])
        self.assertIn("`python -m kandu.release --yes`", steps.split("\n2. ")[1])
        self.assertIn("No change since the last release", section)

    def test_the_design_and_the_glossary_describe_the_release_flow(self):
        design = (ROOT / "DESIGN.md").read_text(encoding="utf-8")
        self.assertIn("«Cut a release»", design)
        glossary = (ROOT / "GLOSSARY.md").read_text(encoding="utf-8")
        self.assertIn("**Cut a release**", glossary)


class BrowserTest(Base):
    """Each look in a real headless browser: the action shows the proposal, and only the confirm opens the PR."""

    def setUp(self):
        super().setUp()
        self.fake = FakeRelease()
        self.api.release_command = self.fake
        self.store.write_current({"supervisor": "awake", "usage": {"session": 30, "week": 65}, "next_wake": None, "workers": []})
        self.github.board_items.return_value = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.api))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"

    def drive(self, plan):
        with tempfile.TemporaryDirectory() as profile:
            done = subprocess.run([NODE, str(ROOT / "tests" / "drive_release.js"), browser(), self.base, profile, TOKEN,
                                   json.dumps(plan)], capture_output=True, text=True, timeout=170)
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        report = json.loads(done.stdout)
        self.assertEqual(report["errors"], [])
        return report["results"]

    @unittest.skipUnless(NODE and browser(), "needs node and a Chromium")
    def test_in_every_look_the_action_shows_the_proposal_asks_and_opens_only_on_the_yes(self):
        for case in self.drive({"looks": list(LOOKS), "confirm": True}):
            look, shown = case["look"], case["shown"]
            self.assertEqual(case["page"], look)
            self.assertTrue(case["action"], look)
            self.assertTrue(shown["open"], look)
            self.assertIn("1.1.0", shown["head"], look)
            self.assertIn("### Features", shown["text"], look)
            self.assertFalse(shown["confirm_hidden"], look)
            self.assertTrue(case["closed"], look)
            self.assertIn(PR_URL, case["after"]["text"], look)
            self.assertTrue(case["after"]["confirm_hidden"], look)
        # Closing the dialog without a yes opened nothing: one PR a look, from its confirm.
        self.assertEqual(len(self.fake.opened()), len(LOOKS))

    @unittest.skipUnless(NODE and browser(), "needs node and a Chromium")
    def test_in_every_look_no_change_is_said_and_no_confirm_is_offered(self):
        self.fake.proposal = None
        for case in self.drive({"looks": list(LOOKS), "confirm": False}):
            look, shown = case["look"], case["shown"]
            self.assertTrue(shown["open"], look)
            self.assertIn("No change since the last release", shown["text"], look)
            self.assertTrue(shown["confirm_hidden"], look)
        self.assertEqual(self.fake.opened(), [])


if __name__ == "__main__":
    unittest.main()
