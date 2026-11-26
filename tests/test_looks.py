"""The dashboard's looks, Kandu Clean, Classic and Classic new, and the switch between them (#53)."""
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from kandu import dashboard
from kandu.dashboard import dispatch, make_handler
from kandu.github import BoardItem
from tests.test_dashboard import Base, REPO, TOKEN

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
TEXT = re.compile(r'<script type="application/json" id="text">(.*?)</script>', re.DOTALL)
# What the Classic look named, or drew, only for the honeycomb look.
HONEYCOMB = ("honeycomb", "hexCell", "HONEY_", "pcell", "shelf", 'id="honey"', "honey-stripes", "tface", "statCells",
             "usageCell", "cellPlaces", "look_cards", "look_switch", "hive_with_tasks", "legend_cell_sizes", "legend_edge_signs")


def read(page):
    return page.read_text(encoding="utf-8")


def words(page):
    return json.loads(TEXT.search(page).group(1))


def function(page, name):
    begin = page.index(f"function {name}(")
    if page[begin - 6:begin] == "async ":
        begin -= 6
    return page[begin:page.index("\n}\n", begin) + 2]


def line(page, start):
    return next(each for each in page.splitlines() if each.startswith(start)) + "\n"


def run_js(script):
    done = subprocess.run([NODE, "-"], input=script, capture_output=True, text=True, timeout=30)
    if done.returncode:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout)


def browser():
    """A Chromium to open the pages in: KANDU_TEST_BROWSER, one on the PATH, or the one Playwright keeps."""
    if os.environ.get("KANDU_TEST_BROWSER"):
        return os.environ["KANDU_TEST_BROWSER"]
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome-headless-shell"):
        if shutil.which(name):
            return shutil.which(name)
    kept = sorted(Path.home().glob(".cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell"))
    return str(kept[-1]) if kept else None


@contextlib.contextmanager
def profile_folder():
    """An empty folder for a headless browser's profile (#117). A driver kills its Chromium and exits at once, so the
    browser may still be writing here when the test goes on. On the way out this waits, for a few seconds at most, until no
    process has the folder as its `--user-data-dir`, then removes it, trying again briefly while it is still being written.
    A folder that cannot be removed even then stays for the system to clean, and never fails the test."""
    folder = tempfile.mkdtemp(prefix="kandu-browser-")
    try:
        yield folder
    finally:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _browser_using(folder):
            time.sleep(0.05)
        deadline = time.monotonic() + 5
        while True:
            try:
                shutil.rmtree(folder)
                break
            except FileNotFoundError:
                break
            except OSError:
                if time.monotonic() >= deadline:
                    shutil.rmtree(folder, ignore_errors=True)
                    break
                time.sleep(0.1)


def _browser_using(folder):
    """Whether a running process has `folder` as its profile, as far as /proc shows; with no /proc, no. Chromium rewrites
    its command line into one string with the flags joined by spaces, so the flag is looked for anywhere in it."""
    flag = f"--user-data-dir={folder}".encode()
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            if flag in cmdline.read_bytes():
                return True
        except OSError:
            continue
    return False


class Served(Base):
    """The dashboard over real HTTP, with tasks in several states on the board and a bee at work."""
    def setUp(self):
        super().setUp()
        items = [BoardItem(REPO, n, f"Task {n} with a fairly long title so it wraps on a phone", "u", column, labels, "")
                 for n, column, labels in ((1, "Ready", ["agent"]), (2, "In progress", ["agent"]), (3, "Blocked", ["agent"]),
                                           (4, "Needs info", ["agent"]), (5, "Backlog", []), (6, "Your approval", ["agent"]))]
        self.github.board_items.return_value = items
        self.github.issue_text.return_value = "## Goal\nA goal.\n"
        self.store.write_current({"supervisor": "awake", "usage": {"session": 30, "week": 65}, "next_wake": None,
                                  "workers": [{"task": f"{REPO}#2", "phase": "implement", "machine": "laptop", "pid": 1,
                                               "run_id": "r-1", "started": "2026-11-16T10:00:00+00:00"}]})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.api))
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def open(self, cookie=None):
        request = urllib.request.Request(self.base + "/", headers={"Cookie": cookie} if cookie else {})
        with urllib.request.urlopen(request) as r:
            return r.headers, r.read()


