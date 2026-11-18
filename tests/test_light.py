"""An open dashboard tab is light on CPU in every look, and Calm makes it near zero (#90)."""
import json
import re
import subprocess
import tempfile
import unittest

from kandu import dashboard
from tests.test_looks import NODE, ROOT, Served, browser, read, words

PAGES = dashboard.LOOKS


@unittest.skipUnless(NODE and browser(), "needs node and a Chromium to open the pages")
class LightTabTest(Served):
    """Each look opened in a real browser, with a bee at work, fed a burst of stream events, then turned Calm."""
    CASES = [{"look": "clean", "width": 1280, "height": 900, "mobile": False}, {"look": "clean", "width": 390, "height": 844, "mobile": True},
             {"look": "classic", "width": 1280, "height": 900, "mobile": False}, {"look": "classic-new", "width": 1280, "height": 900, "mobile": False}]

    def test_a_burst_reads_the_state_once_hidden_tabs_wait_the_meadow_stays_and_calm_stops_everything(self):
        with tempfile.TemporaryDirectory() as profile:
            done = subprocess.run([NODE, str(ROOT / "tests" / "drive_light.js"), browser(), self.base, profile, json.dumps(self.CASES)],
                                  capture_output=True, text=True, timeout=190)
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        report = json.loads(done.stdout)
        self.assertEqual(report["errors"], [])
        for case in report["results"]:
            name, seen = f'{case["look"]} at {case["width"]}px', case["seen"]
            self.assertNotIn("failed", seen, name)
            # A refresh draws only the tab on show; a hidden tab is drawn when it is opened.
            self.assertEqual(seen["hidden_tabs"], {"tasks": "", "settings": "", "bazras": "", "days": ""}, name)
            self.assertEqual(seen["opened_tab"], "drawn", name)
            # 20 events in 2 seconds: each line shows at once, and the state is read once, then once more 5 seconds on.
            self.assertTrue(seen["lines_at_once"], name)
            self.assertEqual(seen["burst_reads"], 1, name)
            self.assertEqual(seen["reads_after"], 2, name)
            self.assertTrue(seen["meadow_kept"], name)
            # Only what is live moves: the one bee at work flies and flaps; no flower sways.
            self.assertEqual(seen["motion"]["flights"], 1, name)
            moving = seen["motion"]["css"]
            self.assertEqual(moving.count("flap flying"), 2, f"{name}: {moving}")
            self.assertEqual([m for m in moving if m.startswith(("sway", "flap")) and m != "flap flying"], [], f"{name}: {moving}")
            # Calm: from the header, or More on a phone in Kandu Clean; nothing moves, the stream closes, a read a minute.
            self.assertEqual(seen["calm_from"], "more" if case["look"] == "clean" and case["mobile"] else "header", name)
            self.assertTrue(seen["calm"]["calm"], name)
            self.assertIn("true", seen["calm"]["pressed"], name)
            self.assertEqual(seen["calm_motion"], {"css": [], "flights": 0}, name)
            self.assertEqual(seen["calm_streams"], 0, name)
            self.assertTrue(seen["calm_minute"], name)
            # The browser remembers it: after a reload it is still Calm and opens no stream.
            self.assertTrue(seen["reloaded"]["calm"], name)
            self.assertEqual(seen["reloaded_streams"], 0, name)
            self.assertEqual(seen["reloaded_motion"], {"css": [], "flights": 0}, name)
            # Off again: the stream and the bee's flight come back.
            self.assertFalse(seen["off"]["calm"], name)
            self.assertEqual(seen["off_streams"], 1, name)
            self.assertEqual(seen["off_motion"]["flights"], 1, name)


class CalmWordsTest(unittest.TestCase):
    def test_every_look_says_calm_in_both_languages_and_stops_motion_as_reduced_motion_does(self):
        for look, path in PAGES.items():
            page, text = read(path), words(read(path))
            for lang in ("en", "fa"):
                for key in ("calm", "hint_calm"):
                    self.assertTrue(text[lang].get(key), f"{look} {lang} {key}")
            self.assertNotEqual(text["fa"]["calm"], text["en"]["calm"], look)
            style = page.split("<style>")[1].split("</style>")[0]
            self.assertRegex(style, r'\[data-calm="on"\] \*', look)

    def test_design_says_how_an_open_tab_stays_light_in_every_look(self):
        design = (ROOT / "DESIGN.md").read_text(encoding="utf-8")
        for part in ("Calm", "once every 5 seconds", "once a minute", "Kandu Clean, Classic and Classic new"):
            self.assertIn(part, design, part)


if __name__ == "__main__":
    unittest.main()
