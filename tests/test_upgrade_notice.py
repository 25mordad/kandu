"""The dashboard's upgrade notice: what a new version brought, until the user marks it as read (issue #123)."""
import json
import re
import subprocess
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from kandu import dashboard
from kandu.dashboard import dispatch, make_handler
from tests.test_looks import NODE, browser, profile_folder
from tests.test_upstream import TOKEN
from tests.test_version import NOW, Installed

ROOT = Path(__file__).resolve().parent.parent
TEXT = re.compile(r'<script type="application/json" id="text">(.*?)</script>', re.DOTALL)
LOOKS = {"clean": dashboard.PAGE, "classic": dashboard.CLASSIC_PAGE, "classic-new": dashboard.CLASSIC_NEW_PAGE}

E120 = "## 1.2.0 - 2026-11-20\n\n### Features\n- Show the **queen's** brief (#12)\n\n### Fixes\n- Keep the <meadow> still (#13)"
E111 = "## 1.1.1 - 2026-11-15\n\n### Fixes\n- Count tokens once (#11)"
E110 = "## 1.1.0 - 2026-11-10\n\n### Features\n- Pick a model per role (#10)"
E100 = "## 1.0.0 - 2026-11-01\n\n### Other\n- The first release (#1)"
CHANGELOG = "# Changelog\n\n" + "\n\n".join([E120, E111, E110, E100]) + "\n"
PUBLIC_RELEASES = "https://github.com/example-org/kandu/releases/tag/"


class Upgraded(Installed):
    """An install whose `VERSION` and `CHANGELOG.md` the test sets, and whose read mark it sets in the state."""

    def setUp(self):
        super().setUp()
        self.github.latest_release.return_value = {"tag": "v0.9.0", "url": PUBLIC_RELEASES + "v0.9.0"}
        self.install_version("1.2.0", CHANGELOG)

    def install_version(self, number, changelog=None):
        (self.install / "VERSION").write_text(f"{number}\n", encoding="utf-8")
        if changelog is not None:
            (self.install / "CHANGELOG.md").write_text(changelog, encoding="utf-8")

    def mark(self, number):
        (self.root / "state").mkdir(exist_ok=True)
        (self.root / "state" / "version-read").write_text(f"{number}\n", encoding="utf-8")

    def notice(self):
        return self.state()["upgrade"]

    def shown(self):
        return [entry["version"] for entry in self.notice()["entries"]]

    def read(self, number, token=TOKEN):
        return dispatch(self.api, "POST", f"/api/v1/upgrade/read?version={number}", token)


class OneUnreadTest(Upgraded):
    def test_a_newer_version_shows_its_entry_exactly_as_written(self):
        self.mark("1.1.1")
        notice = self.notice()
        self.assertEqual(notice["version"], "1.2.0")
        self.assertEqual(notice["entries"], [{"version": "1.2.0", "text": E120, "url": None}])

    def test_on_the_first_run_with_no_read_mark_only_the_installed_version_shows(self):
        self.assertEqual(self.shown(), ["1.2.0"])


class SeveralUnreadTest(Upgraded):
    def test_each_unread_version_shows_newest_first_under_one_notice(self):
        self.mark("1.0.0")
        self.assertEqual(self.shown(), ["1.2.0", "1.1.1", "1.1.0"])
        self.assertEqual([e["text"] for e in self.notice()["entries"]], [E120, E111, E110])

    def test_the_order_is_the_versions_not_the_files(self):
        self.install_version("1.2.0", "# Changelog\n\n" + "\n\n".join([E110, E120, E100, E111]) + "\n")
        self.mark("1.0.0")
        self.assertEqual(self.shown(), ["1.2.0", "1.1.1", "1.1.0"])

    def test_an_unread_version_with_no_entry_is_left_out(self):
        self.install_version("1.2.0", "# Changelog\n\n" + "\n\n".join([E111, E100]) + "\n")
        self.mark("1.0.0")
        self.assertEqual(self.shown(), ["1.1.1"])
        self.assertEqual(self.notice()["version"], "1.2.0")


