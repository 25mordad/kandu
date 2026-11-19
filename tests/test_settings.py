import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from kandu.config import ConfigError, load_config, set_default_look, set_pace, set_project_values
from kandu.dashboard import Api, dispatch
from kandu.state import StateStore

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "secret-token"
NODE = shutil.which("node")


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.local = self.root / "local"
        self.local.mkdir()
        shutil.copy(ROOT / "config.example" / "kandu.example.yaml", self.local / "kandu.yaml")
        shutil.copy(ROOT / "config.example" / "projects.example.yaml", self.local / "projects.yaml")

    def registry(self):
        return (self.local / "projects.yaml").read_text(encoding="utf-8")

    def settings(self):
        return (self.local / "kandu.yaml").read_text(encoding="utf-8")

    def project(self, name="website"):
        return next(p for p in load_config(self.local).projects if p.name == name)


class ProjectValuesTest(Base):
    def test_each_value_changes_its_own_line_and_its_comment_stays(self):
        before = self.registry()
        set_project_values("website", {"priority": 3, "daily_token_limit": 200000, "auto_merge": True, "frozen": True},
                           self.local)
        project = self.project()
        self.assertEqual((project.priority, project.daily_token_limit, project.auto_merge, project.frozen),
                         (3, 200000, True, True))
        after = self.registry()
        self.assertIn("    priority: 3                        # 1 = highest", after)
        self.assertIn("    auto_merge: true                   # overrides supervisor.auto_merge", after)
        self.assertIn("    frozen: true                       # true: nothing merges by itself", after)
        self.assertEqual(len(after.splitlines()), len(before.splitlines()))
        changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
        self.assertEqual(len(changed), 4)

    def test_only_the_named_entry_changes(self):
        other = self.project("internal-tool")
        set_project_values("website", {"daily_token_limit": 5}, self.local)
        self.assertEqual(self.project("internal-tool").daily_token_limit, other.daily_token_limit)

    def test_a_key_the_entry_lacks_is_added_to_it(self):
        self.assertNotIn("frozen", self.registry().split("- name: internal-tool")[1])
        set_project_values("internal-tool", {"frozen": True}, self.local)
        self.assertTrue(self.project("internal-tool").frozen)
        self.assertFalse(self.project().frozen)

    def test_a_wrong_value_writes_nothing(self):
        before = self.registry()
        for values in ({"priority": 0}, {"daily_token_limit": -1}, {"frozen": "yes"}, {"priority": True},
                       {"colour": 1}, {}):
            with self.assertRaises(ConfigError, msg=values):
                set_project_values("website", values, self.local)
        with self.assertRaises(ConfigError):
            set_project_values("ghost", {"priority": 2}, self.local)
        self.assertEqual(self.registry(), before)


class PaceTest(Base):
    def test_the_supervisor_block_takes_both_and_the_rest_stays(self):
        set_pace({"wake_every_minutes": 30, "max_cycle_minutes": 25}, self.local)
        supervisor = load_config(self.local).settings.supervisor
        self.assertEqual((supervisor.wake_every_minutes, supervisor.max_cycle_minutes), (30, 25))
        self.assertIn("# SSH alias from ~/.ssh/config", self.settings())

    def test_a_wrong_value_writes_nothing(self):
        before = self.settings()
        for values in ({"wake_every_minutes": 0}, {"max_cycle_minutes": "soon"}, {"max_parallel_sizers": 3}, {}):
            with self.assertRaises(ConfigError, msg=values):
                set_pace(values, self.local)
        self.assertEqual(self.settings(), before)


