"""The button that opens a waiting task's sheet reads "More information", in Persian "توضیحات", in every look (#144)."""
import json
import re
import subprocess
import unittest

from kandu import dashboard
from kandu.github import BoardItem
from tests.test_dashboard import REPO
from tests.test_looks import NODE, ROOT, Served, browser, profile_folder, read, words

MORE = {"en": "More information", "fa": "توضیحات"}
OLD = {"en": "Open", "fa": "باز کردن"}
PAGES = {"classic": dashboard.CLASSIC_PAGE, "classic-new": dashboard.CLASSIC_NEW_PAGE, "clean": dashboard.PAGE}
# The buttons that open something else keep their words.
KEPT = {"open_didban": ("Open Didban", "باز کردن دیدبان"), "open_zanbourdar": ("Open Zanbourdar", "باز کردن زنبوردار"),
        "open_kanduban": ("Open a Kanduban", "باز کردن کاندوبان"), "open_flight": ("Open a free flight", "باز کردن پرواز آزاد")}
QUESTION = "Should gift cards expire?"


class WordsTest(unittest.TestCase):
    def test_every_look_names_the_button_in_both_languages_and_keeps_the_others(self):
        for look, path in PAGES.items():
            page = read(path)
            text = words(page)
            for lang in ("en", "fa"):
                self.assertEqual(text[lang]["more_info"], MORE[lang], f"{look}, {lang}")
                self.assertNotIn("open", text[lang], f"{look}, {lang}: the old word is left")
            self.assertNotIn('t("open")', page, look)
            for key, kept in KEPT.items():
                self.assertEqual((text["en"][key], text["fa"][key]), kept, f"{look}: {key}")

    def test_the_asks_the_drop_asks_and_every_waiting_item_use_it(self):
        for look, path in PAGES.items():
            page = read(path)
            self.assertEqual(page.count('<button class="btn small" data-review="${esc(q.task)}">${t("more_info")}</button>'), 1, look)
            self.assertEqual(page.count('<button class="btn small" data-review="${esc(d.task)}">${t("more_info")}</button>'), 1, look)
            self.assertEqual(page.count('button(t("more_info"), `data-review="${esc(task)}"`)'), 1, look)


@unittest.skipUnless(NODE and browser(), "needs node and a Chromium to open the pages")
class BrowserTest(Served):
    """Every look, opened in a real browser with the queen asking, in English and Persian, from 1920 down to 390 pixels."""
    WIDTHS = [(1920, 1080, False), (1280, 900, False), (760, 1000, True), (390, 844, True)]

    def setUp(self):
        super().setUp()
        items = self.github.board_items.return_value
        items[2] = BoardItem(REPO, 3, items[2].title, "u", "Blocked", ["agent"], "## Goal\nA goal.\n\n## Blocked by\n#1\n")
        self.github.find_pr.return_value = None  # the asked task has no PR yet
        self.store.record("queen_decided", task=f"{REPO}#4", project="website", action="ask_user", note=QUESTION)
        self.store.record("queen_decided", task=f"{REPO}#3", project="website", action="drop_blocker", note="Not needed.")
        self.store.record("blocker_drop_asked", task=f"{REPO}#3", project="website", blocker=f"{REPO}#1", reason="Not needed.")
        self.store.record("queen_finished", run_id="r-1", brief="b", backlog=[], by_hand=[], splits=[])

    def test_it_reads_more_information_fits_and_still_opens_the_tasks_sheet(self):
        cases = [{"look": look, "lang": lang, "width": w, "height": h, "mobile": m}
                 for look in PAGES for lang in ("en", "fa") for w, h, m in self.WIDTHS]
        with profile_folder() as profile:
            done = subprocess.run([NODE, str(ROOT / "tests" / "drive_more_information.js"), browser(), self.base, profile, json.dumps(cases)],
                                  capture_output=True, text=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        report = json.loads(done.stdout)
        self.assertEqual(report["errors"], [])
        for case in report["results"]:
            name, seen, lang = f'{case["look"]}, {case["lang"]} at {case["width"]}px', case["seen"], case["lang"]
            self.assertEqual((seen["look"], seen["lang"]), (case["look"], lang), name)
            drawn = seen["asks"] + seen["card"]
            self.assertEqual(len(seen["asks"]), 2, name)  # the queen's ask and her drop ask
            if case["look"] == "clean":
                self.assertEqual(len(seen["card"]), 1, name)
            for button in drawn:
                self.assertEqual(button, {"text": MORE[lang], "cut": False, "outside": False, "empty": False}, name)
            waits = {item["task"]: item for item in seen["waits"]}
            self.assertTrue({f"{REPO}#3", f"{REPO}#4"} <= set(waits), name)
            for task, item in waits.items():
                self.assertEqual((item["label"], item["attrs"]), (MORE[lang], f'data-review="{task}"'), name)
            labels = [button["text"] for button in drawn] + [item["label"] for item in seen["waits"]]
            self.assertFalse(set(labels) & set(OLD.values()), name)
            self.assertEqual(case["sheet"]["task"], f"{REPO}#4", name)
            self.assertTrue(case["sheet"]["open"], name)
            self.assertIn(QUESTION, case["sheet"]["text"], name)  # at the step that needs the user: her question


if __name__ == "__main__":
    unittest.main()