class ServedLookTest(Served):
    def test_a_browser_that_chose_nothing_opens_in_the_classic_look(self):
        headers, body = self.open()
        self.assertEqual(body, dashboard.CLASSIC_PAGE.read_bytes())
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_each_browser_opens_in_the_look_it_chose(self):
        self.assertEqual(self.open("kandu-look=clean")[1], dashboard.PAGE.read_bytes())
        self.assertEqual(self.open("kandu-language=fa; kandu-look=clean")[1], dashboard.PAGE.read_bytes())
        self.assertEqual(self.open("kandu-look=classic")[1], dashboard.CLASSIC_PAGE.read_bytes())

    def test_a_malformed_cookie_from_another_app_does_not_hide_the_chosen_look(self):
        # Browsers share the cookies of 127.0.0.1 across ports, so other local apps' cookies come along.
        for other in ('pref={"a":1,"b":2}', "x=a b", "x=café", "x=a\\b", "Path"):
            self.assertEqual(self.open(f"{other}; kandu-look=clean")[1], dashboard.PAGE.read_bytes(), other)

    def test_a_look_that_is_gone_or_unknown_opens_in_the_classic_look(self):
        for cookie in ("kandu-look=honeycomb", "kandu-look=cards", "kandu-look=", "kandu-look", "=;;", "other=clean"):
            self.assertEqual(self.open(cookie)[1], dashboard.CLASSIC_PAGE.read_bytes(), cookie)

    def set_default_look(self, look):
        status, body = dispatch(self.api, "POST", f"/api/v1/default-look?look={look}", TOKEN)
        self.assertEqual((status, body), (200, {"default_look": look}))

    def test_a_browser_that_chose_nothing_opens_in_the_default_look(self):
        self.set_default_look("clean")
        self.assertEqual(self.open()[1], dashboard.PAGE.read_bytes())
        self.assertEqual(self.open("kandu-language=fa")[1], dashboard.PAGE.read_bytes())
        self.set_default_look("classic")
        self.assertEqual(self.open()[1], dashboard.CLASSIC_PAGE.read_bytes())

    def test_a_browsers_own_choice_always_wins_over_the_default_look(self):
        self.set_default_look("clean")
        self.assertEqual(self.open("kandu-look=classic")[1], dashboard.CLASSIC_PAGE.read_bytes())
        self.set_default_look("classic")
        self.assertEqual(self.open("kandu-look=clean")[1], dashboard.PAGE.read_bytes())

    def test_a_look_that_is_gone_or_unknown_opens_in_the_default_look(self):
        self.set_default_look("clean")
        for cookie in ("kandu-look=honeycomb", "kandu-look=", "kandu-look", "other=classic"):
            self.assertEqual(self.open(cookie)[1], dashboard.PAGE.read_bytes(), cookie)

    def test_a_default_look_changed_in_the_config_by_hand_is_used_without_a_restart(self):
        path = self.root / "local" / "kandu.yaml"
        text = path.read_text(encoding="utf-8")
        path.write_text(re.sub(r"(?m)^  look: \w+", "  look: clean", text), encoding="utf-8")
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 10**9))  # a new stamp, however fast
        self.assertEqual(self.open()[1], dashboard.PAGE.read_bytes())

    def test_both_looks_use_only_the_public_api_and_load_nothing_from_outside(self):
        for page in (read(dashboard.PAGE), read(dashboard.CLASSIC_PAGE)):
            self.assertFalse("state/" in page)
            self.assertTrue('fetch("/api/v1"' in page)
            self.assertEqual(re.findall(r'(?:src|href)="https?://', page), [])