class ApiTest(Base):
    def setUp(self):
        super().setUp()
        self.store = StateStore(self.root / "state")
        self.api = Api(load_config(self.local), self.store, None, self.root / "logs", TOKEN, timer_on=lambda: True,
                       local_dir=self.local)

    def post(self, target, token=TOKEN):
        return dispatch(self.api, "POST", target, token)

    def row(self, name="website"):
        return next(r for r in dispatch(self.api, "GET", "/api/v1/state")[1]["project_settings"] if r["name"] == name)

    def test_the_registrys_values_change_from_the_settings_and_are_logged(self):
        status, body = self.post("/api/v1/projects/website/settings?priority=4&frozen=true&daily_token_limit=300000")
        self.assertEqual((status, body), (200, {"priority": 4, "frozen": True, "daily_token_limit": 300000}))
        row = self.row()
        self.assertEqual((row["priority"], row["frozen"], row["daily_token_limit"]), (4, True, 300000))
        self.assertEqual(self.project().priority, 4)
        changes = {e["setting"]: (e["old"], e["new"]) for e in self.store.events(self.store.today())
                   if e["type"] == "project_setting_changed"}
        self.assertEqual(changes["priority"], (1, 4))
        self.assertEqual(changes["frozen"], (False, True))
        self.assertEqual(self.store.project_settings(), {})  # the registry holds them, not the state

    def test_auto_merge_reads_the_supervisors_default_until_the_project_sets_one(self):
        self.assertFalse(self.row("internal-tool")["auto_merge"])
        self.post("/api/v1/projects/internal-tool/settings?auto_merge=true")
        self.assertTrue(self.row("internal-tool")["auto_merge"])
        self.assertIs(self.project("internal-tool").auto_merge, True)

    def test_a_state_switch_and_a_registry_value_go_in_one_call(self):
        status, body = self.post("/api/v1/projects/website/settings?split_approval=queen&priority=2")
        self.assertEqual((status, body), (200, {"split_approval": "queen", "priority": 2}))
        self.assertEqual(self.store.project_settings(), {"website": {"split_approval": "queen"}})

    def test_a_wrong_value_changes_nothing(self):
        before = self.registry()
        for query in ("priority=0", "priority=two", "frozen=yes", "split_approval=queen&priority=0",
                      "daily_token_limit="):
            self.assertEqual(self.post(f"/api/v1/projects/website/settings?{query}")[0], 400, query)
        self.assertEqual(self.post("/api/v1/projects/website/settings?priority=2", "")[0], 403)
        self.assertEqual(self.registry(), before)
        self.assertEqual(self.store.project_settings(), {})
        self.assertIsNone(self.store.last_event("project_setting_changed"))

    def test_each_role_shows_the_projects_own_model_or_none_beside_the_global_one(self):
        own, other = self.row("internal-tool"), self.row("website")
        self.assertEqual({k: own[k] for k in own if k.startswith("model_")},
                         {"model_worker": "claude-opus-5-5", "model_reviewer": "", "model_splitter": "",
                          "model_live_check": "", "model_bazras": ""})
        self.assertEqual(other["model_worker"], "")
        self.assertEqual(own["global_models"], {"worker": "", "reviewer": "", "splitter": "claude-sonnet-5-5",
                                                "live_check": "claude-sonnet-5-5", "bazras": ""})
        self.assertNotIn("model_queen", own)  # the queen's model is global only

    def test_a_model_is_set_and_set_back_to_the_global_one_and_each_change_is_logged(self):
        status, body = self.post("/api/v1/projects/website/settings?model_worker=claude-sonnet-5-5&model_splitter=claude-haiku-5-5")
        self.assertEqual((status, body), (200, {"model_worker": "claude-sonnet-5-5", "model_splitter": "claude-haiku-5-5"}))
        self.assertEqual(self.project().models, {"worker": "claude-sonnet-5-5", "splitter": "claude-haiku-5-5"})
        self.assertEqual(self.row()["model_worker"], "claude-sonnet-5-5")
        self.assertEqual(self.project("internal-tool").models, {"worker": "claude-opus-5-5"})  # the other keeps its own
        self.assertEqual(self.api.config.model_for(self.api.config.projects[0], "worker"), "claude-sonnet-5-5")
        status, body = self.post("/api/v1/projects/website/settings?model_splitter=global")
        self.assertEqual((status, body), (200, {"model_splitter": ""}))
        self.assertEqual(self.project().models, {"worker": "claude-sonnet-5-5"})
        self.assertEqual(self.row()["model_splitter"], "")
        self.assertEqual(self.api.config.model_for(self.api.config.projects[0], "splitter"), "claude-sonnet-5-5")
        changes = [(e["setting"], e["old"], e["new"]) for e in self.store.events(self.store.today())
                   if e["type"] == "project_setting_changed"]
        self.assertEqual(changes, [("model_worker", None, "claude-sonnet-5-5"), ("model_splitter", None, "claude-haiku-5-5"),
                                   ("model_splitter", "claude-haiku-5-5", None)])

    def test_a_wrong_role_or_model_changes_nothing(self):
        before = self.registry()
        for query in ("model_queen=claude-sonnet-5-5", "model_wroker=claude-sonnet-5-5", "model_worker=--yolo",
                      "priority=2&model_worker=--yolo"):
            status, body = self.post(f"/api/v1/projects/website/settings?{query}")
            self.assertEqual(status, 400, query)
            self.assertIn("website.models", body["error"], query)
        self.assertEqual(self.registry(), before)
        self.assertIsNone(self.store.last_event("project_setting_changed"))

    def test_the_pace_changes_and_shows_in_the_state(self):
        self.assertEqual(dispatch(self.api, "GET", "/api/v1/state")[1]["pace"],
                         {"wake_every_minutes": 60, "max_cycle_minutes": 50})
        status, body = self.post("/api/v1/pace?wake_every_minutes=30")
        self.assertEqual((status, body), (200, {"pace": {"wake_every_minutes": 30, "max_cycle_minutes": 50}}))
        self.assertEqual(load_config(self.local).settings.supervisor.wake_every_minutes, 30)
        changed = self.store.last_event("pace_changed")
        self.assertEqual((changed["old"]["wake_every_minutes"], changed["new"]["wake_every_minutes"]), (60, 30))
        for query in ("wake_every_minutes=0", "max_cycle_minutes=soon", "colour=1", ""):
            self.assertEqual(self.post(f"/api/v1/pace?{query}")[0], 400, query)
        self.assertEqual(self.post("/api/v1/pace?max_cycle_minutes=20", "")[0], 403)

    def state(self):
        return dispatch(self.api, "GET", "/api/v1/state")[1]

    def test_the_default_look_is_read_from_the_config(self):
        self.assertEqual(self.state()["default_look"], "classic")
        set_default_look("clean", self.local)
        self.api.refresh_config()
        self.assertEqual(self.state()["default_look"], "clean")

    def test_the_default_look_is_saved_in_the_config_with_the_dashboards_token(self):
        status, body = self.post("/api/v1/default-look?look=clean")
        self.assertEqual((status, body), (200, {"default_look": "clean"}))
        self.assertEqual(load_config(self.local).settings.dashboard.look, "clean")
        self.assertEqual(self.state()["default_look"], "clean")
        self.assertIn("# stay on this machine", self.settings())
        status, body = self.post("/api/v1/default-look?look=classic")
        self.assertEqual((status, body), (200, {"default_look": "classic"}))
        self.assertEqual(load_config(self.local).settings.dashboard.look, "classic")
        self.assertEqual(self.state()["default_look"], "classic")

    def test_a_wrong_default_look_or_no_token_changes_nothing(self):
        before = self.settings()
        for query in ("look=honeycomb", "look=", "", "look=Clean"):
            self.assertEqual(self.post(f"/api/v1/default-look?{query}")[0], 400, query)
        self.assertEqual(self.post("/api/v1/default-look?look=clean", "")[0], 403)
        self.assertEqual(self.post("/api/v1/default-look?look=clean", "wrong")[0], 403)
        self.assertEqual(self.settings(), before)
        self.assertEqual(self.state()["default_look"], "classic")