class MarkAsReadTest(Upgraded):
    def test_mark_as_read_hides_the_notice_and_logs_it(self):
        self.mark("1.0.0")
        status, body = self.read("1.2.0")
        self.assertEqual(status, 200, body)
        self.assertIsNone(self.notice())
        events = [e for e in self.store.events(self.store.today()) if e["type"] == "upgrade_read"]
        self.assertEqual([e["version"] for e in events], ["1.2.0"])

    def test_the_read_mark_survives_a_restart(self):
        self.read("1.2.0")
        self.make_api()
        self.assertIsNone(self.notice())

    def test_mark_as_read_needs_the_token(self):
        for token in (None, "wrong"):
            status, _ = self.read("1.2.0", token)
            self.assertEqual(status, 403)
        self.assertEqual(self.shown(), ["1.2.0"])
        self.assertFalse((self.root / "state" / "version-read").exists())

    def test_a_version_that_is_not_the_newest_shown_marks_nothing(self):
        self.mark("1.0.0")
        for number in ("1.1.0", "1.3.0", ""):
            status, _ = self.read(number)
            self.assertEqual(status, 409, number)
        self.assertEqual(self.shown(), ["1.2.0", "1.1.1", "1.1.0"])

    def test_with_no_notice_there_is_nothing_to_mark(self):
        self.mark("1.2.0")
        status, _ = self.read("1.2.0")
        self.assertEqual(status, 409)

    def test_a_newer_version_after_the_read_shows_only_itself(self):
        self.read("1.2.0")
        e130 = "## 1.3.0 - 2026-11-25\n\n### Fixes\n- Draw the hive (#14)"
        self.install_version("1.3.0", "# Changelog\n\n" + e130 + "\n\n" + CHANGELOG.split("\n\n", 1)[1])
        self.assertEqual(self.shown(), ["1.3.0"])


class UnchangedVersionTest(Upgraded):
    def test_a_deploy_that_keeps_the_version_shows_nothing_new(self):
        self.read("1.2.0")
        # A deploy that brings in commits but no release: the files change, `VERSION` does not.
        (self.install / "CHANGELOG.md").write_text(CHANGELOG + "\n", encoding="utf-8")
        self.make_api()
        self.assertIsNone(self.notice())

    def test_an_older_installed_version_than_the_mark_shows_nothing(self):
        self.mark("1.2.0")
        self.install_version("1.1.0")
        self.assertIsNone(self.notice())


class NoChangelogTest(Upgraded):
    def test_with_no_changelog_no_notice_shows_and_the_page_works(self):
        (self.install / "CHANGELOG.md").unlink()
        state = self.state()
        self.assertIsNone(state["upgrade"])
        self.assertEqual(state["version_badge"]["installed"], "1.2.0")

    def test_with_no_entry_for_any_unread_version_no_notice_shows(self):
        self.install_version("1.2.0", "# Changelog\n\n" + E100 + "\n")
        self.mark("1.0.0")
        self.assertIsNone(self.notice())

    def test_with_no_version_file_no_notice_shows(self):
        (self.install / "VERSION").unlink()
        self.assertIsNone(self.notice())


class ReleaseLinkTest(Upgraded):
    def test_a_version_published_on_the_upstream_links_to_its_release(self):
        self.github.latest_release.return_value = {"tag": "v1.2.0", "url": PUBLIC_RELEASES + "v1.2.0"}
        self.tick(NOW)
        self.mark("1.0.0")
        urls = {e["version"]: e["url"] for e in self.notice()["entries"]}
        self.assertEqual(urls, {"1.2.0": PUBLIC_RELEASES + "v1.2.0", "1.1.1": PUBLIC_RELEASES + "v1.1.1",
                                "1.1.0": PUBLIC_RELEASES + "v1.1.0"})

    def test_a_version_newer_than_the_upstreams_latest_release_has_no_link(self):
        self.github.latest_release.return_value = {"tag": "v1.1.1", "url": PUBLIC_RELEASES + "v1.1.1"}
        self.tick(NOW)
        self.mark("1.0.0")
        urls = {e["version"]: e["url"] for e in self.notice()["entries"]}
        self.assertEqual(urls, {"1.2.0": None, "1.1.1": PUBLIC_RELEASES + "v1.1.1", "1.1.0": PUBLIC_RELEASES + "v1.1.0"})

    def test_without_an_upstream_nothing_links(self):
        self.tick(NOW)
        self.turn_on(enabled="false")
        self.assertEqual([e["url"] for e in self.notice()["entries"]], [None])


