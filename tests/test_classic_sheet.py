"""The Classic look opens a task in the Kandu Clean look's task sheet, over the page the user is on, wherever the task
shows. The sheet is Kandu Clean's as it is, and it shows what the Classic look's task window showed (#64): the PR's
state, the "waits for you" badge, the slices' English text, a done task's whole issue and cost, and a free flight."""
import json
import re
import subprocess
import tempfile
import unittest

from kandu import dashboard
from tests.test_dashboard import REPO
from tests.test_looks import NODE, ROOT, Served, browser, function, line, read, words

CLEAN = read(dashboard.PAGE)
# The Kandu Clean sheet's own code, which the Classic look takes as it is.
SAME = ("taskPhases", "stepOf", "sheetView", "sheetBody", "sheetFoot", "waitItems", "taskMarks", "statusText", "splitAccept", "showRun",
        "waitLines", "heldLines", "keptLines", "roundBox", "byHandBox", "approval", "testsView", "sendBack", "flightControls", "queued",
        "doneBox", "showTask")


class ClassicSheetTest(unittest.TestCase):
    def setUp(self):
        self.page = read(dashboard.CLASSIC_PAGE)
        self.text = words(self.page)

    def test_the_sheet_is_over_the_page_and_the_old_task_window_is_gone(self):
        self.assertIn('<dialog id="sheet" class="sheet" data-t-attr="aria-label" data-t="task_sheet"><div id="sheetbody" class="sheet-body"></div>'
                      '<div id="sheetfoot" class="sheet-foot"></div></dialog>', self.page)
        self.assertIn('$("sheet").showModal();', function(self.page, "showTask"))
        self.assertNotIn("showTab", function(self.page, "showTask"))
        for gone in ("showReviews", "statusLine", "splitBox"):
            self.assertNotIn(gone, self.page)

    def test_every_task_opens_it(self):
        self.assertGreaterEqual(self.page.count('data-review="${esc('), 8)
        self.assertIn('if (review) { confirming = false; sendingBack = false; showTask(review.dataset.review); }', self.page)
        self.assertIn('if (hit("close-sheet")) { $("sheet").close(); return; }', self.page)
        self.assertIn('if (e.target === $("sheet")) $("sheet").close();', self.page)
        self.assertIn('$("sheet").addEventListener("close", () => { shownTask = null; confirming = sendingBack = false; markView(); });', self.page)

    def test_its_code_is_the_kandu_clean_looks(self):
        for name in SAME:
            self.assertEqual(function(self.page, name), function(CLEAN, name), name)
        for start in ("const PHASES = ", "const QUIET_STEPS = ", "const WAIT_ORDER = "):
            self.assertEqual(line(self.page, start), line(CLEAN, start), start)
        self.assertNotIn("classicSheetExtras", self.page)
        self.assertNotIn("classicSheetExtras", read(dashboard.CLASSIC_NEW_PAGE))
        for name in SAME:
            self.assertEqual(function(read(dashboard.CLASSIC_NEW_PAGE), name), function(CLEAN, name), f"classic-new: {name}")

    def test_every_word_it_uses_is_in_both_languages(self):
        code = "".join(function(self.page, name) for name in SAME)
        keys = set(re.findall(r'\b(?:t|fillIn)\("([a-z_]+)"', code)) | {"task_sheet", "mark_question", "kind_check", "kind_blocker"}
        for lang in ("en", "fa"):
            for key in keys:
                self.assertIn(key, self.text[lang], f"{lang}: {key}")
        for phase in ("sizing", "implement", "review", "merge", "deploy", "live check"):
            self.assertIn(phase, self.text["values_fa"], phase)


@unittest.skipUnless(NODE and browser(), "needs node and a Chromium to open the pages")
class ClassicSheetBrowserTest(Served):
    CASES = [{"lang": "en", "width": 1280, "height": 900, "mobile": False}, {"lang": "fa", "width": 1280, "height": 900, "mobile": False},
             {"lang": "en", "width": 390, "height": 844, "mobile": True}, {"lang": "fa", "width": 390, "height": 844, "mobile": True}]
    SHOTS = ""  # a folder, to look at the sheet

    def setUp(self):
        super().setUp()
        pr = {"number": 30, "title": "Task 3", "url": "https://github.com/acme-org/website/pull/30", "state": "OPEN", "headRefOid": "abc", "comments": []}
        self.github.find_pr.side_effect = lambda repo, number: pr if number == 3 else None
        (self.root / "logs").mkdir(exist_ok=True)
        (self.root / "logs" / "r-1.jsonl").write_text("", encoding="utf-8")  # the bee's run log

    def test_a_click_on_a_task_opens_the_sheet_over_the_same_page_with_what_the_classic_look_showed(self):
        tasks = {"stuck": f"{REPO}#3", "working": f"{REPO}#2", "row": f"{REPO}#1"}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
            done = subprocess.run([NODE, str(ROOT / "tests" / "drive_classic_sheet.js"), browser(), self.base, profile, self.SHOTS,
                                   json.dumps([c | tasks for c in self.CASES])], capture_output=True, text=True, timeout=180)
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        report = json.loads(done.stdout)
        self.assertEqual(report["errors"], [])
        for case in report["results"]:
            name, steps = f'{case["lang"]} at {case["width"]}px', case["steps"]
            self.assertEqual(steps["look"], "classic", name)
            for where, task, tab in (("meadow", tasks["stuck"], "meadow"), ("working", tasks["working"], "meadow"), ("work", tasks["row"], "work")):
                seen = steps[where]
                self.assertTrue(seen["open"], f"{name}, {where}")
                self.assertEqual(seen["task"], task, f"{name}, {where}")
                self.assertEqual(seen["tab"], tab, f"{name}, {where}: the page under the sheet changed")
                self.assertFalse(seen["modal"], f"{name}, {where}")
                self.assertGreaterEqual(seen["phases"], 5, f"{name}, {where}")
                self.assertTrue(seen["steps"], f"{name}, {where}")
                self.assertTrue(seen["flight"], f"{name}, {where}: no free flight on the task")
                self.assertFalse(seen["sideways"], f"{name}, {where}: the sheet scrolls sideways")
            self.assertIn("https://github.com/acme-org/website/pull/30", steps["meadow"]["links"], name)
            self.assertIn("OPEN" if case["lang"] == "en" else "باز", steps["meadow"]["chips"], name)
            no_pr = words(read(dashboard.CLASSIC_PAGE))[case["lang"]]["no_pr"]
            self.assertIn(no_pr, steps["work"]["chips"], name)
            self.assertTrue(steps["closed"], name)
            self.assertTrue(steps["working"]["run"], name)
            self.assertTrue(steps["run"]["modal"] and steps["run"]["open"], f"{name}: the run log did not open over the sheet")
            self.assertEqual(steps["after_run"]["task"], tasks["working"], f"{name}: the sheet lost its task")


if __name__ == "__main__":
    unittest.main()
