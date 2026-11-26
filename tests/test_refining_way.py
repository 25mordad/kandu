"""The install's refining way: rounds or grilling, one for the whole install (DESIGN §2)."""
import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from kandu import dashboard
from kandu.config import REFINING_WAYS, ConfigError, load_config, main, set_refining_way
from kandu.dashboard import dispatch
from tests.test_dashboard import Base, TOKEN
from tests.test_looks import function, read, words

EXAMPLES = Path(__file__).resolve().parent.parent / "config.example"
ROOT = EXAMPLES.parent


class ConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        shutil.copy(EXAMPLES / "kandu.example.yaml", self.local / "kandu.yaml")
        shutil.copy(EXAMPLES / "projects.example.yaml", self.local / "projects.yaml")

    def edit(self, change):
        path = self.local / "kandu.yaml"
        data = yaml.safe_load(path.read_text())
        change(data)
        path.write_text(yaml.safe_dump(data))

    def test_the_two_ways_are_rounds_and_grilling(self):
        self.assertEqual(REFINING_WAYS, ("rounds", "grilling"))

    def test_without_the_setting_the_way_is_rounds(self):
        self.edit(lambda d: d.pop("refining", None))
        self.assertEqual(load_config(self.local).settings.refining.way, "rounds")
        self.edit(lambda d: d.update(refining={}))
        self.assertEqual(load_config(self.local).settings.refining.way, "rounds")

    def test_the_config_names_grilling(self):
        self.edit(lambda d: d.update(refining={"way": "grilling"}))
        self.assertEqual(load_config(self.local).settings.refining.way, "grilling")

    def test_any_other_way_is_a_config_problem_that_names_the_setting(self):
        for wrong in ("Grilling", "rounds ", "", "interview"):
            self.edit(lambda d: d.update(refining={"way": wrong}))
            with self.assertRaises(ConfigError) as caught:
                load_config(self.local)
            self.assertIn(f"refining.way: must be one of rounds, grilling, got {wrong!r}", caught.exception.problems)

    def test_the_example_config_holds_the_setting_with_what_each_way_does(self):
        text = (EXAMPLES / "kandu.example.yaml").read_text(encoding="utf-8")
        block = text.split("\nrefining:")[1].split("\n\n")[0]
        self.assertRegex(block, r"\n  way: rounds +# rounds \(default\) = /kandu-task")
        self.assertIn("grilling = /kandu-grill-with-docs", block)
        self.assertEqual(load_config(self.local).settings.refining.way, "rounds")

    def test_it_is_written_line_by_line_and_comments_stay(self):
        before = (self.local / "kandu.yaml").read_text(encoding="utf-8")
        set_refining_way("grilling", self.local)
        after = (self.local / "kandu.yaml").read_text(encoding="utf-8")
        self.assertEqual(load_config(self.local).settings.refining.way, "grilling")
        self.assertEqual([l for l in before.splitlines() if "way:" not in l], [l for l in after.splitlines() if "way:" not in l])
        set_refining_way("rounds", self.local)
        self.assertEqual(load_config(self.local).settings.refining.way, "rounds")

    def test_a_block_that_is_missing_is_added(self):
        self.edit(lambda d: d.pop("refining", None))
        set_refining_way("grilling", self.local)
        self.assertEqual(load_config(self.local).settings.refining.way, "grilling")

    def test_a_wrong_way_writes_nothing(self):
        before = (self.local / "kandu.yaml").read_bytes()
        with self.assertRaises(ConfigError):
            set_refining_way("mode", self.local)
        self.assertEqual((self.local / "kandu.yaml").read_bytes(), before)

    def test_one_command_prints_the_way_and_nothing_else(self):
        for way in REFINING_WAYS:
            set_refining_way(way, self.local)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch("sys.argv", ["config", "--local", str(self.local),
                                                                                   "--refining-way"]):
                self.assertEqual(main(), 0)
            self.assertEqual(out.getvalue(), f"{way}\n")


