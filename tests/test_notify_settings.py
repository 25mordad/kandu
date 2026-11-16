"""Which kinds of news Kandu sends, chosen from the dashboard: `notify.events` in kandu.yaml (Telegram settings)."""
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from kandu import dashboard
from kandu.config import NOTIFY_EVENTS, ConfigError, load_config, set_notify_events
from kandu.dashboard import dispatch
from tests.test_dashboard import Base, TOKEN
from tests.test_looks import NODE, function, line, read, run_js, words

EXAMPLES = Path(__file__).resolve().parent.parent / "config.example"
PAGES = {"classic": dashboard.CLASSIC_PAGE, "classic-new": dashboard.CLASSIC_NEW_PAGE, "clean": dashboard.PAGE}


def some_words(page):
    """Only the words these tests need: the whole text is too long for node's command line."""
    return {lang: {k: v for k, v in text.items() if k.startswith(("notify_", "tg_")) or k in ("close", "save", "cancel")}
            for lang, text in words(page).items() if isinstance(text, dict)}


class SetNotifyEventsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        shutil.copy(EXAMPLES / "kandu.example.yaml", self.local / "kandu.yaml")
        shutil.copy(EXAMPLES / "projects.example.yaml", self.local / "projects.yaml")
        self.path = self.local / "kandu.yaml"

    def notify(self):
        return yaml.safe_load(self.path.read_text(encoding="utf-8"))["notify"]

    def test_it_writes_the_list_in_the_usual_order_and_keeps_the_rest_of_the_file(self):
        before = self.path.read_text(encoding="utf-8")
        self.assertEqual(set_notify_events(["task_done", "failure", "task_done"], self.local), ["failure", "task_done"])
        self.assertEqual(self.notify()["events"], ["failure", "task_done"])
        self.assertEqual(self.notify()["telegram"]["bot_token_env"], "KANDU_TELEGRAM_BOT_TOKEN")
        after = self.path.read_text(encoding="utf-8")
        outside = lambda text: re.sub(r"\n  events:.*?(?=\n\S|\n\n)", "", text, flags=re.S)
        self.assertEqual(outside(after), outside(before))
        self.assertEqual(load_config(self.local).settings.notify.events, ["failure", "task_done"])

    def test_none_at_all_and_back_to_all(self):
        set_notify_events([], self.local)
        self.assertEqual(self.notify()["events"], [])
        self.assertIn("  events: []\n", self.path.read_text(encoding="utf-8"))
        set_notify_events(list(NOTIFY_EVENTS), self.local)
        self.assertEqual(self.notify()["events"], list(NOTIFY_EVENTS))

    def test_a_block_without_events_gets_them(self):
        text = re.sub(r"\n  events:.*?(?=\n\S|\n\n)", "", self.path.read_text(encoding="utf-8"), flags=re.S)
        self.path.write_text(text, encoding="utf-8")
        self.assertNotIn("events", self.notify())
        set_notify_events(["pr_ready"], self.local)
        self.assertEqual(self.notify()["events"], ["pr_ready"])

    def test_an_unknown_kind_writes_nothing(self):
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
            set_notify_events(["pr_ready", "gossip"], self.local)
        self.assertIn("notify.events", caught.exception.problems[0])
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)


class NotifyApiTest(Base):
    def test_the_state_says_what_is_sent_but_never_the_bot_or_the_chat(self):
        notify = self.get("/api/v1/state")[1]["notify"]
        self.assertEqual(notify["kinds"], list(NOTIFY_EVENTS))
        self.assertEqual(notify["events"], load_config(self.root / "local").settings.notify.events)
        self.assertEqual(set(notify), {"adapter", "events", "kinds", "language"})
        self.assertNotIn("TELEGRAM", json.dumps(notify))

    def test_it_is_changed_with_the_token_and_the_next_cycle_reads_it(self):
        self.assertEqual(dispatch(self.api, "POST", "/api/v1/notify?events=task_done")[0], 403)
        status, body = dispatch(self.api, "POST", "/api/v1/notify?events=task_done,pr_ready", TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body["notify"]["events"], ["pr_ready", "task_done"])
        self.assertEqual(load_config(self.root / "local").settings.notify.events, ["pr_ready", "task_done"])
        self.assertEqual(self.get("/api/v1/state")[1]["notify"]["events"], ["pr_ready", "task_done"])
        self.assertEqual(self.store.last_event("notify_changed")["events"], ["pr_ready", "task_done"])
        self.assertEqual(dispatch(self.api, "POST", "/api/v1/notify?events=none", TOKEN)[1]["notify"]["events"], [])
        self.assertEqual(dispatch(self.api, "POST", "/api/v1/notify?events=gossip", TOKEN)[0], 400)
        self.assertEqual(dispatch(self.api, "POST", "/api/v1/notify", TOKEN)[0], 400)