class PagesTest(unittest.TestCase):
    def test_every_look_shows_the_notice_at_the_top_with_its_words_in_both_languages(self):
        for look, path in LOOKS.items():
            page = path.read_text(encoding="utf-8")
            main = page[page.index("<main>"):]
            self.assertRegex(main, r'^<main>\s*<section id="upgrade" class="upgrade"[^>]*hidden', look)
            self.assertIn("renderUpgrade(s.upgrade)", page, look)
            words = json.loads(TEXT.search(page).group(1))
            self.assertEqual(words["en"]["upgrade_title"], "Kandu was upgraded to {v}", look)
            self.assertEqual(words["en"]["upgrade_read"], "Mark as read", look)
            self.assertTrue(words["fa"]["upgrade_title"].count("{v}") == 1, look)
            self.assertTrue(words["fa"]["upgrade_read"], look)
            self.assertTrue(words["values_fa"].get("upgrade_read"), look)

    def test_the_design_the_api_and_the_state_describe_it(self):
        self.assertIn("**Upgrade notice:**", (ROOT / "DESIGN.md").read_text(encoding="utf-8"))
        self.assertIn("/api/v1/upgrade/read", (ROOT / "docs" / "API.md").read_text(encoding="utf-8"))
        self.assertIn("version-read", (ROOT / "docs" / "STATE.md").read_text(encoding="utf-8"))
        self.assertIn("**Upgrade notice**", (ROOT / "GLOSSARY.md").read_text(encoding="utf-8"))


class BrowserTest(Upgraded):
    """Each look in a real headless browser, in both languages: the notice shows, and «Mark as read» hides it for
    every browser."""

    def setUp(self):
        super().setUp()
        self.store.write_current({"supervisor": "awake", "usage": {"session": 30, "week": 65}, "next_wake": None,
                                  "workers": []})
        self.github.board_items.return_value = []
        self.github.latest_release.return_value = {"tag": "v1.2.0", "url": PUBLIC_RELEASES + "v1.2.0"}
        self.tick(NOW)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.api))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"

    def drive(self, plan):
        with profile_folder() as profile:
            done = subprocess.run([NODE, str(ROOT / "tests" / "drive_upgrade.js"), browser(), self.base, profile, TOKEN,
                                   json.dumps(plan)], capture_output=True, text=True, timeout=170)
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        report = json.loads(done.stdout)
        self.assertEqual(report["errors"], [])
        return report["results"]

    @unittest.skipUnless(NODE and browser(), "needs node and a Chromium")
    def test_in_every_look_and_language_the_notice_shows_each_unread_entry_as_written(self):
        self.mark("1.0.0")
        cases = [{"look": look, "lang": lang} for look in LOOKS for lang in ("en", "fa")]
        for case in self.drive({"cases": cases, "press": False}):
            look, lang, shown = case["look"], case["lang"], case["shown"]
            where = f"{look} {lang}"
            self.assertEqual(case["page"], look, where)
            self.assertFalse(shown["hidden"], where)
            words = json.loads(TEXT.search(LOOKS[look].read_text(encoding="utf-8")).group(1))[lang]
            self.assertEqual(shown["title"], words["upgrade_title"].replace("{v}", "1.2.0"), where)
            self.assertEqual(shown["button"], words["upgrade_read"], where)
            self.assertEqual(shown["entries"], [E120, E111, E110], where)
            self.assertEqual(shown["links"], [PUBLIC_RELEASES + v for v in ("v1.2.0", "v1.1.1", "v1.1.0")], where)

    @unittest.skipUnless(NODE and browser(), "needs node and a Chromium")
    def test_in_every_look_mark_as_read_hides_it_in_another_browser_after_a_reload(self):
        for look in LOOKS:
            self.mark("1.0.0")
            [case] = self.drive({"cases": [{"look": look, "lang": "en"}], "press": True})
            self.assertFalse(case["shown"]["hidden"], look)
            self.assertTrue(case["gone"], look)
            self.assertTrue(case["elsewhere_hidden"], look)
            self.assertEqual((self.root / "state" / "version-read").read_text(encoding="utf-8").strip(), "1.2.0")

    @unittest.skipUnless(NODE and browser(), "needs node and a Chromium")
    def test_with_nothing_unread_no_look_shows_the_notice(self):
        self.mark("1.2.0")
        for case in self.drive({"cases": [{"look": look, "lang": "en"} for look in LOOKS], "press": False}):
            self.assertTrue(case["shown"]["hidden"], case["look"])


if __name__ == "__main__":
    unittest.main()