class ClassicPageTest(unittest.TestCase):
    """The Classic look: the dashboard as it was just before the Kandu Clean rebuild, without the honeycomb look."""
    def setUp(self):
        self.page = read(dashboard.CLASSIC_PAGE)
        self.text = words(self.page)
        self.style = self.page.split("<style>")[1].split("</style>")[0]
        self.header = self.page.split('<header class="top">')[1].split("</header>")[0]

    def test_nothing_of_the_honeycomb_look_is_in_it(self):
        for gone in HONEYCOMB:
            self.assertFalse(gone.lower() in self.page.lower(), gone)
        self.assertFalse("dataset.look" in self.page)

    def test_it_is_marked_as_the_classic_look(self):
        self.assertTrue('<html lang="en" data-look="classic">' in self.page)

    def test_a_look_button_beside_the_language_switch_opens_the_look_menu(self):
        self.assertRegex(self.header, r'<button id="look" class="btn plain" aria-haspopup="dialog"><span data-t="look"></span> ▾</button>\s*<button id="lang"')
        self.assertTrue('$("look").onclick = openLooks;' in self.page)
        self.assertIn('$("look").title = t("look_hint");', function(self.page, "applyLanguage"))
        self.assertTrue('const LOOK_HERE = "classic";' in self.page)
        for gone in ("look_clean", "look_clean_hint"):
            self.assertNotIn(gone, self.text["en"], gone)

    def test_wake_the_queen_carries_a_honey_crown_that_stays_while_it_works(self):
        self.assertRegex(self.header, r'<button id="wake" class="btn plain"><svg class="crown" viewBox="0 0 24 24" aria-hidden="true"><path d="[^"]+"/></svg>'
                                      r'<span data-t="wake_queen"></span></button>')
        self.assertRegex(self.style, r"\.crown \{[^}]*fill:var\(--honey\)")
        busy = function(self.page, "busy")
        self.assertIn("const label = button.innerHTML", busy)
        self.assertIn("button.innerHTML = label;", busy)

    @unittest.skipUnless(NODE, "needs node to run the page's script")
    def test_choosing_kandu_clean_keeps_the_choice_in_the_browser_and_opens_it(self):
        done = run_js("const document = {cookie: \"\"}; const calls = []; const location = {reload: () => calls.push(document.cookie)};\n"
                      + line(self.page, "const LOOK_COOKIE = ") + function(self.page, "chooseLook")
                      + 'chooseLook("clean"); console.log(JSON.stringify(calls));')
        self.assertEqual(done, ["kandu-look=clean; path=/; max-age=34560000; samesite=strict"])

    def test_its_tabs_are_the_ones_it_had(self):
        tabs = self.page.split('<nav class="tabs" role="tablist">')[1].split("</nav>")[0]
        found = re.findall(r'data-tab="(\w+)" data-t="(\w+)"', tabs)
        self.assertEqual([name for name, _ in found], ["meadow", "work", "bazras", "settings", "days", "activity"])
        for name, key in found:
            self.assertTrue(f'<section data-panel="{name}"' in self.page)
            self.assertIn(key, self.text["en"])
        self.assertTrue('tab = "meadow"' in self.page)

    def test_a_tab_saved_in_the_kandu_clean_look_opens_the_same_page(self):
        self.assertTrue('const CLEAN_TABS = {today: "meadow", tasks: "work"};' in self.page)
        self.assertIn("chosen = CLEAN_TABS[chosen] || chosen;", function(self.page, "showTab"))

    def test_it_shows_every_warning(self):
        for part in ('<div id="machines" class="list" hidden></div>', '<div id="asks" class="list" hidden></div>',
                     "const refused = githubRefused(s);", "${limitBadge(overLimit(lastState, project.name))}",
                     '<ellipse class="soil${patch.paused ? " off" : ""}${patch.over ? " over" : ""}"', "statusText(v.status)",
                     "waitLines(v.status.wait)", "heldLines("):
            self.assertTrue(part in self.page, part)

    def test_every_task_state_has_its_flower_and_its_badge(self):
        flowers = re.search(r"const FLOWERS = \{(.*?)\n\};", self.page, re.DOTALL).group(1)
        for column in dashboard.QUEUE_COLUMNS:
            self.assertIn(f'"{column}":', flowers)
        self.assertIn('"Stuck":', flowers)
        self.assertTrue('item.yours ? `<span class="badge" style="--c:#e8590c">${esc(t("by_hand_yours"))}</span>`' in self.page)

    def test_every_action_it_had_is_there(self):
        for hit in ("approve", "split", "back", "to-queen", "stop", "pause", "drop", "fly", "land", "remove", "inspect",
                    "limits", "parallel", "backup", "upstream-check", "upstream-bring", "requests", "drops", "step", "review"):
            self.assertRegex(self.page, rf'hit\("{hit}"\)', hit)
        for button in ('id="stop"', 'id="resume"', 'id="reload"', 'id="wake"', 'id="restart"', 'id="didbanbox"',
                       'id="zanbourdarbox"', 'id="lang"', 'id="addopen"'):
            self.assertTrue(button in self.page, button)

    def test_its_settings_tab_changes_each_setting_at_once(self):
        render = function(self.page, "renderSettings")
        for control in ("data-setting-pause", "data-setting-tokens", "data-setting-auto-merge", "data-setting-frozen", "data-setting-multi-user",
                        "data-setting-splits", "data-setting-one-way", "data-remove", "?priority={n}", "/parallel?project=",
                        "/pace?wake_every_minutes={n}", "/pace?max_cycle_minutes={n}"):
            self.assertIn(control, render, control)
        self.assertTrue('if (hit("step")) { changeStep(hit("step")); return; }' in self.page)
        self.assertNotIn("Save", render)

    @unittest.skipUnless(NODE, "needs node to run the page's script")
    def test_its_settings_tab_shows_the_default_look_and_changes_it_with_the_token(self):
        self.assertTrue('<div id="defaultlook" class="card pad"></div>' in self.page.split('<section data-panel="settings"')[1].split("</section>")[0])
        prelude = (f"const TEXT = {json.dumps(self.text)};\nlet lang = \"en\";\nconst shown = {{}}; const $ = id => (shown[id] ??= {{}});\n"
                   + "".join(line(self.page, start) for start in ("const esc = ", "const t = "))
                   + function(self.page, "defaultLookHtml") + function(self.page, "changeDefaultLook"))
        seen = run_js(prelude + """const calls = []; const act = async (el, path) => { calls.push(path); return {}; };
const refreshState = async () => {}; const renderSettings = {};
(async () => {
  const classic = defaultLookHtml("classic"), clean = defaultLookHtml("clean");
  await changeDefaultLook({value: "clean"});
  console.log(JSON.stringify({classic, clean, calls}));
})();""")
        self.assertIn(self.text["en"]["default_look"], seen["classic"])
        self.assertIn('<select data-default-look', seen["classic"])
        self.assertIn('<option value="classic" selected>Classic</option><option value="classic-new">Classic new</option><option value="clean">Kandu Clean</option>', seen["classic"])
        self.assertIn('<option value="classic">Classic</option><option value="classic-new">Classic new</option><option value="clean" selected>Kandu Clean</option>', seen["clean"])
        self.assertEqual(seen["calls"], ["/default-look?look=clean"])  # act sends the dashboard's token
        self.assertIn("defaultLookHtml(s.default_look)", function(self.page, "renderSettings"))
        self.assertIn("s.default_look", function(self.page, "renderSettings").split("const key = ")[1].split("\n")[0])
        self.assertTrue('if (e.target.matches?.("[data-default-look]")) changeDefaultLook(e.target);' in self.page)
        for key in ("default_look", "default_look_hint", "default_look_classic", "default_look_clean"):
            self.assertNotEqual(self.text["fa"][key], self.text["en"][key], key)

    def test_every_call_it_makes_is_in_todays_api(self):
        script = self.page.split("<script>")[1]
        quoted = r'(`[^`]*`|"[^"]*"(?: \+)?)'
        found = re.findall(rf"\b(?:get|post)\({quoted}|\bact\((?:[^()]|\([^()]*\))*?, {quoted}", script)
        paths = set()
        for literal in (a or b for a, b in found):  # "\0" stands for what the page fills in
            if literal[0] == "`":
                path = re.sub(r"\$\{[^}]*\}", "\0", literal[1:-1])
            else:
                path = literal.split('"')[1] + ("\0" if literal.endswith("+") else "")
            paths.add(path.split("?")[0])
        self.assertGreaterEqual(len(paths), 30)
        routes = [re.sub(r"\{\w+\}", "x", path) for _, path, _ in dashboard.ROUTES]
        for path in paths:  # what the page fills in may hold a slash, as `owner/repo` does, or be empty
            called = re.compile("^" + ".*".join(re.escape(part) for part in path.split("\0")) + "$")
            self.assertTrue(any(called.match(route) for route in routes), path)

    def test_both_languages_have_every_word_it_uses(self):
        self.assertEqual(set(self.text["en"]), set(self.text["fa"]))
        used = set(re.findall(r'data-t="(\w+)"', self.page)) | set(re.findall(r'\bt\("(\w+)"\)', self.page))
        self.assertEqual(used - set(self.text["en"]), set())
        self.assertTrue('document.documentElement.dir = lang === "fa" ? "rtl" : "ltr";' in self.page)

    def test_nothing_pushes_a_phone_page_sideways(self):
        self.assertRegex(self.style, r"\.bar \{[^}]*flex-wrap:wrap")
        self.assertRegex(self.style, r"\.tabs \{[^}]*overflow-x:auto")
        self.assertRegex(self.style, r"#projectsettings \{[^}]*overflow-x:auto")
        self.assertIn(".stats { grid-template-columns:repeat(2,minmax(0,1fr)); }", self.style)


