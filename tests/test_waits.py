import json
import shutil
import subprocess
import unittest
from pathlib import Path

from kandu import notify, waits
from kandu.dashboard import task_statuses

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
TASK = "acme-org/website#1"
LINT = {"__typename": "CheckRun", "name": "lint <b>", "status": "COMPLETED", "conclusion": "FAILURE",
        "detailsUrl": "https://ci.example.com/run/1"}
UNIT = {"__typename": "StatusContext", "context": "unit", "state": "ERROR", "targetUrl": "javascript:alert(1)"}
GREEN = {"__typename": "CheckRun", "name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"}


class ReasonsTest(unittest.TestCase):
    def test_each_failing_check_is_named_plainly_with_a_safe_link(self):
        self.assertEqual(waits.failing_checks([LINT, UNIT, GREEN]),
                         [{"name": "lint b", "url": "https://ci.example.com/run/1"}, {"name": "unit", "url": None}])
        self.assertEqual(waits.check_reasons([LINT, UNIT]),
                         ["check lint b is failing: https://ci.example.com/run/1", "check unit is failing"])

    def test_what_a_held_merge_waits_on(self):
        check = waits.from_reasons(["CI is failing", "check lint is failing: https://ci.example.com/x"])
        self.assertEqual((check["on"], check["checks"], check["held"]),
                         ("check", [{"name": "lint", "url": "https://ci.example.com/x"}], True))
        blocker = waits.from_reasons(["blocker acme-org/website#2 is not merged yet"])
        self.assertEqual((blocker["on"], blocker["blockers"]), ("blocker", ["acme-org/website#2"]))
        problem = waits.from_reasons(["the PR has conflicts", "this is a one-way door"])
        self.assertEqual((problem["on"], problem["detail"], problem["held"]), ("decision", ["the PR has conflicts"], True))
        self.assertEqual(waits.from_reasons(["this is a one-way door"]), waits.yes())
        self.assertIsNone(waits.from_reasons(["CI is running", "the project is frozen"]))
        self.assertEqual(waits.from_reasons([]), waits.unknown())

    def test_a_pr_left_to_the_user_waits_for_the_yes_unless_something_holds_it(self):
        self.assertEqual(waits.settled(waits.USER_DECIDES), waits.yes())
        held = waits.settled("the user decides next; CI is failing; check lint is failing")
        self.assertEqual((held["on"], held["held"]), ("check", True))
        self.assertTrue(waits.user_decides("the user decides next; blocker a/b#1 is not merged yet"))
        self.assertFalse(waits.user_decides("CI is failing"))

    def test_the_words_say_what_where_and_the_next_step_in_both_languages(self):
        check = waits.from_reasons(["CI is failing", "check lint is failing: https://ci.example.com/x"])
        self.assertEqual(waits.lines(check, "en"), ["Waits on: the failing check lint.", "Where: lint: https://ci.example.com/x",
                                                    "Next step: open the lint log.", "Your yes counts once that is cleared."])
        self.assertEqual(waits.lines(waits.yes(), "en"), ["Waits on: your yes.", "Next step: approve it on the dashboard."])
        blocker = waits.from_reasons(["blocker acme-org/website#2 is not merged yet"])
        self.assertIn("Where: https://github.com/acme-org/website/issues/2", waits.lines(blocker, "en"))
        self.assertEqual(waits.lines(waits.decision(), "fa")[0], "منتظر: تصمیم تو.")


class StatusTest(unittest.TestCase):
    def event(self, kind, **fields):
        return {"type": kind, "task": TASK, "project": "website"} | fields

    def status(self, *events):
        return task_statuses([self.event("review_finished", blockers=0, criteria_met=True), *events])[TASK]

    def test_a_pr_left_to_the_user_says_it_waits_for_the_yes(self):
        found = self.status(self.event("decision", decision="approved", reason="the user decides next"))
        self.assertEqual((found["why"], found["wait"]), ("waits_for_you", waits.yes()))

    def test_a_held_pr_is_a_problem_and_does_not_offer_the_yes(self):
        found = self.status(self.event("decision", decision="approved",
                                       reason="the user decides next; CI is failing; check lint is failing"))
        self.assertEqual((found["state"], found["why"], found["detail"]), ("problem", "held_before_yes", "CI is failing"))
        self.assertEqual(found["wait"]["on"], "check")

    def test_a_held_merge_and_a_stuck_task_say_what_they_wait_on(self):
        held = self.status(self.event("decision", decision="merge_waits", reason="blocker acme-org/website#2 is not merged yet"))
        self.assertEqual(held["wait"]["on"], "blocker")
        stuck = self.status(self.event("task_finished", outcome="blocked"))
        self.assertEqual(stuck["wait"], waits.decision())
        self.assertIsNone(self.status()["wait"])


