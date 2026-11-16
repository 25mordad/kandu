"""The Classic new look: Classic made compact, with an explanation on every part."""
import json
import re
import subprocess
import tempfile
import unittest

from kandu import dashboard
from tests.test_looks import NODE, ROOT, Served, browser, function, line, read, run_js, words

PAGE = read(dashboard.CLASSIC_NEW_PAGE)
TEXT = words(PAGE)


def js(*parts):
    return f"const TEXT = {json.dumps(TEXT)};\nlet lang = \"en\";\n" + "".join(parts)


class ClassicNewPageTest(unittest.TestCase):
    def test_it_is_its_own_look_in_the_menu_and_the_config(self):
        self.assertIn('<html lang="en" data-look="classic-new">', PAGE)
        self.assertIn('const LOOK_HERE = "classic-new";', PAGE)
        self.assertEqual(list(dashboard.LOOKS)[1], "classic-new")
        for lang in ("en", "fa"):
            self.assertIn("default_look_classic-new", words(read(dashboard.CLASSIC_PAGE))[lang])
            self.assertIn("default_look_classic-new", words(read(dashboard.PAGE))[lang])
        self.assertNotEqual(TEXT["fa"]["default_look_classic-new"], TEXT["en"]["default_look_classic-new"])

    def test_every_explanation_in_its_markup_has_words_in_both_languages(self):
        keys = set(re.findall(r'data-hint="(\w+)"', PAGE)) | set(re.findall(r'hint_\w+', PAGE.split("<script>")[1]))
        keys = {key for key in keys if not key.endswith("_")}
        self.assertGreater(len(keys), 15)
        for key in keys - {"hint_field_", "hint_"}:
            if key.startswith("hint_") or key in ("add_why", "settings_about", "look_hint"):
                for lang in ("en", "fa"):
                    self.assertTrue(TEXT[lang].get(key), f"{lang}: {key}")
                self.assertNotEqual(TEXT["fa"][key], TEXT["en"][key], key)

    @unittest.skipUnless(NODE, "needs node to run the page's script")
    def test_finished_tasks_go_newest_first_each_with_its_whole_hours_and_no_empty_cells(self):
        hour = 3600000
        seen = run_js(js(line(PAGE, "const esc = "), line(PAGE, "const t = "), line(PAGE, "const num = "), line(PAGE, "const numberOf = "),
                         line(PAGE, "const locale = "), "const COMB_ROW = 12; const shown = {}; const $ = id => (shown[id] ??= {});\n",
                         line(PAGE, "const hoursAgo = "), function(PAGE, "renderComb"),
                         f"""const at = h => new Date(Date.now() - h * {hour}).toISOString();
renderComb([{{task: "a/b#1", title: "old", project: "p", closed_at: at(30.5)}}, {{task: "a/b#2", title: "new", project: "p", closed_at: at(2.9)}},
            {{task: "a/b#3", title: "mid", project: "q", closed_at: at(12)}}], 5);
const full = shown.comb.innerHTML;
renderComb([], 5);
console.log(JSON.stringify({{full, empty: shown.comb.innerHTML}}));"""))
        self.assertEqual(re.findall(r'data-review="a/b#(\d)"', seen["full"]), ["2", "3", "1"])
        self.assertEqual(re.findall(r'<small class="ago" dir="ltr">([^<]*)</small>', seen["full"]), ["2h ago", "12h ago", "30h ago"])
        self.assertNotIn('<span class="cell">', seen["full"])
        self.assertEqual(seen["empty"].count('<span class="cell">'), 12)
        self.assertIn(".cells { direction:ltr; }", PAGE)

    @unittest.skipUnless(NODE, "needs node to run the page's script")
    def test_three_tabs_over_the_meadow_show_projects_that_are_on_with_tasks_paused_or_all(self):
        groups = line(PAGE, "  const groups = ").strip()
        seen = run_js(js(line(PAGE, "const esc = "), line(PAGE, "const t = "), line(PAGE, "const num = "),
                         "let field = \"off\"; const shown = {}; const $ = id => (shown[id] ??= {innerHTML: \"\"});\n",
                         line(PAGE, "const FIELDS = "), function(PAGE, "renderFieldTabs"),
                         """const all = [{name: "on", paused: false, tasks: [{column: "Ready"}]}, {name: "done", paused: false, tasks: [{column: "Done"}]},
                                      {name: "empty", paused: false, tasks: []}, {name: "off", paused: true, tasks: [{column: "Ready"}]}];
""", groups, """
renderFieldTabs(groups);
console.log(JSON.stringify({names: Object.fromEntries(Object.entries(groups).map(([k, v]) => [k, v.map(p => p.name)])), tabs: shown.fieldtabs.innerHTML}));"""))
        self.assertEqual(seen["names"], {"active": ["on"], "off": ["off"], "all": ["on", "done", "empty", "off"]})
        self.assertEqual(re.findall(r'data-field="(\w+)" aria-pressed="(\w+)"', seen["tabs"]), [("active", "false"), ("off", "true"), ("all", "false")])
        self.assertEqual(re.findall(r"<small>(\d+)</small>", seen["tabs"]), ["1", "1", "4"])
        self.assertIn(TEXT["en"]["hint_field_off"], seen["tabs"])
        self.assertIn("const projects = groups[field] || groups.active;", function(PAGE, "renderScene"))
        self.assertIn('if (hit("field")) { field = hit("field").dataset.field; save(FIELD_KEY, field);', PAGE)

    @unittest.skipUnless(NODE, "needs node to run the page's script")
    def test_wake_the_queen_stops_her_while_a_cycle_runs_and_resumes_a_stopped_kandu_first(self):
        handler = PAGE.split('$("wake").onclick = ')[1].split("\n};\n")[0] + "\n}"
        seen = run_js(f"""const calls = []; let lastState = null;
const act = async (button, path) => {{ calls.push(path); return {{}}; }};
const wake = {handler};
(async () => {{
  await wake({{currentTarget: {{dataset: {{mode: "stop"}}}}}});
  lastState = {{stop_requested: true}}; await wake({{currentTarget: {{dataset: {{mode: "wake"}}}}}});
  lastState = {{stop_requested: false}}; await wake({{currentTarget: {{dataset: {{mode: "wake"}}}}}});
  console.log(JSON.stringify(calls));
}})();""")
        self.assertEqual(seen, ["/stop", "/resume", "/wake", "/wake"])
        state = function(PAGE, "refreshState")
        self.assertIn('const working = s.supervisor === "awake" && !s.stop_requested;', state)
        self.assertIn('$("wakelabel").textContent = t(working ? "stop_queen" : "wake_queen");', state)

    @unittest.skipUnless(NODE, "needs node to run the page's script")
    def test_a_usage_chip_shows_its_use_its_bar_and_its_limit_after_the_bar(self):
        seen = run_js(js(line(PAGE, "const esc = "), function(PAGE, "schip"),
                         'console.log(JSON.stringify(schip("5h", "49%", {attrs: "data-limits", bar: {value: 49, limit: 90, over: false}, after: \'<bdi class="of">90%</bdi>\', title: "5-hour"})));'))
        self.assertRegex(seen, r'^<button class="schip" data-limits title="5-hour"><span class="schip-l">5h</span><b>49%</b><span class="mini">.*</span><bdi class="of">90%</bdi></button>$')
        self.assertIn('after: `<bdi class="of">${pct(limit)}</bdi>`', function(PAGE, "renderStats"))

    @unittest.skipUnless(NODE, "needs node to run the page's script")
    def test_the_header_flights_say_only_the_sessions_name(self):
        seen = run_js(js(line(PAGE, "const esc = "), line(PAGE, "const t = "), function(PAGE, "headerFlight"),
                         """console.log(JSON.stringify([headerFlight(null, "didban"), headerFlight({status: "open", url: "https://x", key: "k"}, "zanbourdar")]));"""))
        closed, open_ = seen
        self.assertIn('data-fly="didban"', closed)
        self.assertIn(">Didban</button>", closed)
        self.assertIn(">Zanbourdar ↗</a>", open_)
        self.assertIn('data-land="k"', open_)
        self.assertIn(">×</button>", open_)


