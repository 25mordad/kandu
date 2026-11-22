"""The install's version in every look's header, and whether it is the upstream's newest release (issue #97)."""
import json
import re
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta
from pathlib import Path

from kandu import dashboard, upstream, version
from kandu.github import GitHub, GitHubError
from tests.test_upstream import Base, git

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = "https://github.com/example-org/kandu.git"
RELEASE = "https://github.com/example-org/kandu/releases/tag/v1.2.0"
NOW = datetime(2026, 11, 18, 9, 0).astimezone()


class Installed(Base):
    """An install at 1.0.0 whose `upstream` remote is a public repo on GitHub, with GitHub mocked."""

    def setUp(self):
        super().setUp()
        (self.install / "VERSION").write_text("1.0.0\n", encoding="utf-8")
        git(self.install, "remote", "set-url", "upstream", PUBLIC)
        self.github.latest_release.return_value = {"tag": "v1.0.0", "url": "https://github.com/example-org/kandu/releases/tag/v1.0.0"}
        self.turn_on()

    def badge(self):
        return self.state()["version_badge"]

    def tick(self, at=NOW):
        self.api.upstream_tick(at)


class UpToDateTest(Installed):
    def test_the_badge_says_the_install_is_the_latest(self):
        self.tick()
        shown = self.badge()
        self.assertEqual((shown["installed"], shown["latest"], shown["up_to_date"]), ("1.0.0", "v1.0.0", True))
        self.github.latest_release.assert_called_once_with("example-org/kandu")

    def test_the_page_reads_the_release_from_kandu_never_from_github(self):
        self.tick()
        self.github.latest_release.reset_mock()
        for _ in range(3):
            self.badge()
        self.github.latest_release.assert_not_called()

    def test_a_page_read_runs_no_git(self):
        self.tick()
        with mock.patch("kandu.upstream.public_url", wraps=upstream.public_url) as remote:
            for _ in range(3):
                self.assertEqual(self.badge()["upstream"], "example-org/kandu")
        remote.assert_not_called()


class BehindTest(Installed):
    def test_the_badge_names_the_newer_version_and_links_its_changes(self):
        self.github.latest_release.return_value = {"tag": "v1.2.0", "url": RELEASE}
        self.tick()
        shown = self.badge()
        self.assertEqual((shown["installed"], shown["latest"], shown["up_to_date"], shown["url"]),
                         ("1.0.0", "v1.2.0", False, RELEASE))

    def test_a_newer_number_counts_by_its_parts_not_its_letters(self):
        self.assertTrue(version.is_newer("v1.10.0", "1.9.0"))
        self.assertFalse(version.is_newer("v1.9.0", "1.10.0"))
        self.assertFalse(version.is_newer("1.0.0", "1.0.0"))

    def test_nothing_updates_the_install(self):
        self.github.latest_release.return_value = {"tag": "v1.2.0", "url": RELEASE}
        head = git(self.install, "rev-parse", "HEAD")
        self.tick()
        self.badge()
        self.assertEqual(git(self.install, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.install, "status", "--porcelain"), "?? VERSION")
        self.assertEqual((self.install / "VERSION").read_text(encoding="utf-8"), "1.0.0\n")


class NoUpstreamTest(Installed):
    def test_with_the_upstream_check_off_nothing_is_read_and_the_latest_is_not_known(self):
        text = (self.local / "kandu.yaml").read_text(encoding="utf-8").replace("enabled: true\n  project", "enabled: false\n  project")
        (self.local / "kandu.yaml").write_text(text, encoding="utf-8")
        self.make_api()
        self.tick()
        self.github.latest_release.assert_not_called()
        shown = self.badge()
        self.assertEqual((shown["installed"], shown["upstream"], shown["latest"], shown["up_to_date"], shown["tried_at"]),
                         ("1.0.0", None, None, None, None))

    def test_a_remote_that_is_not_on_github_is_no_upstream(self):
        git(self.install, "remote", "set-url", "upstream", "https://git.example.com/kandu.git")
        self.tick()
        self.github.latest_release.assert_not_called()
        self.assertIsNone(self.badge()["latest"])

    def test_the_github_remotes_name_its_repo(self):
        for url in (PUBLIC, "https://github.com/example-org/kandu", "git@github.com:example-org/kandu.git",
                    "ssh://git@github.com/example-org/kandu.git", "https://user:pw@github.com/example-org/kandu.git"):
            git(self.install, "remote", "set-url", "upstream", url)
            self.assertEqual(version.upstream_repo(self.api.config.settings.upstream, self.install), "example-org/kandu", url)


