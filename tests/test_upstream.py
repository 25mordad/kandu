"""The install's `main` against the public Kandu it came from, and the one task that brings the updates in (issue #1)."""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from kandu import dashboard, notify, upstream
from kandu.config import ConfigError, load_config, set_notify_events
from kandu.dashboard import Api, dispatch
from kandu.github import GitHubError
from kandu.state import StateStore

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "secret-token"
REPO = "acme-org/website"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(cwd, name):
    (Path(cwd) / name).write_text(name, encoding="utf-8")
    git(cwd, "add", name)
    git(cwd, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", name)


def remove(cwd, *names):
    """A commit that takes `names` out again."""
    git(cwd, "rm", "-q", *names)
    git(cwd, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", f"remove {' '.join(names)}")


def merge(cwd, ref):
    git(cwd, "-c", "user.name=t", "-c", "user.email=t@example.com", "merge", "-q", "--no-ff", "--no-edit", ref)


class Base(unittest.TestCase):
    """A public repo, and an install cloned from it with that repo as `upstream`."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.public = self.root / "public"
        self.public.mkdir()
        git(self.public, "init", "-q", "-b", "main")
        commit(self.public, "first")
        self.install = self.root / "install"
        git(self.root, "clone", "-q", "-o", "upstream", str(self.public), str(self.install))
        local = self.root / "local"
        local.mkdir()
        shutil.copy(ROOT / "config.example" / "kandu.example.yaml", local / "kandu.yaml")
        shutil.copy(ROOT / "config.example" / "projects.example.yaml", local / "projects.yaml")
        self.local = local
        self.store = StateStore(self.root / "state")
        self.github = mock.Mock()
        self.github.repo_is_public.return_value = False
        self.github.create_issue.side_effect = lambda *a, **k: f"https://github.com/{REPO}/issues/{40 + self.github.create_issue.call_count}"
        self.github.issue_is_open.return_value = True
        self.api = None

    def turn_on(self, **values):
        text = (self.local / "kandu.yaml").read_text(encoding="utf-8")
        settings = {"enabled": "true", "project": "website"} | values
        block = "\n".join(f"  {key}: {value}" for key, value in settings.items())
        text = re.sub(r"^upstream:.*?(?=^\S|\Z)", "", text, flags=re.MULTILINE | re.DOTALL)
        (self.local / "kandu.yaml").write_text(f"{text.rstrip()}\n\nupstream:\n{block}\n", encoding="utf-8")
        self.make_api()

    def make_api(self):
        self.api = Api(load_config(self.local), self.store, self.github, self.root / "logs", TOKEN,
                       timer_on=lambda: True, local_dir=self.local, install_dir=self.install)

    def state(self):
        status, body = dispatch(self.api, "GET", "/api/v1/state")
        self.assertEqual(status, 200)
        return body

    def post(self, path, token=TOKEN):
        return dispatch(self.api, "POST", f"/api/v1{path}", token)

    def check(self):
        status, body = self.post("/upstream/check")
        self.assertEqual(status, 200, body)
        return body


class CountTest(Base):
    def setUp(self):
        super().setUp()
        self.turn_on()

    def test_up_to_date(self):
        self.check()
        shown = self.state()["upstream"]
        self.assertEqual((shown["behind"], shown["ahead"], shown["failed"]), (0, 0, False))

    def test_behind(self):
        commit(self.public, "a")
        commit(self.public, "b")
        self.check()
        shown = self.state()["upstream"]
        self.assertEqual((shown["behind"], shown["ahead"]), (2, 0))

    def test_ahead(self):
        commit(self.install, "mine")
        self.check()
        shown = self.state()["upstream"]
        self.assertEqual((shown["behind"], shown["ahead"]), (0, 1))

    def test_both(self):
        commit(self.public, "a")
        commit(self.public, "b")
        commit(self.public, "c")
        commit(self.install, "mine")
        self.check()
        shown = self.state()["upstream"]
        self.assertEqual((shown["behind"], shown["ahead"]), (3, 1))

    def test_main_is_counted_even_when_another_branch_is_checked_out(self):
        commit(self.public, "a")
        git(self.install, "checkout", "-q", "-b", "side")
        commit(self.install, "side work")
        self.check()
        shown = self.state()["upstream"]
        self.assertEqual((shown["behind"], shown["ahead"]), (1, 0))

    def test_the_check_only_fetches_and_counts(self):
        commit(self.public, "a")
        commit(self.install, "mine")
        (self.install / "unsaved").write_text("work in progress", encoding="utf-8")
        before = (git(self.install, "rev-parse", "main"), git(self.install, "status", "--porcelain"),
                  git(self.install, "branch", "--list"), git(self.install, "rev-parse", "HEAD"))
        self.check()
        after = (git(self.install, "rev-parse", "main"), git(self.install, "status", "--porcelain"),
                 git(self.install, "branch", "--list"), git(self.install, "rev-parse", "HEAD"))
        self.assertEqual(before, after)
        self.assertEqual(git(self.public, "rev-list", "--count", "main"), "2")  # nothing pushed to it

    def test_the_check_costs_github_nothing(self):
        self.check()
        self.assertEqual(self.github.method_calls, [])


class OwnChangesTest(Base):
    """"Ahead" counts only the install's own changes that `main` still holds."""

    def setUp(self):
        super().setUp()
        self.turn_on()

    def counts(self):
        self.check()
        shown = self.state()["upstream"]
        return shown["behind"], shown["ahead"]

    def test_files_that_came_back_to_upstreams_leave_only_the_commits_after_them(self):
        commit(self.install, "mine-1")
        commit(self.install, "mine-2")
        remove(self.install, "mine-1", "mine-2")  # main's files are upstream's again
        commit(self.install, "mine-3")
        self.assertEqual(self.counts(), (0, 1))

    def test_main_with_upstreams_files_is_not_ahead(self):
        commit(self.install, "mine-1")
        commit(self.install, "mine-2")
        remove(self.install, "mine-1", "mine-2")
        self.assertEqual(self.counts(), (0, 0))

    def test_a_merge_commit_is_not_counted(self):
        commit(self.public, "a")
        commit(self.install, "mine")
        git(self.install, "fetch", "-q", "upstream")
        merge(self.install, "upstream/main")
        self.assertEqual(self.counts(), (0, 1))

    def test_a_merge_of_upstream_after_the_files_came_back_still_leaves_only_the_commits_after_them(self):
        commit(self.install, "mine-1")
        commit(self.install, "mine-2")
        remove(self.install, "mine-1", "mine-2")  # main's files are upstream's again
        commit(self.install, "mine-3")
        commit(self.public, "a")
        git(self.install, "fetch", "-q", "upstream")
        merge(self.install, "upstream/main")
        self.assertEqual(self.counts(), (0, 1))

    def test_commits_before_files_equal_to_an_older_upstream_commit_are_not_counted(self):
        commit(self.install, "mine-1")
        remove(self.install, "mine-1")  # the files of upstream's first commit
        commit(self.install, "mine-2")
        commit(self.public, "a")
        self.assertEqual(self.counts(), (1, 1))

    def test_bring_in_states_this_count(self):
        commit(self.install, "mine-1")
        commit(self.install, "mine-2")
        remove(self.install, "mine-1", "mine-2")
        commit(self.install, "mine-3")
        commit(self.public, "a")
        self.check()
        status, body = self.post("/upstream/bring-in")
        self.assertEqual(status, 200, body)
        text = self.github.create_issue.call_args.args[2]
        self.assertIn("holds 1 commit of its own", text)


class NotifyTest(Base):
    """A check tells the user on Telegram when its counts changed, and once when it starts failing."""

    def setUp(self):
        super().setUp()
        self.sent = []
        patcher = mock.patch.object(notify.Telegram, "send", autospec=True,
                                    side_effect=lambda adapter, text, buttons=None: self.sent.append(text))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.turn_on()

    def set_language(self, language):
        path = self.local / "kandu.yaml"
        text = re.sub(r"^(  language:) en\b", rf"\1 {language}", path.read_text(encoding="utf-8"), count=1,
                      flags=re.MULTILINE)
        path.write_text(text, encoding="utf-8")
        self.make_api()

    def break_remote(self):
        git(self.install, "remote", "set-url", "upstream", str(self.root / "gone"))

    def heal(self):
        git(self.install, "remote", "set-url", "upstream", str(self.public))

    def test_the_first_check_ever_sends_nothing(self):
        commit(self.public, "a")
        self.check()
        self.assertEqual(self.sent, [])

    def test_a_first_check_that_fails_sends_nothing(self):
        self.break_remote()
        self.check()
        self.assertEqual(self.sent, [])

    def test_a_changed_count_sends_both_counts_and_the_button_to_press(self):
        self.check()
        commit(self.public, "a")
        commit(self.public, "b")
        commit(self.install, "mine")
        self.check()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("2 behind", self.sent[0])
        self.assertIn("1 ahead", self.sent[0])
        self.assertIn("«Bring in updates»", self.sent[0])
        self.assertIn("dashboard", self.sent[0])

    def test_a_changed_ahead_alone_names_no_button(self):
        self.check()
        commit(self.install, "mine")
        self.check()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("0 behind", self.sent[0])
        self.assertIn("1 ahead", self.sent[0])
        self.assertNotIn("Bring in updates", self.sent[0])

    def test_an_unchanged_count_sends_nothing(self):
        commit(self.public, "a")
        self.check()
        self.check()
        self.assertEqual(self.sent, [])

    def test_a_failure_is_sent_once_until_a_check_succeeds(self):
        self.check()
        self.break_remote()
        self.check()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("could not check", self.sent[0].lower())
        self.check()
        self.assertEqual(len(self.sent), 1)  # the second failure in a row sends nothing
        self.heal()
        self.check()
        self.assertEqual(len(self.sent), 1)  # back, with the counts of the last good check
        self.break_remote()
        self.check()
        self.assertEqual(len(self.sent), 2)  # failing again after a success is news again

    def test_a_success_after_a_failure_sends_a_changed_count(self):
        self.check()
        self.break_remote()
        self.check()
        self.heal()
        commit(self.public, "a")
        self.check()
        self.assertEqual(len(self.sent), 2)
        self.assertIn("1 behind", self.sent[1])

    def test_the_scheduled_check_sends_too(self):
        now = datetime.now().astimezone()
        self.api.upstream_tick(now)
        commit(self.public, "a")
        self.api.upstream_tick(now + timedelta(hours=5))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("1 behind", self.sent[0])

    def test_nothing_is_sent_while_the_kind_is_off(self):
        kinds = [kind for kind in load_config(self.local).settings.notify.events if kind != "upstream"]
        set_notify_events(kinds, self.local)
        self.make_api()
        self.check()
        commit(self.public, "a")
        self.check()
        self.break_remote()
        self.check()
        self.assertEqual(self.sent, [])

    def test_it_is_said_in_the_notifications_language(self):
        self.set_language("fa")
        self.check()
        commit(self.public, "a")
        self.check()
        self.assertEqual(len(self.sent), 1)
        self.assertIn(notify.MESSAGES["fa"]["upstream_behind"].split("{")[0].strip(), self.sent[0])
        self.assertIn("آوردن به‌روزرسانی‌ها", self.sent[0])
        self.assertNotIn("behind", self.sent[0])

    def test_it_is_on_by_default_without_a_list_of_events(self):
        path = self.local / "kandu.yaml"
        text = re.sub(r"^  events:.*?(?=^  \S|^\S)", "", path.read_text(encoding="utf-8"), count=1,
                      flags=re.MULTILINE | re.DOTALL)
        path.write_text(text, encoding="utf-8")
        self.assertIn("upstream", load_config(self.local).settings.notify.events)


class FailedCheckTest(Base):
    def setUp(self):
        super().setUp()
        self.turn_on()

    def test_a_missing_remote_says_could_not_check_with_the_last_good_check(self):
        self.check()
        good = self.store.last_event("upstream_checked")["ts"]
        git(self.install, "remote", "remove", "upstream")
        body = self.check()
        self.assertTrue(body["failed"])
        state = self.state()
        self.assertTrue(state["upstream"]["failed"])
        self.assertEqual(state["upstream"]["last_good_at"], good)
        for key in ("flowers", "backup", "limits", "project_order"):  # the rest of the dashboard still answers
            self.assertIn(key, state)

    def test_a_failure_before_any_good_check_has_no_last_good_time(self):
        self.turn_on(remote="nowhere")
        self.check()
        shown = self.state()["upstream"]
        self.assertEqual((shown["failed"], shown["last_good_at"]), (True, None))

    def test_an_unreachable_remote_fails_the_check(self):
        git(self.install, "remote", "set-url", "upstream", str(self.root / "gone"))
        self.assertTrue(self.check()["failed"])

    def test_the_reason_keeps_no_secret_of_the_remote(self):
        git(self.install, "remote", "set-url", "upstream", "https://user:hunter2@127.0.0.1:9/x.git")
        self.check()
        self.assertNotIn("hunter2", json.dumps(self.store.last_event("upstream_check_failed")))
        self.assertNotIn("hunter2", json.dumps(self.state()["upstream"]))


class ScheduleTest(Base):
    def setUp(self):
        super().setUp()
        self.turn_on(every_hours=4)

    def test_the_first_tick_checks_and_the_next_waits_its_hours(self):
        now = datetime.now().astimezone()
        self.assertTrue(self.api.upstream_tick(now))
        self.assertFalse(self.api.upstream_tick(now + timedelta(hours=3, minutes=59)))
        self.assertTrue(self.api.upstream_tick(now + timedelta(hours=4, minutes=1)))

    def test_a_failed_check_also_waits_its_hours(self):
        git(self.install, "remote", "remove", "upstream")
        now = datetime.now().astimezone()
        self.assertTrue(self.api.upstream_tick(now))
        self.assertFalse(self.api.upstream_tick(now + timedelta(hours=1)))

    def test_the_hours_come_from_the_config(self):
        self.turn_on(every_hours=1)
        now = datetime.now().astimezone()
        self.api.upstream_tick(now)
        self.assertTrue(self.api.upstream_tick(now + timedelta(hours=1, minutes=1)))

    def test_no_tick_checks_while_it_is_off(self):
        self.make_api_off()
        self.assertFalse(self.api.upstream_tick(datetime.now().astimezone()))
        self.assertIsNone(self.store.last_event("upstream_checked"))

    def make_api_off(self):
        shutil.copy(ROOT / "config.example" / "kandu.example.yaml", self.local / "kandu.yaml")
        self.make_api()


class OffTest(Base):
    def test_it_is_off_by_default_and_shows_nothing(self):
        self.make_api()
        self.assertFalse(load_config(self.local).settings.upstream.enabled)
        self.assertIsNone(self.state()["upstream"])
        status, _ = self.post("/upstream/check")
        self.assertEqual(status, 409)
        status, _ = self.post("/upstream/bring-in")
        self.assertEqual(status, 409)
        self.assertIsNone(self.store.last_event("upstream_checked"))
        self.github.create_issue.assert_not_called()

    def test_the_page_draws_nothing_without_it(self):
        page = dashboard.PAGE.read_text(encoding="utf-8")
        card = page.split("function upstreamCard(")[1].split("\n}\n")[0]
        self.assertIn('if (!u) return "";', card)
        self.assertIn("upstreamCard(s.upstream)", page)


class BringInTest(Base):
    def setUp(self):
        super().setUp()
        self.turn_on()
        commit(self.public, "a")
        commit(self.public, "b")
        self.check()

    def test_controls_need_the_token(self):
        self.assertEqual(self.post("/upstream/check", token=None)[0], 403)
        self.assertEqual(self.post("/upstream/bring-in", token=None)[0], 403)
        self.assertEqual(self.post("/upstream/bring-in", token="wrong")[0], 403)
        self.github.create_issue.assert_not_called()

    def test_one_task_on_the_board_of_kandus_own_project(self):
        status, body = self.post("/upstream/bring-in")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["created"])
        self.github.create_issue.assert_called_once()
        repo, title, text = self.github.create_issue.call_args.args[:3]
        self.assertEqual(repo, REPO)  # the repo of `upstream.project`
        self.assertIn("2", title)
        self.assertIn("2 commit", text)
        self.assertIn("`main`", text)
        self.assertIn("own change", text)
        self.assertIn("merged there", text)  # a change sent upstream takes upstream's version
        self.assertIn("tests pass", text)
        self.assertIn("through", text.lower())
        board = load_config(self.local).projects[0].board
        self.github.add_to_board.assert_called_once_with(board, body["url"])
        self.assertEqual(self.github.set_status.call_args.args[1:], (body["url"], "Backlog"))

    def test_a_second_press_links_to_the_open_task(self):
        first = self.post("/upstream/bring-in")[1]
        status, second = self.post("/upstream/bring-in")
        self.assertEqual(status, 200)
        self.assertFalse(second["created"])
        self.assertEqual((second["task"], second["url"]), (first["task"], first["url"]))
        self.github.create_issue.assert_called_once()
        self.assertEqual(self.state()["upstream"]["task"], {"task": first["task"], "url": first["url"]})

    def test_a_task_the_copy_does_not_know_yet_counts_as_open(self):
        self.post("/upstream/bring-in")
        self.github.issue_is_open.side_effect = GitHubError("no such issue")
        self.assertFalse(self.post("/upstream/bring-in")[1]["created"])
        self.github.create_issue.assert_called_once()

    def test_once_that_task_is_closed_a_new_press_makes_a_new_one(self):
        first = self.post("/upstream/bring-in")[1]
        self.github.issue_is_open.return_value = False
        self.assertIsNone(self.state()["upstream"]["task"])
        second = self.post("/upstream/bring-in")[1]
        self.assertTrue(second["created"])
        self.assertNotEqual(second["task"], first["task"])

    def test_nothing_to_bring_in_makes_no_task(self):
        git(self.install, "pull", "-q", "--ff-only", "upstream", "main")
        self.check()
        self.assertEqual(self.post("/upstream/bring-in")[0], 409)
        self.github.create_issue.assert_not_called()

    def body_for_remote(self, url):
        """The task's text when `upstream`'s URL is `url`; the check already ran, so only the URL is read."""
        git(self.install, "remote", "set-url", "upstream", url)
        status, body = self.post("/upstream/bring-in")
        self.assertEqual(status, 200, body)
        return self.github.create_issue.call_args.args[2]

    def test_the_task_names_an_https_remote_without_its_user_or_password(self):
        text = self.body_for_remote("https://someone:s3cret@git.example.com:8443/acme/kandu.git")
        self.assertIn("https://git.example.com:8443/acme/kandu.git", text)
        self.assertNotIn("s3cret", text)
        self.assertNotIn("someone", text)

    def test_the_task_names_an_scp_style_remote_as_it_is(self):
        self.assertIn("git@git.example.com:acme/kandu.git", self.body_for_remote("git@git.example.com:acme/kandu.git"))

    def test_the_task_names_a_file_remote_that_has_no_host(self):
        self.assertIn("file:///srv/kandu.git", self.body_for_remote("file:///srv/kandu.git"))

    def test_the_task_keeps_the_brackets_of_an_ipv6_host(self):
        self.assertIn("https://[2001:db8::1]:8443/acme/kandu.git",
                      self.body_for_remote("https://token@[2001:db8::1]:8443/acme/kandu.git"))

    def test_it_never_merges_or_pushes(self):
        main = git(self.install, "rev-parse", "main")
        public = git(self.public, "rev-parse", "main")
        self.post("/upstream/bring-in")
        self.assertEqual((git(self.install, "rev-parse", "main"), git(self.public, "rev-parse", "main")), (main, public))
        called = {name for name, *_ in self.github.method_calls}
        self.assertFalse(called & {"merge_pr", "merge", "push"})


class ConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        shutil.copy(ROOT / "config.example" / "kandu.example.yaml", self.local / "kandu.yaml")
        shutil.copy(ROOT / "config.example" / "projects.example.yaml", self.local / "projects.yaml")

    def write(self, block):
        text = (self.local / "kandu.yaml").read_text(encoding="utf-8")
        text = re.sub(r"^upstream:.*?(?=^\S|\Z)", "", text, flags=re.MULTILINE | re.DOTALL)
        (self.local / "kandu.yaml").write_text(f"{text.rstrip()}\n\nupstream:\n{block}\n", encoding="utf-8")

    def problems(self, block):
        self.write(block)
        with self.assertRaises(ConfigError) as caught:
            load_config(self.local)
        return "\n".join(caught.exception.problems)

    def test_the_example_has_it_off_with_generic_values(self):
        text = (ROOT / "config.example" / "kandu.example.yaml").read_text(encoding="utf-8")
        block = re.search(r"^upstream:.*?(?=^\S|\Z)", text, re.MULTILINE | re.DOTALL).group(0)
        self.assertIn("enabled: false", block)
        self.assertNotIn("http", block)
        self.assertNotIn("github.com", block)
        settings = load_config(self.local).settings.upstream
        self.assertEqual((settings.enabled, settings.remote, settings.branch, settings.every_hours), (False, "upstream", "main", 4))

    def test_turned_on_it_needs_a_project_with_a_board(self):
        self.assertIn("upstream.project", self.problems("  enabled: true\n  project: nowhere"))
        self.write("  enabled: true\n  project: website")
        self.assertEqual(load_config(self.local).settings.upstream.project, "website")

    def test_a_remote_or_branch_cannot_be_a_flag(self):
        self.assertIn("upstream.remote", self.problems("  enabled: true\n  project: website\n  remote: --upload-pack=x"))
        self.assertIn("upstream.branch", self.problems("  enabled: true\n  project: website\n  branch: -x"))

    def test_the_hours_are_at_least_one(self):
        self.assertIn("upstream.every_hours", self.problems("  every_hours: 0"))


class SetupTest(unittest.TestCase):
    def test_kandu_setup_asks_about_it(self):
        setup = (ROOT / ".claude" / "commands" / "kandu-setup.md").read_text(encoding="utf-8")
        self.assertIn("(`upstream`, off by default)", setup)

    def test_kandu_setup_offers_its_notification_kind(self):
        setup = (ROOT / ".claude" / "commands" / "kandu-setup.md").read_text(encoding="utf-8")
        question = next(l for l in setup.splitlines() if "`notify.events`" in l)
        self.assertIn("`upstream`", question)


class NotificationKindDocsTest(unittest.TestCase):
    def test_the_config_example_lists_it(self):
        text = (ROOT / "config.example" / "kandu.example.yaml").read_text(encoding="utf-8")
        events = re.search(r"^  events:.*?(?=^\S)", text, re.MULTILINE | re.DOTALL).group(0)
        self.assertRegex(events, r"\n    - upstream +#")

    def test_design_describes_it(self):
        design = (ROOT / "DESIGN.md").read_text(encoding="utf-8")
        kinds = next(l for l in design.splitlines() if l.startswith("- `notify.events` lists"))
        self.assertIn("`upstream`", kinds)


class PageTest(unittest.TestCase):
    def setUp(self):
        self.page = dashboard.PAGE.read_text(encoding="utf-8")
        text = re.search(r'<script type="application/json" id="text">(.*?)</script>', self.page, re.DOTALL)
        self.text = json.loads(text.group(1))

    def test_it_says_each_state_in_both_languages(self):
        en = self.text["en"]
        self.assertEqual(en["upstream_up_to_date"], "Up to date")
        self.assertEqual(en["upstream_behind"], "{n} behind")
        self.assertEqual(en["upstream_ahead"], "{n} ahead")
        self.assertEqual(en["upstream_failed"], "Could not check")
        self.assertEqual(en["upstream_check_now"], "Check now")
        self.assertEqual(en["upstream_bring_in"], "Bring in updates")
        for key in ("upstream_up_to_date", "upstream_behind", "upstream_ahead", "upstream_failed", "upstream_check_now",
                    "upstream_bring_in", "upstream_task_open", "upstream_last_good"):
            self.assertTrue(self.text["fa"][key], key)

    def test_its_buttons_go_through_the_token(self):
        card = self.page.split("function upstreamCard(")[1].split("\n}\n")[0]
        self.assertIn("data-upstream-check", card)
        self.assertIn("data-upstream-bring", card)
        self.assertIn('act(hit("upstream-check"), "/upstream/check")', self.page)
        self.assertIn('act(hit("upstream-bring"), "/upstream/bring-in"', self.page)

    def test_bring_in_answers_from_the_server_and_redraws_the_card(self):
        handler = next(l for l in self.page.splitlines() if 'act(hit("upstream-bring")' in l)
        self.assertIn("created", handler)
        self.assertNotIn("lastState", handler)
        self.assertIn("refreshState()", handler)


if __name__ == "__main__":
    unittest.main()