class NotifyPagesTest(unittest.TestCase):
    def test_every_look_names_every_kind_in_both_languages(self):
        for look, path in PAGES.items():
            text = words(read(path))
            for kind in NOTIFY_EVENTS:
                for key in (f"notify_{kind}", f"notify_{kind}_hint"):
                    for lang in ("en", "fa"):
                        self.assertTrue(text[lang].get(key), f"{look} {lang}: {key}")
                    self.assertNotEqual(text["fa"][key], text["en"][key], f"{look}: {key}")
            self.assertEqual(line(read(path), "const NOTIFY_ORDER = "), line(read(dashboard.CLASSIC_PAGE), "const NOTIFY_ORDER = "))
            self.assertEqual(sorted(json.loads(line(read(path), "const NOTIFY_ORDER = ").split("= ")[1].rstrip(";\n"))), sorted(NOTIFY_EVENTS))

    def test_classic_new_has_the_telegram_button_and_its_pop_up(self):
        page = read(dashboard.CLASSIC_NEW_PAGE)
        self.assertIn('<button id="telegram" class="btn plain icon" aria-haspopup="dialog" data-hint="hint_telegram"', page)
        self.assertIn('<dialog id="tgsheet" class="sheet"', page)
        self.assertIn('$("telegram").onclick = openTelegram;', page)
        self.assertIn('$("telegram").hidden = !s.notify;', function(page, "refreshState"))

    @unittest.skipUnless(NODE, "needs node to run the page's script")
    def test_the_pop_up_changes_nothing_until_save_and_then_sends_the_whole_list(self):
        page = read(dashboard.CLASSIC_NEW_PAGE)
        seen = run_js(f"const TEXT = {json.dumps(some_words(page))};\nlet lang = \"fa\";\n" + line(page, "const esc = ") + line(page, "const t = ")
                      + "const shown = {}; const $ = id => (shown[id] ??= {innerHTML: '', showModal() { this.open = true; }, close() { this.open = false; }});\n"
                      + "const calls = []; const act = async (b, path) => { calls.push(path); return {}; }; const toast = () => {}; const refreshState = async () => {};\n"
                      + line(page, "const NOTIFY_ORDER = ") + line(page, "const notifyKinds = ") + line(page, "const notifyPath = ")
                      + line(page, "const TG_ICON = ") + "let tgDraft = null;\n"
                      + function(page, "openTelegram") + function(page, "drawTelegram") + function(page, "saveTelegram")
                      + """let lastState = {notify: {adapter: "telegram", events: ["pr_ready", "failure"], kinds: ["supervisor_stopped", "failure", "pr_ready", "brand_new"], language: "fa"}};
(async () => {
  openTelegram();
  const first = shown.tgbody.innerHTML, foot = shown.tgfoot.innerHTML;
  tgDraft.delete("pr_ready"); drawTelegram();
  await saveTelegram({});
  console.log(JSON.stringify({first, foot, after: shown.tgfoot.innerHTML, calls, open: shown.tgsheet.open}));
})();""")
        self.assertEqual(re.findall(r'aria-checked="(\w+)" data-tg-flip="(\w+)"', seen["first"]),
                         [("true", "pr_ready"), ("true", "failure"), ("false", "supervisor_stopped"), ("false", "brand_new")])
        self.assertIn(words(page)["fa"]["notify_pr_ready_hint"], seen["first"])
        self.assertIn("data-tg-save disabled", seen["foot"])
        self.assertNotIn("data-tg-save disabled", seen["after"])
        self.assertEqual(seen["calls"], ["/notify?events=failure"])
        self.assertFalse(seen["open"])

    @unittest.skipUnless(NODE, "needs node to run the page's script")
    def test_classic_and_kandu_clean_flip_one_kind_at_a_time(self):
        clean = read(dashboard.PAGE)
        seen = run_js(f"const TEXT = {json.dumps(some_words(clean))};\nlet lang = \"en\";\n" + line(clean, "const esc = ") + line(clean, "const t = ")
                      + line(clean, "const NOTIFY_ORDER = ") + line(clean, "const notifyKinds = ") + line(clean, "const notifyPath = ")
                      + function(clean, "switchRow") + function(clean, "notifyHtml")
                      + 'console.log(JSON.stringify(notifyHtml({adapter: "telegram", events: ["pr_ready", "failure"], kinds: ["failure", "pr_ready", "task_done"], language: "en"})));')
        self.assertEqual(re.findall(r'aria-checked="(\w+)" data-switch="([^"]+)"', seen),
                         [("true", "/notify?events=failure"), ("false", "/notify?events=pr_ready%2Cfailure%2Ctask_done"), ("true", "/notify?events=pr_ready")])
        classic = read(dashboard.CLASSIC_PAGE)
        self.assertIn('<div id="notifysettings" class="card pad"></div>', classic)
        self.assertIn('if (e.target.matches?.("[data-notify-kind]")) changeNotify(e.target);', classic)
        self.assertIn("s.notify", function(classic, "renderSettings").split("const key = ")[1].split("\n")[0])


if __name__ == "__main__":
    unittest.main()
