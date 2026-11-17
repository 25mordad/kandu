"""Codex's usage and limits beside Claude's in every look, in English and Persian, only while a project uses Codex
(#79). Each look's own functions run in node, with its own words."""
import json
import unittest

from kandu import dashboard
from tests.test_looks import NODE, function, line, read, run_js, words

PAGES = {"classic": read(dashboard.CLASSIC_PAGE), "classic-new": read(dashboard.CLASSIC_NEW_PAGE), "clean": read(dashboard.PAGE)}
HELPERS = ("const esc = ", "const locale = ", "const t = ", "const num = ", "const pct = ")
CODEX = {"usage": {"session": 12, "week": 61}, "limits": {"session": 50, "week": 60}}
STATE = {"usage": {"session": 30, "week": 40}, "limits": {"session": 70, "week": 60}}
DIGITS = {"en": ("12%", "61%", "50%"), "fa": ("۱۲٪", "۶۱٪", "۵۰٪")}


def js(look, lang, parts, script):
    page = PAGES[look]
    return run_js(f"const TEXT = {json.dumps(words(page))};\nlet lang = {json.dumps(lang)};\n"
                  + "".join(line(page, start) for start in HELPERS) + "".join(parts) + script)


def fns(look, *names):
    return [function(PAGES[look], name) for name in names]


# The dialog the change buttons open, with what it posts once saved.
DIALOG = """
const nodes = {}, posted = [];
const $ = id => nodes[id] ||= {value: "", textContent: "", querySelector: () => nodes.title ||= {textContent: ""},
  addEventListener: (kind, then) => { nodes.closed = then; }, showModal: () => {}};
const act = (button, path) => posted.push(path);
function open(state, codex) {
  lastState = state;
  changeLimits({dataset: {limits: codex ? "codex" : ""}});
  const seen = {title: nodes.title.textContent, session: $("limitsession").value, week: $("limitweek").value};
  $("limitsbox").returnValue = "yes";
  $("limitsession").value = 55;
  nodes.closed();
  return {...seen, posted: posted.pop()};
}
let lastState;
"""


@unittest.skipUnless(NODE, "needs node to run the page's script")
class EveryLookTest(unittest.TestCase):
    def test_each_look_has_codex_words_in_english_and_persian(self):
        for look, page in PAGES.items():
            text = words(page)
            keys = ("codex_session", "codex_week", "codex_limits_title") + (("s_codex_session", "s_codex_week") if look == "classic-new" else ()) \
                + (("codex_limit_session", "codex_limit_week") if look == "clean" else ())
            for key in keys:
                self.assertIn("Codex", text["en"][key], (look, key))
                self.assertIn("Codex", text["fa"][key], (look, key))
                self.assertNotEqual(text["en"][key], text["fa"][key], (look, key))

    def test_the_change_dialog_sets_codex_limits_from_a_codex_tile_and_claudes_from_claudes(self):
        state = STATE | {"codex": CODEX}
        for look in PAGES:
            for lang in ("en", "fa"):
                seen = js(look, lang, fns(look, "changeLimits"), DIALOG + "console.log(JSON.stringify([open(%s, true), open(%s, false)]));"
                          % (json.dumps(state), json.dumps(state)))
                codex, claude = seen
                text = words(PAGES[look])[lang]
                self.assertEqual((codex["title"], codex["session"], codex["week"]), (text["codex_limits_title"], 50, 60), look)
                self.assertEqual(codex["posted"], "/limits?session=55&week=60&cli=codex", look)
                self.assertEqual((claude["title"], claude["session"], claude["week"]), (text["limits_title"], 70, 60), look)
                self.assertEqual(claude["posted"], "/limits?session=55&week=60", look)  # Claude's, as before


@unittest.skipUnless(NODE, "needs node to run the page's script")
class ClassicTilesTest(unittest.TestCase):
    """Classic and Kandu Clean draw a tile for each usage; Codex's two follow Claude's two."""
    def tiles(self, look, lang, state):
        return js(look, lang, fns(look, "meter", "codexMeters"), f"console.log(JSON.stringify(codexMeters({json.dumps(state)})));")

    def test_no_codex_tile_while_no_project_uses_codex(self):
        for look in ("classic", "clean"):
            self.assertEqual(self.tiles(look, "en", STATE), "", look)
            self.assertIn("+ codexMeters(s) +", function(PAGES[look], "renderStats"), look)

    def test_codex_tiles_show_its_usage_against_its_limits_in_each_language(self):
        for look in ("classic", "clean"):
            for lang in ("en", "fa"):
                html = self.tiles(look, lang, STATE | {"codex": CODEX})
                text, (used, week, limit) = words(PAGES[look])[lang], DIGITS[lang]
                self.assertIn(text["codex_session"], html)
                self.assertIn(text["codex_week"], html)
                self.assertIn(f">{used}<", html)
                self.assertIn(f'<div class="stat-value over">{week}</div>', html)  # 61% is past its 60% limit
                self.assertIn(f"{text['limit']} {limit}", html)
                self.assertEqual(html.count('data-limits="codex"'), 2)

    def test_codex_not_read_yet_shows_a_dash(self):
        html = self.tiles("classic", "en", STATE | {"codex": {"usage": None, "limits": CODEX["limits"]}})
        self.assertEqual(html.count('<div class="stat-value muted">—</div>'), 2)

    def test_claudes_tiles_are_as_before(self):
        for look in ("classic", "clean"):
            html = js(look, "en", fns(look, "meter"), 'console.log(JSON.stringify(meter("5-hour", 30, 70)));')
            self.assertIn('<button class="textlink limit-edit" data-limits>', html)


