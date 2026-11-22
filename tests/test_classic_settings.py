"""The Classic look opens a project's settings in a pop-up over the same page when its name is clicked: on the meadow,
on the Work page and on the Settings page. The pop-up is the Kandu Clean look's, so it has every setting, the new
name among them, and nothing changes until Save."""
import json
import subprocess
import tempfile
import unittest

from kandu import dashboard
from tests.test_dashboard import TOKEN
from tests.test_looks import NODE, ROOT, Served, browser, function, line, read, run_js, words

CLEAN = read(dashboard.PAGE)
FUNCTIONS = ("settingsDraft", "settingsCalls", "mergeOnAsked", "saveStep", "applySettings", "settingControl", "settingsBody",
             "settingsFoot", "openSettings", "drawSettings", "changeDraft", "saveSettings", "closeSettings")
KEYS = ("set_group_models", "set_model_own", "set_model_global", "set_model_from_global", "set_model_same",
        "set_model_default", "set_model_worker", "set_model_reviewer", "set_model_splitter", "set_model_live_check",
        "set_model_bazras", "set_open", "set_open_hint", "set_group_work", "set_group_merge", "set_group_queen", "set_name", "set_name_hint",
        "set_run_label", "set_running", "set_me", "set_the_queen", "set_split_by", "set_one_way_by", "set_refused",
        "set_danger", "set_merge_ask", "set_merge_no", "set_merge_yes", "set_saved")


class ClassicSettingsPopupTest(unittest.TestCase):
    def setUp(self):
        self.page = read(dashboard.CLASSIC_PAGE)
        self.text = words(self.page)

    def test_the_pop_up_is_over_the_page_not_a_page_of_its_own(self):
        self.assertIn('<dialog id="setsheet" class="sheet" data-t-attr="aria-label" data-t="tab_settings"><div id="setbody" class="sheet-body"></div>'
                      '<div id="setfoot" class="sheet-foot"></div></dialog>', self.page)
        self.assertIn('$("setsheet").showModal();', function(self.page, "openSettings"))
        self.assertNotIn("showTab", function(self.page, "openSettings"))

    def test_every_project_name_opens_it(self):
        self.assertIn('<text class="patch-name clickable" data-name="${esc(patch.name)}" data-settings-open="${esc(patch.name)}"', self.page)
        self.assertIn('<tspan class="merge-mark clickable" data-settings-open="${esc(patch.name)}">', function(self.page, "mergeMark"))
        self.assertIn('class="textlink project-name" data-settings-open="${esc(project.name)}"', self.page)
        self.assertIn('data-settings-open="${esc(p.name)}"', function(self.page, "renderSettings"))
        self.assertIn('if (hit("settings-open")) { e.preventDefault(); openSettings(hit("settings-open").dataset.settingsOpen); return; }', self.page)

    def test_its_code_is_the_kandu_clean_looks_with_its_own_on_off_control(self):
        for name in FUNCTIONS:
            classic, clean = function(self.page, name), function(CLEAN, name)
            if name == "settingControl":
                clean = clean.replace('class="switch" role="switch"', 'class="flip" role="switch"')
            self.assertEqual(classic, clean, name)
        for start in ("const MODEL_KEYS = ", "const SETTING_GROUPS = ", "const SETTING_LABELS = ", "const SETTING_HINTS = "):
            self.assertEqual(line(self.page, start), line(CLEAN, start), start)

    def test_its_words_are_in_both_languages_and_the_same_as_in_kandu_clean(self):
        clean = words(CLEAN)
        for lang in ("en", "fa"):
            for key in KEYS:
                self.assertIn(key, self.text[lang], f"{lang}: {key}")
                if key != "set_open_hint":
                    self.assertEqual(self.text[lang][key], clean[lang][key], f"{lang}: {key}")
        self.assertIn("give the project a name", self.text["values_fa"])

    def test_a_removed_project_closes_its_pop_up(self):
        self.assertIn("closeSettings(name);", function(self.page, "removeProject"))

    @unittest.skipUnless(NODE, "needs node to run the page's script")
    def test_save_sends_each_change_as_the_settings_page_always_did_and_the_new_name_last(self):
        calls = run_js(line(self.page, "const MODEL_KEYS = ") + line(self.page, "const SETTING_GROUPS = ") + function(self.page, "settingsDraft") + function(self.page, "settingsCalls")
                       + 'const row = {name: "website", paused: false, max_parallel_workers: 1, priority: 2, daily_token_limit: 5, auto_merge: false,'
                       ' frozen: false, split_approval: "user", queen_answers_one_way: false};'
                       'const was = settingsDraft(row), now = {...was, name: "site", paused: true, max_parallel_workers: 3, priority: 1,'
                       ' frozen: true, split_approval: "queen"};'
                       'console.log(JSON.stringify(settingsCalls("website", was, now).map(c => c.path)));')
        self.assertEqual(calls, ["/projects/website/pause", "/parallel?project=website%3A3", "/projects/website/settings?priority=1",
                                 "/projects/website/settings?frozen=true", "/projects/website/settings?split_approval=queen",
                                 "/projects/website/settings?name=site"])