class CleanSwitchTest(unittest.TestCase):
    """From the Kandu Clean look, More switches to the Classic look, on a wide screen and on a phone."""
    def setUp(self):
        self.page = read(dashboard.PAGE)
        self.text = words(self.page)

    def prelude(self):
        return (f"const TEXT = {json.dumps(self.text)};\nlet lang = \"en\";\n"
                + "".join(line(self.page, start) for start in ("const esc = ", "const t = ", "const LOOK_COOKIE = "))
                + "".join(function(self.page, name) for name in ("moreItems", "chooseLook", "chooseMore")))

    def test_it_is_marked_as_the_kandu_clean_look(self):
        self.assertTrue('<html lang="en" data-look="clean">' in self.page)

    @unittest.skipUnless(NODE, "needs node to run the page's script")
    def test_more_offers_the_look_menu_on_a_wide_screen_and_on_a_phone(self):
        items = run_js(self.prelude() + "console.log(JSON.stringify([moreItems([], true), moreItems([], false)]));")
        for menu in items:
            look = [i for i in menu if i["label"] == "Look"]
            self.assertEqual(len(look), 1)
            self.assertEqual(look[0]["attrs"], 'data-menu="look"')
            self.assertEqual(look[0]["hint"], "Kandu Clean")

    def test_a_look_button_beside_the_language_switch_on_a_wide_screen_opens_the_look_menu(self):
        header = self.page.split('<header class="top">')[1].split("</header>")[0]
        self.assertRegex(header, r'<button id="look" class="btn plain head-wide" aria-haspopup="dialog"><span data-t="look"></span> ▾</button>\s*<button id="lang"')
        self.assertTrue('$("look").onclick = openLooks;' in self.page)
        self.assertTrue('const LOOK_HERE = "clean";' in self.page)

    @unittest.skipUnless(NODE, "needs node to run the page's script")
    def test_choosing_it_keeps_the_choice_in_the_browser_and_opens_it(self):
        done = run_js(self.prelude().replace("const LOOK_COOKIE", 'const calls = [];\nconst openLooks = () => calls.push("open the look menu");\n'
                                             "const $ = id => ({close: () => calls.push(`close ${id}`)});\nconst LOOK_COOKIE", 1)
                      + '(async () => { await chooseMore({dataset: {menu: "look"}}); console.log(JSON.stringify(calls)); })();')
        self.assertEqual(done, ["close moremenu", "open the look menu"])

    def test_its_words_read_in_both_languages(self):
        for key in ("look", "look_hint"):
            self.assertEqual(self.page.count(f'"{key}": '), 2, key)
        for gone in ("look_classic", "hint_look"):
            self.assertNotIn(gone, self.text["en"], gone)

    def test_it_no_longer_forgets_the_look_a_browser_chose(self):
        self.assertFalse('forget("kandu-look")' in self.page)