class FailedReadTest(Installed):
    def test_a_failed_read_shows_only_the_installs_version_and_when_it_last_tried(self):
        self.github.latest_release.side_effect = GitHubError("gh api repos/example-org/kandu/releases/latest: HTTP 404")
        self.tick()
        shown = self.badge()
        self.assertEqual((shown["installed"], shown["latest"], shown["up_to_date"], shown["failed"]), ("1.0.0", None, None, True))
        self.assertEqual(datetime.fromisoformat(shown["tried_at"]), NOW.replace(microsecond=0))

    def test_a_good_read_before_a_failed_one_is_not_shown_as_the_latest(self):
        self.tick()
        self.github.latest_release.side_effect = GitHubError("gh api: network down")
        self.tick(NOW + timedelta(days=1))
        self.assertIsNone(self.badge()["latest"])


class OnceADayTest(Installed):
    def test_it_reads_github_at_most_once_a_day(self):
        self.tick()
        self.tick(NOW + timedelta(hours=1))
        self.tick(NOW + timedelta(hours=23, minutes=59))
        self.assertEqual(self.github.latest_release.call_count, 1)
        self.tick(NOW + timedelta(hours=24, minutes=1))
        self.assertEqual(self.github.latest_release.call_count, 2)

    def test_a_failed_read_waits_a_day_too(self):
        self.github.latest_release.side_effect = GitHubError("gh api: network down")
        self.tick()
        self.tick(NOW + timedelta(hours=5))
        self.assertEqual(self.github.latest_release.call_count, 1)

    def test_the_kept_read_outlives_a_restart(self):
        self.tick()
        self.make_api()
        self.tick(NOW + timedelta(hours=2))
        self.assertEqual(self.github.latest_release.call_count, 1)
        self.assertEqual(self.badge()["latest"], "v1.0.0")


class GitHubCallTest(unittest.TestCase):
    def test_it_asks_for_the_newest_release_and_keeps_its_tag_and_page(self):
        calls = []
        gh = GitHub(run=lambda args: calls.append(args) or json.dumps({"tag_name": "v1.2.0", "html_url": RELEASE, "body": "x"}))
        self.assertEqual(gh.latest_release("example-org/kandu"), {"tag": "v1.2.0", "url": RELEASE})
        self.assertEqual(calls, [["api", "repos/example-org/kandu/releases/latest"]])


class RepoTest(unittest.TestCase):
    def test_the_repo_holds_the_installs_version(self):
        self.assertRegex(version.installed(ROOT), r"^\d+\.\d+\.\d+$")


PAGES = {"clean": dashboard.PAGE, "classic": dashboard.CLASSIC_PAGE, "classic-new": dashboard.CLASSIC_NEW_PAGE}
TEXT = re.compile(r'<script type="application/json" id="text">(.*?)</script>', re.DOTALL)


class PageTest(unittest.TestCase):
    def test_every_looks_header_has_the_badge_beside_the_mark(self):
        for look, path in PAGES.items():
            page = path.read_text(encoding="utf-8")
            header = page[page.index('<header class="top">'):page.index("</header>")]
            self.assertRegex(header, r'class="brand"[^\n]*\n\s*<a id="version" class="version"', look)
            self.assertIn("renderVersion(s.version_badge)", page, look)
            self.assertRegex(page, r"\.version \{[^}]*min-width:0", look)

    def test_every_look_has_the_badges_words_in_both_languages(self):
        for look, path in PAGES.items():
            text = json.loads(TEXT.search(path.read_text(encoding="utf-8")).group(1))
            for key in ("version_latest", "version_newer", "version_unknown", "version_never_tried", "version_tried"):
                self.assertIn(key, text["en"], f"{look}: {key}")
                self.assertIn(key, text["fa"], f"{look}: {key}")

    def test_design_says_it_in_every_look(self):
        design = (ROOT / "DESIGN.md").read_text(encoding="utf-8")
        self.assertIn("once a day", design)
        self.assertRegex(design, r"(?i)version badge[^\n]*Kandu Clean, Classic and Classic new")


class VersionTest(unittest.TestCase):
    def test_the_repo_holds_kandus_version_starting_at_1_0_0(self):
        held = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(held, r"^\d+\.\d+\.\d+$")
        self.assertFalse(version.is_newer("1.0.0", held), f"VERSION {held} is older than 1.0.0")

    def test_the_install_reads_the_version_from_the_repo(self):
        self.assertEqual(version.read(), (ROOT / "VERSION").read_text(encoding="utf-8").strip())
        self.assertRegex(version.read(), r"^\d+\.\d+\.\d+$")

    def test_another_file_is_read_as_it_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "VERSION"
            path.write_text("2.3.4\n", encoding="utf-8")
            self.assertEqual(version.read(path), "2.3.4")

    def test_a_missing_file_reads_as_no_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(version.read(Path(tmp) / "VERSION"))
