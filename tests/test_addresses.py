"""Every view of the dashboard has its own address, in every look (#94)."""
import json
import re
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request

from kandu import dashboard
from kandu.github import GitHubError
from tests.test_dashboard import REPO
from tests.test_looks import NODE, ROOT, Served, browser, function, line, read, run_js, words

PAGES = {"classic": dashboard.CLASSIC_PAGE, "classic-new": dashboard.CLASSIC_NEW_PAGE, "clean": dashboard.PAGE}
OWNER, NAME = REPO.split("/")
# A tab, and each thing that opens over a tab, as a user would copy them.
VIEWS = ["/today", "/tasks", "/bazras", "/settings", "/days", "/activity", "/meadow", "/work", "/today/",
         f"/tasks/task/{OWNER}/{NAME}/1", "/settings/project/website", "/activity/run/r-1",
         f"/today/task/{OWNER}/{NAME}/2/run/r-1", f"/tasks/task/{OWNER}/{NAME}/999", "/settings/project/gone"]


class ServedAddressTest(Served):
    def get(self, path, cookie=None):
        request = urllib.request.Request(self.base + path, headers={"Cookie": cookie} if cookie else {})
        with urllib.request.urlopen(request) as r:
            return r.status, r.headers["Content-Type"], r.read()

    def test_every_views_address_answers_with_the_page_of_the_browsers_look(self):
        for look, page in PAGES.items():
            for path in VIEWS:
                status, kind, body = self.get(path, f"kandu-look={look}")
                self.assertEqual((status, kind, body), (200, "text/html; charset=utf-8", page.read_bytes()), f"{look}: {path}")

    def test_a_view_opened_with_a_query_is_still_the_page(self):
        self.assertEqual(self.get("/tasks?from=chat")[2], dashboard.CLASSIC_PAGE.read_bytes())

    def test_the_api_and_an_unknown_address_answer_as_before(self):
        self.assertEqual(json.loads(self.get("/api/v1/days")[2]), {"days": []})
        for path in ("/nothing", "/todays", "/api/v1/today"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get(path)
            self.assertEqual(caught.exception.code, 404, path)
            caught.exception.close()


class AddressScriptTest(unittest.TestCase):
    """The page's own reading and writing of an address, the same in every look."""
    NAMES = ("viewAddress", "readAddress", "markView", "openAddress")

    def test_every_look_has_the_same_addresses(self):
        clean = read(dashboard.PAGE)
        for look, path in PAGES.items():
            page = read(path)
            for name in self.NAMES:
                self.assertEqual(function(page, name), function(clean, name), f"{look}: {name}")
            self.assertEqual(line(page, "const TAB_ADDRESS = "), line(clean, "const TAB_ADDRESS = "), look)
            self.assertIn('addEventListener("popstate", () => { openAddress(); });', page)
            self.assertIn("\nopenAddress();\n", page)
            for lang in ("en", "fa"):
                self.assertIn("view_not_found", words(page)[lang], f"{look}: {lang}")
            self.assertNotEqual(words(page)["fa"]["view_not_found"], words(page)["en"]["view_not_found"])

    @unittest.skipUnless(NODE, "needs node to run the page's script")
    def test_an_address_reads_back_as_the_view_it_was_written_from(self):
        page = read(dashboard.CLASSIC_PAGE)
        seen = run_js(line(page, "const TAB_ADDRESS = ") + function(page, "viewAddress") + function(page, "readAddress") + """
let tab, shownTask, setEdit, shownRun;
const views = [["meadow", null, null, null], ["work", "acme-org/website#12", null, null], ["settings", null, {name: "my site"}, null],
               ["work", "acme-org/website#12", null, "r-20261118-ab"], ["days", null, null, null]];
const written = views.map(([a, b, c, d]) => { tab = a; shownTask = b; setEdit = c; shownRun = d; return viewAddress(); });
const odd = ["/", "/tasks/task/acme-org/website", "/tasks/task/acme-org/website/x", "/tasks/flower/1", "/settings/project"];
console.log(JSON.stringify({written, read: written.map(readAddress), odd: odd.map(readAddress)}));""")
        self.assertEqual(seen["written"], ["/today", "/tasks/task/acme-org/website/12", "/settings/project/my%20site",
                                           "/tasks/task/acme-org/website/12/run/r-20261118-ab", "/days"])
        blank = {"task": None, "project": None, "run": None, "lost": False}
        self.assertEqual(seen["read"], [{**blank, "tab": "today"}, {**blank, "tab": "tasks", "task": "acme-org/website#12"},
                                        {**blank, "tab": "settings", "project": "my site"},
                                        {**blank, "tab": "tasks", "task": "acme-org/website#12", "run": "r-20261118-ab"},
                                        {**blank, "tab": "days"}])
        self.assertEqual(seen["odd"][0], {**blank, "tab": None})
        self.assertEqual([view["lost"] for view in seen["odd"][1:]], [True, True, True, True])


class DesignTest(unittest.TestCase):
    def test_design_says_every_view_has_its_own_address_in_every_look(self):
        section = read(ROOT / "DESIGN.md").split("## 7. Dashboard")[1].split("\n## 8.")[0]
        said = next(each for each in section.splitlines() if each.startswith("- **Addresses:**"))
        for part in ("Kandu Clean", "Classic", "Classic new", "reload", "Back and Forward", "copied", "task", "project", "run",
                     "not found", "token", "look and the language"):
            self.assertIn(part.lower(), said.lower(), part)


@unittest.skipUnless(NODE and browser(), "needs node and a Chromium to open the pages")
class BrowserAddressTest(Served):
    """In each look, in a real browser: open a task, its run, a project and Settings, reload, Back and Forward, and paste."""
    def setUp(self):
        super().setUp()
        known = {n for n in range(1, 7)}

        def issue_text(repo, number):
            if number not in known:
                raise GitHubError("no such issue")
            return "## Goal\nA goal.\n"
        self.github.issue_text.side_effect = issue_text
        self.github.find_pr.return_value = None  # no PR yet, so a task's sheet shows no review
        logs = self.root / "logs"
        logs.mkdir(exist_ok=True)
        (logs / "r-1.jsonl").write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "README.md"}}]}}) + "\n", encoding="utf-8")

    def test_each_view_keeps_its_address_through_reload_back_forward_and_a_copied_link_in_every_look(self):
        task = f"{REPO}#1"
        plan = {"looks": list(PAGES), "task": task, "project": "website", "run": "r-1",
                "lost": [f"/tasks/task/{REPO}/999", "/settings/project/gone", "/today/run/r-gone", "/today/flower/1"]}
        with tempfile.TemporaryDirectory() as profile:
            done = subprocess.run([NODE, str(ROOT / "tests" / "drive_addresses.js"), browser(), self.base, profile, json.dumps(plan)],
                                  capture_output=True, text=True, timeout=200)
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        report = json.loads(done.stdout)
        self.assertEqual(report["errors"], [])
        at_task = f"/today/task/{REPO}/1"
        view = lambda address, tab, task=None, project=None, run=None: {"address": address, "tab": tab, "task": task, "project": project, "run": run}
        expected = {"first": view("/today", "today"), "task": view(at_task, "today", task), "run": view(at_task + "/run/r-1", "today", task, run="r-1"),
                    "run_reloaded": view(at_task + "/run/r-1", "today", task, run="r-1"), "run_back": view(at_task, "today", task),
                    "task_back": view("/today", "today"), "task_forward": view(at_task, "today", task), "task_closed": view("/today", "today"),
                    "settings": view("/settings", "settings"), "project": view("/settings/project/website", "settings", project="website"),
                    "project_reloaded": view("/settings/project/website", "settings", project="website"),
                    "project_back": view("/settings", "settings"), "settings_back": view("/today", "today")}
        for look, steps in report["walks"].items():
            for name, seen in steps.items():
                self.assertEqual({k: seen[k] for k in ("address", "tab", "task", "project", "run")}, expected[name], f"{look}: {name}")
                self.assertEqual(seen["look"], look, f"{look}: {name}")
                self.assertIsNone(seen["note"], f"{look}: {name}")
            for name in ("run_back", "task_back", "task_forward", "task_closed", "settings", "project", "project_back", "settings_back"):
                self.assertTrue(steps[name]["same"], f"{look}: {name} reloaded the page")
        self.assertEqual(set(report["pasted"]), {each["address"] for each in expected.values()})
        for address, looks in report["pasted"].items():
            for look, seen in looks.items():
                self.assertEqual((seen["look"], seen["address"]), (look, address), f"{address} in {look}")
                self.assertIsNone(seen["note"], f"{address} in {look}")
        self.assertEqual(report["pasted"][at_task + "/run/r-1"]["clean"]["run"], "r-1")
        self.assertEqual(report["pasted"]["/settings/project/website"]["classic-new"]["project"], "website")
        note = words(read(dashboard.CLASSIC_PAGE))["en"]["view_not_found"]
        for address, seen in report["lost"].items():
            self.assertEqual({k: seen[k] for k in ("address", "tab", "task", "project", "run", "note")},
                             {**view("/today", "today"), "note": note}, address)


if __name__ == "__main__":
    unittest.main()