@unittest.skipUnless(NODE and browser(), "needs node and a Chromium to open the pages")
class ClassicSettingsBrowserTest(Served):
    CASES = [{"lang": "en", "width": 1280, "height": 900, "mobile": False}, {"lang": "fa", "width": 1280, "height": 900, "mobile": False},
             {"lang": "en", "width": 390, "height": 844, "mobile": True}, {"lang": "fa", "width": 390, "height": 844, "mobile": True}]

    def test_a_click_on_a_project_name_opens_its_settings_over_the_same_page_and_save_changes_them(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
            done = subprocess.run([NODE, str(ROOT / "tests" / "drive_classic_settings.js"), browser(), self.base, profile, TOKEN,
                                   json.dumps(self.CASES)], capture_output=True, text=True, timeout=180)
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        report = json.loads(done.stdout)
        self.assertEqual(report["errors"], [])
        every = ["name", "paused", "max_parallel_workers", "priority", "daily_token_limit", "multi_user", "auto_merge", "frozen", "watch_main",
                 "split_approval", "queen_answers_one_way", "model_worker", "model_reviewer", "model_splitter", "model_live_check",
                 "model_bazras"]
        for case in report["results"]:
            name, steps = f'{case["lang"]} at {case["width"]}px', case["steps"]
            self.assertEqual(steps["look"], "classic", name)
            for where, tab in (("meadow", "meadow"), ("work", "work"), ("settings", "settings")):
                seen = steps[where]
                self.assertTrue(seen["open"], f"{name}, {where}")
                self.assertEqual(seen["project"], "website", f"{name}, {where}")
                self.assertEqual(seen["tab"], tab, f"{name}, {where}: the page under the pop-up changed")
                self.assertEqual(seen["controls"], every, f"{name}, {where}")
                self.assertTrue(seen["remove"] and seen["save"], f"{name}, {where}")
                self.assertFalse(seen["sideways"], f"{name}, {where}: the pop-up scrolls sideways")
            self.assertTrue(steps["meadow_closed"], name)
            self.assertTrue(steps["work_card_kept"], f"{name}: the click folded the project's card")
            self.assertTrue(steps["saved_closed"], name)
            before = steps["priority"]["before"]
            self.assertEqual(steps["priority"], {"before": before, "unsaved": before, "after": before + 1}, name)
            self.assertIn("website", steps["toast"], name)


if __name__ == "__main__":
    unittest.main()


class ClassicRunTmpTest(unittest.TestCase):
    """Each Classic look lists the machines' run temporary folders (#120); Change opens the same pop-up as Kandu Clean."""

    def test_each_machine_shows_its_folder_and_change_opens_its_pop_up(self):
        machines = [{"name": "hive-box", "run_tmp": True, "run_tmp_dir": "~/kandu-tmp"},
                    {"name": "studio", "run_tmp": False, "run_tmp_dir": None}]
        for page in (read(dashboard.CLASSIC_PAGE), read(dashboard.CLASSIC_NEW_PAGE)):
            text = words(page)
            script = (f"const TEXT = {json.dumps(text)}; let lang = 'en';\n" + line(page, "const t = ")
                      + line(page, "const esc = ") + line(page, "const bdi = ") + function(page, "runTmpCard")
                      + f"\nconsole.log(JSON.stringify(runTmpCard({json.dumps(machines)})));")
            html = run_js(script)
            self.assertIn('<bdi dir="ltr">~/kandu-tmp</bdi>', html)
            self.assertIn(text["en"]["run_tmp_off"], html)
            self.assertIn('data-run-tmp="studio"', html)
            self.assertIn('if (hit("run-tmp")) { changeRunTmp(hit("run-tmp")); return; }', page)
            self.assertIn('id="runtmpsettings"', page)
            self.assertEqual(function(page, "changeRunTmp"), function(CLEAN, "changeRunTmp"))
            self.assertEqual({k: v for k, v in text["fa"].items() if k.startswith("run_tmp")},
                             {k: v for k, v in words(CLEAN)["fa"].items() if k.startswith("run_tmp")})
