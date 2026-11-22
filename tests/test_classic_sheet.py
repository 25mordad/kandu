"""The Classic look opens a task in the Kandu Clean look's task sheet, over the page the user is on, wherever the task
shows. The sheet keeps what the Classic look's task window showed and Kandu Clean's sheet does not show yet: those
are drawn by `classicSheetExtras`, parked as technical debt for the Kandu Clean sheet."""
import json
import re
import subprocess
import tempfile
import unittest

from kandu import dashboard
from tests.test_dashboard import REPO
from tests.test_looks import NODE, ROOT, Served, browser, function, line, read, run_js, words

CLEAN = read(dashboard.PAGE)
# The Kandu Clean sheet's own code, which the Classic look takes as it is.
SAME = ("taskPhases", "stepOf", "sheetView", "sheetBody", "sheetFoot", "waitItems", "taskMarks", "statusText", "splitAccept", "showRun",
        "waitLines", "heldLines", "keptLines", "roundBox", "byHandBox", "approval", "testsView", "sendBack", "flightControls", "queued")


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
        # The Classic look's showTask is Kandu Clean's, with the extras drawn in after.
        clean = [each for each in function(CLEAN, "showTask").splitlines()]
        classic = [each for each in function(self.page, "showTask").splitlines() if "extra" not in each]
        self.assertEqual(classic, clean)

    def test_every_word_it_uses_is_in_both_languages(self):
        code = "".join(function(self.page, name) for name in (*SAME, "classicSheetExtras", "showTask", "doneBox"))
        keys = set(re.findall(r'\b(?:t|fillIn)\("([a-z_]+)"', code)) | {"task_sheet", "mark_question", "kind_check", "kind_blocker"}
        for lang in ("en", "fa"):
            for key in keys:
                self.assertIn(key, self.text[lang], f"{lang}: {key}")
        for phase in ("sizing", "implement", "review", "merge", "deploy", "live check"):
            self.assertIn(phase, self.text["values_fa"], phase)

    @unittest.skipUnless(NODE, "needs node to run the page's script")
    def test_it_keeps_what_the_classic_look_showed(self):
        prelude = (f"const TEXT = {json.dumps(self.text)};\nlet lang = \"en\"; let lastState = {{flowers: {{}}}};\n"
                   "let flights = {open: [], flown: []}, confirming = false, sendingBack = false;\n"
                   + "".join(line(self.page, start) for start in ("const esc = ", "const t = ", "const locale = ", "const num = ",
                                                                  "const fillIn = ", "const yoursChip = "))
                   + "".join(function(self.page, name) for name in ("tr", "doneBox", "flightControls", "classicSheetExtras")))
        seen = run_js(prelude + """
const info = {issue: {body: "## Goal\\nThe whole issue, not only its goal."}, outcome: "merged",
              cost: {total: {total: 1500, cache_read: 500}, roles: {builder: {runs: 2, duration_ms: 600000, tokens: {total: 1200, cache_read: 200}}}}};
const proposal = {reason: "big", user_text: {reason: "بزرگ", slices: [{title: "یک", body: "متن"}]}, slices: [{title: "One", body: "English body"}]};
const merged = classicSheetExtras({task: "a/b#1", item: {column: "Done", yours: false}, review: {pr: {state: "MERGED"}}, info, proposal: null, language: "en"});
const none = classicSheetExtras({task: "a/b#2", item: {column: "Blocked", yours: true}, review: {pr: null}, info: null, proposal, language: "de"});
console.log(JSON.stringify({merged, none}));""")
        merged, none = seen["merged"], seen["none"]
        self.assertIn('<span class="chip">MERGED</span>', merged["state"])
        self.assertNotIn(self.text["en"]["waits_for_you"], merged["state"])
        for part in ("The whole issue, not only its goal.", self.text["en"]["task_full_issue"], self.text["en"]["cost_cached"],
                     self.text["en"]["cost_role"], "builder"):
            self.assertIn(part, merged["body"], part)
        self.assertIn(self.text["en"]["no_pr"], none["state"])
        self.assertIn(self.text["en"]["waits_for_you"], none["state"])
        self.assertIn(self.text["en"]["english_text"], none["body"])
        self.assertIn("English body", none["body"])
        self.assertNotIn(self.text["en"]["task_full_issue"], none["body"])
        for extras in (merged, none):
            self.assertIn('<span id="taskflight" class="flightbox">', extras["flight"])
            self.assertIn("data-fly=", extras["flight"])


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