class LookMenuTest(unittest.TestCase):
    """The Look menu, the same in every look: every look the dashboard has, the one this page is marked."""
    PAGES = {"classic": dashboard.CLASSIC_PAGE, "classic-new": dashboard.CLASSIC_NEW_PAGE, "clean": dashboard.PAGE}

    def test_every_look_has_the_same_menu(self):
        classic, clean = read(dashboard.CLASSIC_PAGE), read(dashboard.PAGE)
        for other in (clean, read(dashboard.CLASSIC_NEW_PAGE)):
            for name in ("lookItems", "openLooks", "pickLook", "chooseLook"):
                self.assertEqual(function(classic, name), function(other, name), name)
            self.assertEqual(line(classic, "const LOOK_NAMES = "), line(other, "const LOOK_NAMES = "))
        for page in (read(path) for path in self.PAGES.values()):
            self.assertIn('<dialog id="lookmenu" class="menu" data-t-attr="aria-label" data-t="look"><div class="menu-head" data-t="look"></div>'
                          '<div id="lookitems"></div><p class="menu-hint" data-t="look_hint"></p></dialog>', page)
            self.assertIn('if (hit("choose-look")) { pickLook(hit("choose-look")); return; }', page)
            self.assertIn('if (e.target === $("lookmenu")) { $("lookmenu").close(); return; }', page)
            for lang in ("en", "fa"):
                for key in ("look", "look_hint", "default_look_classic", "default_look_classic-new", "default_look_clean"):
                    self.assertIn(key, words(page)[lang], f"{lang}: {key}")
            self.assertNotEqual(words(page)["fa"]["look"], words(page)["en"]["look"])

    def test_the_state_names_every_look_in_the_menus_order(self):
        self.assertEqual(list(dashboard.LOOKS), ["classic", "classic-new", "clean"])
        self.assertIn('"looks": list(LOOKS),', (ROOT / "kandu" / "dashboard.py").read_text(encoding="utf-8"))
        api = read(ROOT / "docs" / "API.md")
        self.assertIn("`looks`", next(each for each in api.splitlines() if each.startswith("| GET | `/api/v1/state`")))

    @unittest.skipUnless(NODE, "needs node to run the page's script")
    def test_it_lists_the_looks_the_state_names_and_marks_this_one(self):
        for here, path in self.PAGES.items():
            page = read(path)
            seen = run_js(f"const TEXT = {json.dumps(words(page))}; let lang = \"fa\";\n" + line(page, "const esc = ")
                          + line(page, "const LOOK_NAMES = ") + function(page, "lookItems")
                          + 'console.log(JSON.stringify([lookItems(["classic", "clean", "sunny"], "clean"), lookItems(undefined, "classic")]));')
            three, fallback = seen
            self.assertEqual(re.findall(r'data-choose-look="([\w-]+)" aria-current="(\w+)"', three),
                             [("classic", "false"), ("clean", "true"), ("sunny", "false")], here)
            self.assertIn("<span>sunny</span>", three)  # a look this page has no name for shows as it is called
            self.assertIn(f"<span>{words(page)['fa']['default_look_clean']}</span>", three)
            self.assertEqual(three.count("✓"), 1)
            self.assertEqual(re.findall(r'data-choose-look="([\w-]+)"', fallback), ["classic", "classic-new", "clean"], here)

    @unittest.skipUnless(NODE, "needs node to run the page's script")
    def test_choosing_another_look_keeps_it_in_the_browser_and_this_one_only_closes(self):
        for here, path in self.PAGES.items():
            page = read(path)
            other = "clean" if here == "classic" else "classic"
            done = run_js("const document = {cookie: \"\"}; const calls = []; const location = {reload: () => calls.push(document.cookie)};\n"
                          "const $ = id => ({close: () => calls.push(`close ${id}`)});\n"
                          + line(page, "const LOOK_COOKIE = ") + line(page, "const LOOK_HERE = ") + function(page, "chooseLook") + function(page, "pickLook")
                          + f'pickLook({{dataset: {{chooseLook: "{here}"}}}}); pickLook({{dataset: {{chooseLook: "{other}"}}}}); console.log(JSON.stringify(calls));')
            self.assertEqual(done, ["close lookmenu", "close lookmenu", f"kandu-look={other}; path=/; max-age=34560000; samesite=strict"], here)