class DefaultLookTest(Base):
    """The install's default look in `kandu.yaml`'s `dashboard` block, changed line by line."""

    def test_it_is_written_in_the_dashboard_block_and_the_rest_stays(self):
        before = self.settings()
        set_default_look("clean", self.local)
        self.assertEqual(load_config(self.local).settings.dashboard.look, "clean")
        after = self.settings()
        self.assertIn("# stay on this machine", after)
        self.assertEqual([(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b],
                         [(line, "  look: clean") for line in before.splitlines() if line.startswith("  look:")])
        set_default_look("classic", self.local)
        self.assertEqual(load_config(self.local).settings.dashboard.look, "classic")

    def test_a_config_without_it_gets_the_line(self):
        path = self.local / "kandu.yaml"
        path.write_text("\n".join(l for l in self.settings().splitlines() if not l.startswith("  look:")) + "\n",
                        encoding="utf-8")
        self.assertEqual(load_config(self.local).settings.dashboard.look, "classic")
        set_default_look("clean", self.local)
        self.assertEqual(load_config(self.local).settings.dashboard.look, "clean")
        self.assertIn("  port: 8790", self.settings())

    def test_a_look_that_is_not_one_of_the_two_writes_nothing(self):
        before = self.settings()
        for look in ("honeycomb", "", "Clean", None, True):
            with self.assertRaises(ConfigError, msg=look):
                set_default_look(look, self.local)
        self.assertEqual(self.settings(), before)


class PageTest(unittest.TestCase):
    def setUp(self):
        self.page = (ROOT / "kandu" / "dashboard.html").read_text(encoding="utf-8")

    def function(self, name):
        begin = self.page.index(f"function {name}(")
        return self.page[begin:self.page.index("\n}\n", begin) + 2]

    @unittest.skipUnless(NODE, "needs node to run the page's script")
    def test_a_stepper_sends_the_next_number_and_never_goes_below_one(self):
        script = "const t = k => k, num = String, esc = s => String(s);\n" + self.function("stepper") + """
console.log(JSON.stringify([stepper(1, "/parallel?total={n}", "all"), stepper(60, "/pace?wake_every_minutes={n}", "wake", 5)]));
"""
        done = subprocess.run([NODE, "-"], input=script, capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        one, wake = json.loads(done.stdout)
        self.assertIn('data-step="-1" data-step-path="/parallel?total={n}" data-step-from="1"', one)
        self.assertRegex(one, r'data-step="-1"[^>]*disabled')
        self.assertNotRegex(one, r'data-step="1"[^>]*disabled')
        self.assertIn('data-step="5"', wake)

    def test_each_project_setting_is_in_its_pop_up_and_kandus_pace_beside_it(self):
        groups = self.page.split("const SETTING_GROUPS = ", 1)[1].split("\n", 1)[0]
        for setting in ("paused", "max_parallel_workers", "priority", "daily_token_limit", "auto_merge", "frozen",
                        "split_approval", "queen_answers_one_way"):
            self.assertIn(f'"{setting}"', groups)
        pace = self.function("kanduHtml")
        for path in ("/parallel?total={n}", "/pace?wake_every_minutes={n}", "/pace?max_cycle_minutes={n}"):
            self.assertIn(path, pace)
        self.assertIn('if (hit("step")) { changeStep(hit("step")); return; }', self.page)

    def test_the_meadow_shows_the_merge_mode_and_frozen_wins(self):
        mode = self.function("mergeMode")
        self.assertIn('row.frozen ? "frozen" : row.auto_merge ? "auto" : "waits"', mode)
        self.assertIn('data-tab="settings"', self.function("mergeMark"))

    def test_the_words_are_there_in_both_languages(self):
        for key in ("pace_title", "pace_workers", "pace_wake", "pace_cycle", "pace_hint", "setting_workers",
                    "setting_priority", "setting_tokens", "setting_auto_merge", "setting_frozen", "less", "more",
                    "mode_auto", "mode_waits", "mode_frozen", "mode_auto_hint", "mode_waits_hint",
                    "mode_frozen_hint"):
            self.assertEqual(self.page.count(f'"{key}": '), 2, key)


if __name__ == "__main__":
    unittest.main()
