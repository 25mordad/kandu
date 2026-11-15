import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from kandu.config import load_config
from kandu.dashboard import Api, dispatch
from kandu.state import StateStore

ROOT = Path(__file__).resolve().parent.parent


class OverLimitTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        local = self.root / "local"
        local.mkdir()
        shutil.copy(ROOT / "config.example" / "kandu.example.yaml", local / "kandu.yaml")
        shutil.copy(ROOT / "config.example" / "projects.example.yaml", local / "projects.yaml")
        self.store = StateStore(self.root / "state")
        self.api = Api(load_config(local), self.store, None, self.root / "logs", "token", timer_on=lambda: True,
                       local_dir=local)
        self.api.config.projects[0].daily_token_limit = 100

    def spend(self, tokens, project="website", cache_read=0, when=None):
        self.store.record("run_finished", now=when, task="acme-org/website#1", project=project, run_id="r-1",
                          role="worker", models={"m": {"input": tokens, "output": 0, "cache_read": cache_read,
                                                        "cache_creation": 0}}, duration_ms=1, is_error=False)

    def over(self):
        return dispatch(self.api, "GET", "/api/v1/state")[1]["over_limit"]

    def test_a_project_shows_once_today_reaches_its_limit(self):
        self.spend(99, cache_read=1000)  # a cache read does not count, as for the supervisor
        self.assertEqual(self.over(), {})
        self.spend(1)
        self.assertEqual(self.over(), {"website": {"today": 100, "limit": 100}})

    def test_another_day_and_a_higher_limit_clear_it(self):
        self.spend(500, when=datetime(2026, 1, 1, 12, 0).astimezone())
        self.assertEqual(self.over(), {})
        self.spend(150)
        self.assertIn("website", self.over())
        self.api.config.projects[0].daily_token_limit = 200
        self.assertEqual(self.over(), {})


class PageTest(unittest.TestCase):
    def setUp(self):
        self.page = (ROOT / "kandu" / "dashboard.html").read_text(encoding="utf-8")

    def test_the_patch_and_the_card_turn_red(self):
        self.assertIn('${patch.over ? " over" : ""}" cx=', self.page)  # the meadow's soil
        self.assertIn("${limitBadge(overLimit(lastState, project.name))}", self.page)
        self.assertRegex(self.page, r"\.soil\.over \{[^}]*stroke:")  # an outline a pause never draws
        self.assertIn("lastState.over_limit]", self.page)  # the list redraws when it changes

    def test_the_legend_explains_the_red_while_it_shows_in_both_languages(self):
        self.assertIn("lastState?.over_limit", self.page.split("function renderLegend()", 1)[1].split("\n}\n", 1)[0])
        for key in ("over_limit", "over_limit_hint", "legend_over_limit"):
            self.assertEqual(self.page.count(f'"{key}": '), 2, key)


if __name__ == "__main__":
    unittest.main()