class DesignTest(unittest.TestCase):
    def test_design_describes_the_looks_the_switch_and_where_new_things_go(self):
        section = read(ROOT / "DESIGN.md").split("## 7. Dashboard")[1].split("\n## 8.")[0]
        looks = next(each for each in section.splitlines() if each.startswith("- **Looks:**"))
        for part in ("Kandu Clean", "Classic", "Classic new", "More", "language switch", "each browser remembers", "chosen nothing",
                     "honeycomb", "every look", "only into the Kandu Clean look", "warning", "task state", "action"):
            self.assertIn(part.lower(), looks.lower(), part)

    def test_design_and_the_api_docs_describe_the_default_look(self):
        design = read(ROOT / "DESIGN.md")
        looks = next(each for each in design.split("## 7. Dashboard")[1].splitlines() if each.startswith("- **Looks:**"))
        for part in ("default look", "`dashboard.look`", "Classic by default", "own choice always wins", "Settings tab of every look"):
            self.assertIn(part, looks, part)
        setup = design.split("### A. Initial setup")[1].split("### B.")[0]
        self.assertIn("`dashboard.look`", setup)
        api = read(ROOT / "docs" / "API.md")
        self.assertIn("`default_look`", next(each for each in api.splitlines() if each.startswith("| GET | `/api/v1/state`")))
        self.assertIn("`look=classic`, `classic-new` or `clean`", next(each for each in api.splitlines() if each.startswith("| POST | `/api/v1/default-look`")))

    def test_setup_and_config_ask_for_the_default_look(self):
        commands = ROOT / ".claude" / "commands"
        setup, config = read(commands / "kandu-setup.md"), read(commands / "kandu-config.md")
        question = next(each for each in setup.splitlines() if "`dashboard.look`" in each)
        self.assertRegex(question, r"^   \d+\. ")
        for part in ("Classic", "Classic new", "Kandu Clean", "default"):
            self.assertIn(part, question, part)
        self.assertIn("`dashboard.look`", config)
        self.assertIn("Classic, Classic new or Kandu Clean", config)


