"""The Classic new look: Classic made compact, with an explanation on every part."""
import json
import re
import subprocess
import unittest

from kandu import dashboard
from tests.test_looks import NODE, ROOT, Served, browser, function, line, profile_folder, read, run_js, words

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
    def test_token_numbers_are_short_with_a_letter_and_persian_digits_in_persian(self):
        values = [0, 950, 1500, 400000, 999999, 1000000, 1234567, 4000000, 12500000]
        seen = run_js(js(line(PAGE, "const num = "), line(PAGE, "const tokenShort = "),
                         f"const values = {json.dumps(values)};\n",
                         'const en = values.map(tokenShort); lang = "fa"; console.log(JSON.stringify({en, fa: values.map(tokenShort)}));'))
        self.assertEqual(seen["en"], ["0", "950", "1.5K", "400K", "1M", "1M", "1.2M", "4M", "12.5M"])
        self.assertEqual(seen["fa"], ["۰", "۹۵۰", "۱٫۵ه", "۴۰۰ه", "۱م", "۱م", "۱٫۲م", "۴م", "۱۲٫۵م"])

    @unittest.skipUnless(NODE, "needs node to run the page's script")
    def test_a_token_gauge_shows_the_use_in_bold_its_bar_and_the_limit_at_its_end(self):
        seen = run_js(js(line(PAGE, "const esc = "), line(PAGE, "const t = "), line(PAGE, "const num = "), line(PAGE, "const tokenShort = "),
                         line(PAGE, "const limitText = "), line(PAGE, "const fillIn = "), function(PAGE, "schip"), function(PAGE, "tokenGauge"),
                         """const cases = [{today: 1234567, limit: 4000000}, {today: 0, limit: 400000}, {today: 4100000, limit: 4000000}];
const en = cases.map(tokenGauge); lang = "fa"; console.log(JSON.stringify({en, fa: cases.map(tokenGauge)}));"""))
        some, none, over = seen["en"]
        self.assertRegex(some, r'^<span class="schip tgauge" title="[^"]*"><b>1\.2M</b><span class="mini"><i style="width:30\.8\d*%"></i></span><bdi class="of">4M</bdi></span>$')
        self.assertIn('title="Tokens today: \u20661,234,567 / 4,000,000\u2069\n' + TEXT["en"]["tokens_today_again"] + '"', some)
        self.assertIn('<b>0</b><span class="mini"><i style="width:0%"></i></span><bdi class="of">400K</bdi>', none)
        self.assertNotIn("over", none)
        self.assertIn('<span class="schip tgauge over" ', over)  # the used amount in red
        self.assertIn('<span class="mini over"><i style="width:100%"></i></span>', over)  # the bar in red, never past its end
        self.assertIn("<b>۱٫۲م</b>", seen["fa"][0])
        self.assertIn('<bdi class="of">۴م</bdi>', seen["fa"][0])
        self.assertIn("\u2066۱٬۲۳۴٬۵۶۷ / ۴٬۰۰۰٬۰۰۰\u2069", seen["fa"][0])
        for lang in ("en", "fa"):
            self.assertIn("{used}", TEXT[lang]["tokens_today"])
            self.assertTrue(TEXT[lang]["tokens_today_again"])
        self.assertNotEqual(TEXT["fa"]["tokens_today_again"], TEXT["en"]["tokens_today_again"])

    def test_every_patch_on_the_meadow_has_its_gauge_under_its_name_paused_ones_too(self):
        scene = function(PAGE, "renderScene")
        self.assertIn("patch.tokens = state.project_tokens?.[patch.name] || null;", function(PAGE, "fields"))
        self.assertIn("${gaugeUnder(patch)}", scene)
        self.assertNotIn("paused", function(PAGE, "gaugeUnder"))

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

    def setUp(self):
        super().setUp()
        self.store.pause_project("internal-tool")  # a paused patch has its gauge too
        for project, tokens in (("website", 2000000), ("internal-tool", 0)):  # one at its limit, one with none used
            self.store.record("run_finished", task=f"acme-org/{project}#1", project=project, run_id="r-9", role="worker",
                              models={"m": {"input": tokens, "output": 0, "cache_read": 0, "cache_creation": 0}}, duration_ms=1, is_error=False)

    def test_the_header_is_one_row_at_every_width_and_explains_its_buttons(self):
        cases = [{"lang": lang, "width": w, "height": h, "mobile": False} for w, h in self.WIDTHS for lang in ("en", "fa")]
        cases += [{"lang": lang, "width": 390, "height": 844, "mobile": True} for lang in ("en", "fa")]
        with profile_folder() as profile:
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
            gauges = seen["gauges"]
            self.assertEqual((gauges["count"], gauges["names"]), (2, 2), name)  # every patch, the paused one too
            self.assertEqual(gauges["bad"], [], name)  # under its own name, covering no name, switch or flower
            self.assertEqual(gauges["used"], ["2M", "0"] if case["lang"] == "en" else ["۲م", "۰"], name)
            self.assertEqual(gauges["over"], [True, False], name)
            self.assertIn(TEXT[case["lang"]]["tokens_today_again"], gauges["tip"], name)


class ServedClassicNewTest(Served):
    def test_a_browser_that_chose_it_opens_in_it(self):
        self.assertEqual(self.open("kandu-look=classic-new")[1], dashboard.CLASSIC_NEW_PAGE.read_bytes())


if __name__ == "__main__":
    unittest.main()


class StoppingInTheClassicLooksTest(unittest.TestCase):
    """After Stop, both classic looks say Kandu is stopping, with the bees still at work, and then stopped."""

    PAGES = {"classic": read(dashboard.CLASSIC_PAGE), "classic-new": PAGE}

    @unittest.skipUnless(NODE, "needs node to run the page's script")
    def test_the_state_says_stopping_with_how_many_bees_still_work_then_stopped(self):
        for name, page in self.PAGES.items():
            for lang, words_of in (("en", ("Stopping · 2 bees still at work", "Stopping · 1 bee still at work", "stopped")),
                                   ("fa", ("در حال توقف · ۲ زنبور هنوز سر کار هستند", "در حال توقف · ۱ زنبور هنوز سر کار است",
                                           "متوقف"))):
                seen = run_js(f"const TEXT = {json.dumps(words(page))};\nlet lang = {json.dumps(lang)};\n"
                              + line(page, "const t = ") + line(page, "const num = ") + line(page, "const faDigits = ")
                              + function(page, "tr") + function(page, "stopWord")
                              + """console.log(JSON.stringify([{stop_requested: true, stopping: 2}, {stop_requested: true, stopping: 1},
                                     {stop_requested: true, stopping: 0}, {stop_requested: false, stopping: 0}].map(stopWord)));""")
                self.assertEqual(seen, [*words_of, ""], f"{name} {lang}")
            self.assertIn("(stopWord(s) || tr(name))", function(page, "refreshState"), name)

    def test_the_stop_and_stop_the_queen_hints_say_the_bees_finish_their_step(self):
        for name, page in self.PAGES.items():
            text = words(page)
            self.assertIn("finish their step", text["en"]["hint_stop"], name)
            self.assertIn("قدمشان را تمام می‌کنند", text["fa"]["hint_stop"], name)
        self.assertIn("finish their step, nothing new starts", TEXT["en"]["hint_stop_queen"])
        self.assertNotIn("the running cycle ends", TEXT["en"]["hint_stop_queen"])
        self.assertIn("قدمشان را تمام می‌کنند", TEXT["fa"]["hint_stop_queen"])
        self.assertIn('$("stop").title = t("hint_stop");', function(self.PAGES["classic"], "refreshState"))