class ApiTest(Base):
    def test_the_state_shows_the_way(self):
        self.assertEqual(dispatch(self.api, "GET", "/api/v1/state")[1]["refining_way"], "rounds")

    def test_the_way_changes_in_the_config_with_the_token_and_is_logged(self):
        local = self.root / "local"
        self.assertEqual(dispatch(self.api, "POST", "/api/v1/refining-way?way=grilling")[0], 403)
        self.assertEqual(load_config(local).settings.refining.way, "rounds")
        status, body = dispatch(self.api, "POST", "/api/v1/refining-way?way=grilling", TOKEN)
        self.assertEqual((status, body), (200, {"refining_way": "grilling"}))
        self.assertEqual(load_config(local).settings.refining.way, "grilling")
        self.assertEqual(dispatch(self.api, "GET", "/api/v1/state")[1]["refining_way"], "grilling")
        self.assertEqual(self.store.last_event("refining_way_changed")["way"], "grilling")

    def test_a_way_that_is_not_one_of_the_two_is_refused_and_nothing_is_written(self):
        local = self.root / "local"
        before = (local / "kandu.yaml").read_bytes()
        for wrong in ("mode", "", "Rounds"):
            status, body = dispatch(self.api, "POST", f"/api/v1/refining-way?way={wrong}", TOKEN)
            self.assertEqual(status, 400, wrong)
            self.assertIn("refining.way", body["error"])
        self.assertEqual((local / "kandu.yaml").read_bytes(), before)
        self.assertIsNone(self.store.last_event("refining_way_changed"))


class PagesTest(unittest.TestCase):
    PAGES = {"classic": dashboard.CLASSIC_PAGE, "classic-new": dashboard.CLASSIC_NEW_PAGE, "clean": dashboard.PAGE}

    def test_every_look_shows_and_changes_the_way_in_english_and_persian(self):
        for look, path in self.PAGES.items():
            page = read(path)
            text = words(page)
            for key in ("refining_way", "refining_way_hint", "refining_way_rounds", "refining_way_grilling"):
                self.assertIn(key, text["en"], (look, key))
                self.assertIn(key, text["fa"], (look, key))
                self.assertNotEqual(text["fa"][key], text["en"][key], (look, key))
            self.assertIn("refining_way_changed", text["values_fa"], look)
            html = function(page, "refiningWayHtml")
            self.assertIn("rounds", html, look)
            self.assertIn("grilling", html, look)
            if look == "clean":
                self.assertIn('data-switch="/refining-way?way=${value}"', html)
                self.assertIn("refiningWayHtml(s.refining_way)", function(page, "kanduHtml"))
            else:
                self.assertIn("/refining-way?way=", function(page, "changeRefiningWay"), look)
                self.assertIn('if (e.target.matches?.("[data-refining-way]")) changeRefiningWay(e.target);', page, look)
                self.assertIn("refiningWayHtml(s.refining_way)", function(page, "renderSettings"), look)
            self.assertIn("s.refining_way", function(page, "renderSettings").split("const key = ")[1].split("]);")[0], look)

    def test_setup_asks_it_and_config_changes_it(self):
        setup = (ROOT / ".claude" / "commands" / "kandu-setup.md").read_text(encoding="utf-8")
        change = (ROOT / ".claude" / "commands" / "kandu-config.md").read_text(encoding="utf-8")
        self.assertIn("(`refining.way`)? Rounds, the default", setup)
        self.assertIn("`refining.way`", change)

    def test_the_glossary_holds_the_word(self):
        glossary = (ROOT / "GLOSSARY.md").read_text(encoding="utf-8")
        self.assertIn("| **Refining way** | Which of Kandu's two ways turns a request into tasks in an install, rounds or "
                      "grilling", glossary)
        self.assertIn("Avoid: mode, flow, process", glossary)


if __name__ == "__main__":
    unittest.main()