class ProfileFolderTest(unittest.TestCase):
    """The browser's profile folder (#117): the drivers kill Chromium and exit at once, so it may still write there."""
    # Stands in for the browser. Its command line is one string, flags joined by spaces, as Chromium rewrites its own.
    # For half a second it keeps writing into the profile, then goes quiet, then writes once more on its way out, making
    # the folder again if it is gone, as Chromium does when it is killed.
    WRITER = ("import pathlib, sys, time\n"
              "folder = pathlib.Path(sys.argv[1].split('--user-data-dir=')[1].split()[0]) / 'Default'\n"
              "end, n = time.monotonic() + 0.5, 0\n"
              "while time.monotonic() < end:\n"
              "    try:\n"
              "        folder.mkdir(exist_ok=True)\n"
              "        (folder / f'f{n}').write_text('x')\n"
              "        n += 1\n"
              "    except OSError:\n"
              "        pass\n"
              "time.sleep(1)\n"
              "folder.mkdir(parents=True, exist_ok=True)\n"
              "(folder / 'last').write_text('x')\n")

    def test_a_browser_still_writing_when_the_test_goes_on_neither_errors_nor_leaves_the_folder(self):
        with profile_folder() as profile:
            writer = subprocess.Popen([sys.executable, "-c", self.WRITER, f"--headless --user-data-dir={profile} about:blank"])
            self.addCleanup(writer.kill)
            while not (Path(profile) / "Default" / "f100").exists():
                time.sleep(0.01)
        writer.wait(timeout=10)
        self.assertFalse(Path(profile).exists())


@unittest.skipUnless(NODE and browser(), "needs node and a Chromium to open the pages")
class BrowserTest(Served):
    """Both looks, opened in a real browser: switched from each to the other, in English and Persian, wide and on a phone."""
    CASES = [{"lang": "en", "width": 1280, "height": 900, "mobile": False}, {"lang": "fa", "width": 1280, "height": 900, "mobile": False},
             {"lang": "en", "width": 390, "height": 844, "mobile": True}, {"lang": "fa", "width": 390, "height": 844, "mobile": True}]

    def test_each_look_switches_to_the_other_and_keeps_the_choice_in_both_languages_wide_and_on_a_phone(self):
        with tempfile.TemporaryDirectory() as profile:
            done = subprocess.run([NODE, str(ROOT / "tests" / "drive_looks.js"), browser(), self.base, profile, json.dumps(self.CASES)],
                                  capture_output=True, text=True, timeout=180)
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        report = json.loads(done.stdout)
        self.assertEqual(report["errors"], [])
        for case in report["results"]:
            name = f'{case["lang"]} at {case["width"]}px'
            steps = case["steps"]
            menus = {step: steps.pop(step) for step in ("menu", "clean_menu")}
            for step, current in (("menu", "classic"), ("clean_menu", "clean")):
                self.assertEqual(menus[step], {"open": True, "looks": ["classic", "classic-new", "clean"], "current": [current], "sideways": False}, f"{name}, {step}")
            looks = {step: seen["look"] for step, seen in steps.items() if step != "more_open"}
            self.assertEqual(looks, {"first": "classic", "switched": "clean", "reloaded": "clean", "back": "classic",
                                     "back_reloaded": "classic"}, name)
            self.assertTrue(steps["more_open"], name)
            for step, seen in steps.items():
                if step == "more_open":
                    continue
                self.assertEqual((seen["lang"], seen["dir"]), (case["lang"], "rtl" if case["lang"] == "fa" else "ltr"), f"{name}, {step}")
                self.assertEqual(seen["sideways"], [], f"{name}, {step}: these pages scroll sideways")
            self.assertEqual(steps["first"]["pages"], ["work", "meadow", "bazras", "settings", "days", "activity"], name)


if __name__ == "__main__":
    unittest.main()