@unittest.skipUnless(NODE and browser(), "needs node and a Chromium to open the page")
class ClassicNewBrowserTest(Served):
    """The page in a real browser: one header row from a wide screen to a tablet, nothing past the page's edge, and the
    explanation under the pointer, in English and Persian."""
    WIDTHS = [(1920, 1080), (1440, 900), (1280, 800), (1024, 768), (1080, 1920), (768, 1024)]

    def test_the_header_is_one_row_at_every_width_and_explains_its_buttons(self):
        cases = [{"lang": lang, "width": w, "height": h, "mobile": False} for w, h in self.WIDTHS for lang in ("en", "fa")]
        cases += [{"lang": lang, "width": 390, "height": 844, "mobile": True} for lang in ("en", "fa")]
        with tempfile.TemporaryDirectory() as profile:
            done = subprocess.run([NODE, str(ROOT / "tests" / "drive_classic_new.js"), browser(), self.base, profile, json.dumps(cases)],
                                  capture_output=True, text=True, timeout=180)
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        report = json.loads(done.stdout)
        self.assertEqual(report["errors"], [])
        for case in report["results"]:
            name, seen = f'{case["lang"]} at {case["width"]}px', case["seen"]
            self.assertEqual((seen["look"], seen["lang"]), ("classic-new", case["lang"]), name)
            self.assertFalse(seen["barPast"], name)
            self.assertEqual(seen["past"], [], name)
            if not case["mobile"]:
                self.assertLess(seen["header"], 50, name)
            self.assertTrue(seen["hive"], name)
            self.assertGreaterEqual(seen["stats"], 6, name)
            self.assertEqual(seen["fields"], ["active", "off", "all"], name)
            self.assertEqual(seen["wake"], "stop", name)  # the fixture's supervisor is awake
            self.assertEqual(seen["wakeTip"], TEXT[case["lang"]]["hint_stop_queen"], name)
            self.assertEqual(seen["restartTip"], TEXT[case["lang"]]["hint_restart"], name)


class ServedClassicNewTest(Served):
    def test_a_browser_that_chose_it_opens_in_it(self):
        self.assertEqual(self.open("kandu-look=classic-new")[1], dashboard.CLASSIC_NEW_PAGE.read_bytes())


if __name__ == "__main__":
    unittest.main()