class NotifyTest(unittest.TestCase):
    def render(self, **event):
        message = notify.message_for({"type": "decision", "task": TASK, "project": "website", "ts": "t"} | event)
        return message, notify._render(message, "en")

    def test_a_ready_pr_says_the_next_step(self):
        message, text = self.render(decision="approved", reason="the user decides next")
        self.assertEqual(message.text, "pr_ready")
        self.assertIn("Waits on: your yes.\nNext step: approve it on the dashboard.", text)

    def test_a_held_pr_says_what_holds_it_instead_of_asking_for_the_yes(self):
        message, text = self.render(decision="approved",
                                    reason="the user decides next; CI is failing; check lint is failing: https://ci.example.com/x")
        self.assertEqual((message.kind, message.text), ("pr_ready", "pr_held"))
        self.assertIn("Next step: open the lint log.", text)
        self.assertNotIn("approve it", text)


class PageTest(unittest.TestCase):
    STUBS = """
const lang = "en", TEXT = {};
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const tr = v => v;
"""

    def setUp(self):
        self.page = (ROOT / "kandu" / "dashboard.html").read_text(encoding="utf-8")

    def words(self):
        start = self.page.index('  "wait_on": ')
        block = self.page[start:self.page.index('  "wait_held": ', start)]
        block += self.page[self.page.index('  "wait_held": ', start):].split("\n", 1)[0]
        return "const WORDS = {" + block.rstrip(",") + "};\nconst t = k => WORDS[k] ?? k;\n"

    @unittest.skipUnless(NODE, "needs node to run the page's script")
    def test_the_card_draws_the_wait_with_safe_links(self):
        begin = self.page.index("function waitLines(")
        script = self.page[begin:self.page.index("\n}\n", begin) + 2]
        fill = "const fillIn = (key, values) => Object.entries(values).reduce((text, [k, v]) => text.replaceAll(`{${k}}`, v), t(key));\n"
        done = subprocess.run([NODE, "-e", self.STUBS + self.words() + fill + script + """
console.log(JSON.stringify([
  waitLines({on: "check", checks: [{name: "lint", url: "https://ci.example.com/x"}], blockers: [], detail: [], held: true}),
  waitLines({on: "blocker", checks: [], blockers: ["acme-org/website#2"], detail: [], held: true}),
  waitLines({on: "yes", checks: [], blockers: [], detail: [], held: false}),
  waitLines(null)]));
"""], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        check, blocker, yes, none = json.loads(done.stdout)
        self.assertEqual(check[0], "Waits on: the failing check lint.")
        self.assertIn('href="https://ci.example.com/x"', check[1])
        self.assertEqual(check[2:], ["Next step: open the lint log.", "Your yes counts once that is cleared."])
        self.assertIn('href="https://github.com/acme-org/website/issues/2"', blocker[1])
        self.assertEqual(yes, ["Waits on: your yes.", "Next step: approve it here."])
        self.assertEqual(none, [])

    def test_the_words_are_there_in_both_languages(self):
        for key in ("wait_on", "wait_where", "wait_step", "wait_yes", "wait_decision", "wait_unknown", "wait_check",
                    "wait_checks", "wait_blocker", "wait_decision_on", "step_yes", "step_ask", "step_log", "step_check",
                    "step_checks", "step_blocker", "wait_held", "status_held_before_yes"):
            self.assertEqual(self.page.count(f'"{key}": '), 2, key)


if __name__ == "__main__":
    unittest.main()