@unittest.skipUnless(NODE, "needs node to run the page's script")
class ClassicNewStripTest(unittest.TestCase):
    """Classic new draws its numbers as chips; Codex's two gauges follow Claude's in the lead."""
    def strip(self, lang, state):
        look = "classic-new"
        stubs = ("const hiveChip = () => '<hive>';\nconst lastQueue = null;\nconst wakeText = () => '—';\n"
                 "const nodes = {};\nconst $ = id => nodes[id] ||= {};\n")
        return js(look, lang, [stubs] + fns(look, "schip", "renderStats"),
                  f"renderStats({json.dumps(state | {'workers': []})});\nconsole.log(JSON.stringify(nodes.stats.innerHTML));")

    def lead(self, html):
        return html.split('<div class="gauges">')[1].split("</div>")[0]

    def test_no_codex_chip_while_no_project_uses_codex(self):
        html = self.strip("en", STATE)
        self.assertNotIn("Codex", html)
        self.assertEqual(self.lead(html).count('<button class="schip'), 2)

    def test_codex_chips_follow_claudes_in_the_lead_in_each_language(self):
        for lang in ("en", "fa"):
            text, (used, week, limit) = words(PAGES["classic-new"])[lang], DIGITS[lang]
            lead = self.lead(self.strip(lang, STATE | {"codex": CODEX}))
            chips = lead.split("</button>")
            self.assertEqual(len(chips) - 1, 4, lang)
            self.assertIn(text["s_session"], chips[0])
            self.assertIn(text["s_codex_session"], chips[2])
            self.assertIn(f"<b>{used}</b>", chips[2])
            self.assertIn(f'<bdi class="of">{limit}</bdi>', chips[2])
            self.assertIn('class="schip over" data-limits="codex"', chips[3])  # 61% is past its 60% limit
            self.assertIn(text["codex_week"], chips[3])


@unittest.skipUnless(NODE, "needs node to run the page's script")
class KanduCleanTest(unittest.TestCase):
    """Kandu Clean shows Codex in the header's gauges, the phone's health line and the Settings' limits."""
    def run_clean(self, lang, script, *names):
        stubs = "const stateChip = () => ({kind: 'asleep', long: 'Asleep'});\nconst fillIn = key => key;\nconst when = x => x;\n"
        return js("clean", lang, [stubs, line(PAGES["clean"], "const LIMIT_STEP = ")] + fns("clean", "gauge", *names), script)

    def test_the_header_gauges_show_codex_after_claude_only_when_a_project_uses_it(self):
        for lang in ("en", "fa"):
            text = words(PAGES["clean"])[lang]
            seen = self.run_clean(lang, f"console.log(JSON.stringify([gauges({json.dumps(STATE)}), gauges({json.dumps(STATE | {'codex': CODEX})})]));", "gauges")
            self.assertNotIn("Codex", seen[0])
            self.assertEqual(seen[0].count('class="gauge'), 2)
            self.assertEqual(seen[1].count('class="gauge'), 4)
            self.assertLess(seen[1].index(text["week"]), seen[1].index(text["codex_session"]))
            self.assertIn(f'class="gauge over" title="{text["codex_week"]}', seen[1])

    def test_the_phone_health_line_has_codex_bars_only_when_a_project_uses_it(self):
        seen = self.run_clean("en", f"console.log(JSON.stringify([healthItems({json.dumps(STATE)}), healthItems({json.dumps(STATE | {'codex': CODEX})})]));", "healthItems")
        bars = [[i["name"] for i in items if i["kind"] == "bar"] for items in seen]
        self.assertEqual(bars, [["5-hour", "Week"], ["5-hour", "Week", "Codex 5-hour", "Codex week"]])
        self.assertEqual([i["value"] for i in seen[1] if i["kind"] == "bar"][2:], [12, 61])

    def test_settings_set_codex_limits_beside_claudes_only_when_a_project_uses_it(self):
        for lang in ("en", "fa"):
            text = words(PAGES["clean"])[lang]
            seen = self.run_clean(lang, f"console.log(JSON.stringify([limitsHtml({json.dumps(STATE)}), limitsHtml({json.dumps(STATE | {'codex': CODEX})})]));",
                                  "stepper", "limitsHtml")
            self.assertNotIn("Codex", seen[0])
            self.assertTrue(seen[1].startswith(seen[0]))  # Claude's as before, then Codex's
            self.assertIn(text["codex_limits_title"], seen[1])
            self.assertIn(text["codex_limit_session"], seen[1])
            self.assertIn('data-step-path="/limits?session={n}&amp;week=60&amp;cli=codex" data-step-from="50"', seen[1])
            self.assertIn('data-step-path="/limits?session=50&amp;week={n}&amp;cli=codex" data-step-from="60"', seen[1])

    def test_a_change_of_codex_limits_redraws_the_settings(self):
        self.assertIn("s.codex?.limits", function(PAGES["clean"], "renderSettings"))


if __name__ == "__main__":
    unittest.main()
