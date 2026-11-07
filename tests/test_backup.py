import contextlib
import fcntl
import filecmp
import io
import subprocess
import sys
import os
import re
import shutil
import tempfile
import unittest
from datetime import datetime
from functools import partial
from pathlib import Path
from unittest import mock

from kandu import backup
from kandu.config import ConfigError, load_config, set_backup
from kandu.dashboard import Api, dispatch
from kandu.notify import Notifier
from kandu.state import StateStore

ROOT = Path(__file__).resolve().parent.parent
SECRET = "s3cr3t-value-never-shown"


def at(day, clock="03:30:00"):
    return datetime.fromisoformat(f"{day}T{clock}").astimezone()


def same_tree(test, left, right):
    """Every file and folder in both, with the same bytes."""
    compared = filecmp.dircmp(left, right)
    test.assertEqual((compared.left_only, compared.right_only, compared.funny_files), ([], [], []), left)
    _, mismatch, errors = filecmp.cmpfiles(left, right, compared.common_files, shallow=False)
    test.assertEqual((mismatch, errors), ([], []), left)
    for folder in compared.common_dirs:
        same_tree(test, Path(left) / folder, Path(right) / folder)


class Sent:
    def __init__(self):
        self.texts = []

    def send(self, text):
        self.texts.append(text)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "install"
        local, state = self.root / "local", self.root / "state"
        local.mkdir(parents=True)
        shutil.copy(ROOT / "config.example" / "kandu.example.yaml", local / "kandu.yaml")
        shutil.copy(ROOT / "config.example" / "projects.example.yaml", local / "projects.yaml")
        (local / ".env").write_text(f"KANDU_DASHBOARD_TOKEN={SECRET}\n", encoding="utf-8")
        (local / "reports" / "bazras" / "website").mkdir(parents=True)
        (local / "reports" / "bazras" / "website" / "2026-11-01.md").write_text("all well\n", encoding="utf-8")
        (local / "empty").mkdir()
        self.store = StateStore(state)
        self.store.record("supervisor_woke", now=at("2026-11-01"))
        (state / "mirror").mkdir()
        (state / "mirror" / "issues.json").write_bytes(b"\x00\x01binary")
        (self.root / "logs").mkdir()
        (self.root / "logs" / "run-1.jsonl").write_text("{}\n", encoding="utf-8")
        self.destination = self.tmp / "backups"

    def make(self, keep=3, day="2026-11-07", clock="03:30:00"):
        return backup.make(self.root, self.destination, keep, now=at(day, clock))

    def config(self, enabled=True, keep=3, clock="03:00"):
        config = load_config(self.root / "local")
        config.settings.backup.enabled = enabled
        config.settings.backup.at = clock
        config.settings.backup.destination = str(self.destination)
        config.settings.backup.keep = keep
        return config


class MakeAndRestoreTest(Base):
    def test_a_backup_restores_to_the_same_files(self):
        made = self.make()
        target = self.tmp / "restored"
        target.mkdir()
        backup.restore(made.copy, target)
        same_tree(self, self.root / "local", target / "local")
        same_tree(self, self.root / "state", target / "state")

    def test_the_run_logs_are_left_out(self):
        target = self.tmp / "restored"
        backup.restore(self.make().copy, target)
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["local", "state"])

    def test_the_copy_is_dated_and_only_its_owner_reads_it(self):
        made = self.make(day="2026-11-07")
        self.assertTrue(made.copy.name.startswith("kandu-2026-11-07"))
        self.assertEqual(made.copy.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o700)

    def test_restore_needs_an_empty_folder(self):
        made = self.make()
        target = self.tmp / "full"
        target.mkdir()
        (target / "mine.txt").write_text("keep me", encoding="utf-8")
        with self.assertRaises(backup.BackupError):
            backup.restore(made.copy, target)
        self.assertEqual([p.name for p in target.iterdir()], ["mine.txt"])

    def test_a_destination_inside_what_it_backs_up_is_refused(self):
        with self.assertRaises(backup.BackupError):
            backup.make(self.root, self.root / "state" / "backups", 3, now=at("2026-11-07"))


class KeepTest(Base):
    def test_only_the_number_kept_stays_and_the_oldest_go(self):
        for day in ("2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04"):
            self.make(keep=2, day=day)
        self.assertEqual([p.name[:16] for p in backup.copies(self.destination)],
                         ["kandu-2026-11-03", "kandu-2026-11-04"])

    def test_keeping_one_never_removes_the_newest(self):
        self.make(keep=1, day="2026-11-01")
        made = self.make(keep=1, day="2026-11-02")
        self.assertEqual(backup.copies(self.destination), [made.copy])

    def test_other_files_in_the_destination_are_not_touched(self):
        self.destination.mkdir()
        (self.destination / "notes.txt").write_text("mine", encoding="utf-8")
        for day in ("2026-11-01", "2026-11-02"):
            self.make(keep=1, day=day)
        self.assertTrue((self.destination / "notes.txt").exists())


class FailureTest(Base):
    def setUp(self):
        super().setUp()
        if os.geteuid() == 0:
            self.skipTest("root reads every file")
        self.notices = Sent()
        self.store.listeners.append(Notifier(self.store, self.notices, ["failure"], "en").on_event)
        self.unreadable = self.root / "state" / "mirror" / "issues.json"

    def break_a_file(self):
        self.unreadable.chmod(0)
        self.addCleanup(self.unreadable.chmod, 0o600)

    def test_a_partial_backup_leaves_the_previous_copies_untouched(self):
        first = self.make(day="2026-11-01").copy
        second = self.make(day="2026-11-02").copy
        before = {p: p.read_bytes() for p in (first, second)}
        self.break_a_file()
        with self.assertRaises(backup.BackupError):
            self.make(keep=1, day="2026-11-03")
        self.assertEqual(backup.copies(self.destination), sorted(before))
        self.assertEqual([p for p in self.destination.iterdir() if p.name.endswith(".partial")], [])
        self.assertEqual({p: p.read_bytes() for p in (first, second)}, before)

    def test_a_failed_backup_is_logged_and_told_without_a_secret(self):
        (self.root / "local" / ".env").chmod(0)
        self.addCleanup((self.root / "local" / ".env").chmod, 0o600)
        self.assertIsNone(backup.run(self.config(), self.store, self.root, now=at("2026-11-07")))
        failed = self.store.last_event("backup_failed")
        self.assertIsNotNone(failed)
        self.assertEqual(len(self.notices.texts), 1)
        self.assertIn("backup", self.notices.texts[0])
        log = "".join(p.read_text(encoding="utf-8") for p in self.store.events_dir.iterdir())
        self.assertNotIn(SECRET, log + self.notices.texts[0])

    def test_a_failure_is_told_once_a_day(self):
        self.break_a_file()
        for clock in ("03:00:00", "03:15:00"):
            backup.run(self.config(), self.store, self.root, now=at("2026-11-07", clock))
        self.assertEqual(len(self.notices.texts), 1)


class RunTest(Base):
    def test_off_by_default_it_does_nothing(self):
        config = load_config(self.root / "local")
        self.assertFalse(config.settings.backup.enabled)
        self.assertIsNone(backup.run(config, self.store, self.root, now=at("2026-11-07", "23:00:00")))
        self.assertFalse(self.destination.exists())

    def test_it_waits_for_its_hour_and_runs_once_a_day(self):
        config = self.config(clock="03:00")
        self.assertIsNone(backup.run(config, self.store, self.root, now=at("2026-11-07", "02:59:00")))
        made = backup.run(config, self.store, self.root, now=at("2026-11-07", "03:00:00"))
        self.assertIsNotNone(made)
        self.assertIsNone(backup.run(config, self.store, self.root, now=at("2026-11-07", "09:00:00")))
        made = backup.run(config, self.store, self.root, now=at("2026-11-08", "04:00:00"))
        self.assertIsNotNone(made)
        self.assertEqual(self.store.last_event("backup_made")["copy"], made.copy.name)

    def test_by_hand_it_runs_into_another_destination_while_off(self):
        other = self.tmp / "by-hand"
        made = backup.run(self.config(enabled=False), self.store, self.root, now=at("2026-11-07", "01:00:00"),
                          force=True, destination=other)
        self.assertEqual(made.copy.parent, other)


if __name__ == "__main__":
    unittest.main()


class ConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        shutil.copy(ROOT / "config.example" / "kandu.example.yaml", self.local / "kandu.yaml")
        shutil.copy(ROOT / "config.example" / "projects.example.yaml", self.local / "projects.yaml")
        self.path = self.local / "kandu.yaml"

    def without_backup(self):
        return re.sub(r"\nbackup:.*?\n\n", "\n", self.path.read_text(encoding="utf-8"), flags=re.DOTALL)

    def problems(self, block):
        self.path.write_text(self.without_backup() + "\n" + block + "\n", encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
            load_config(self.local)
        return "\n".join(caught.exception.problems)

    def test_the_example_has_a_backup_and_it_is_off(self):
        settings = load_config(self.local).settings.backup
        self.assertFalse(settings.enabled)
        self.assertIn("\nbackup:", self.path.read_text(encoding="utf-8"))
        self.assertTrue(settings.destination.startswith("~/"))

    def test_without_a_backup_block_it_is_off(self):
        self.path.write_text(self.without_backup(), encoding="utf-8")
        self.assertFalse(load_config(self.local).settings.backup.enabled)

    def test_wrong_values_are_named(self):
        self.assertIn("backup.at", self.problems('backup:\n  at: "3am"'))
        self.assertIn("backup.keep", self.problems("backup:\n  keep: 0"))
        self.assertIn("backup.destination", self.problems("backup:\n  enabled: true\n  destination: \"\""))
        self.assertIn("backup.destination", self.problems("backup:\n  destination: backups"))

    def test_set_backup_writes_each_value_and_keeps_comments(self):
        set_backup(True, "04:15", "/srv/kandu backups", 5, self.local)
        settings = load_config(self.local).settings.backup
        self.assertEqual((settings.enabled, settings.at, settings.destination, settings.keep),
                         (True, "04:15", "/srv/kandu backups", 5))
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# stop when weekly usage", text)
        set_backup(False, None, None, None, self.local)
        self.assertEqual(load_config(self.local).settings.backup.at, "04:15")

    def test_set_backup_adds_a_missing_block(self):
        self.path.write_text(self.without_backup(), encoding="utf-8")
        set_backup(True, "02:00", "~/kandu-backups", 3, self.local)
        self.assertTrue(load_config(self.local).settings.backup.enabled)

    def test_a_wrong_value_writes_nothing(self):
        before = self.path.read_text(encoding="utf-8")
        for values in ((True, "25:00", None, None), (True, None, None, 0), (True, None, "relative", None),
                       (True, None, "/a\nb", None)):
            with self.assertRaises(ConfigError):
                set_backup(*values, self.local)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)


class DashboardTest(Base):
    def setUp(self):
        super().setUp()
        self.turned_on = []
        self.api = Api(load_config(self.root / "local"), self.store, mock.Mock(), self.root / "logs", "token",
                       timer_on=lambda: True, local_dir=self.root / "local", backup_timer=self.turned_on.append)

    def post(self, query, token="token"):
        return dispatch(self.api, "POST", f"/api/v1/backup{query}", token)

    def test_state_says_the_backup_is_off(self):
        shown = dispatch(self.api, "GET", "/api/v1/state")[1]["backup"]
        self.assertEqual((shown["enabled"], shown["copies"], shown["last"]), (False, [], None))

    def test_it_is_turned_on_with_its_hour_destination_and_copies(self):
        query = f"?enabled=true&at=04:30&keep=5&destination={self.destination}"
        status, body = self.post(query)
        self.assertEqual(status, 200)
        self.assertEqual((body["enabled"], body["at"], body["keep"], body["destination"]),
                         (True, "04:30", 5, str(self.destination)))
        settings = load_config(self.root / "local").settings.backup
        self.assertEqual((settings.enabled, settings.at, settings.keep), (True, "04:30", 5))
        self.assertEqual(len(self.turned_on), 1)
        self.assertTrue(self.store.last_event("backup_changed")["enabled"])

    def test_turning_it_off_or_a_wrong_value_starts_no_timer(self):
        self.assertEqual(self.post("?enabled=false")[0], 200)
        before = (self.root / "local" / "kandu.yaml").read_text(encoding="utf-8")
        for query in ("?enabled=maybe", "?enabled=true&at=99:99", "?enabled=true&keep=0", "?enabled=true&keep=a"):
            self.assertEqual(self.post(query)[0], 400, query)
        self.assertEqual((self.root / "local" / "kandu.yaml").read_text(encoding="utf-8"), before)
        self.assertEqual(self.turned_on, [])
        self.assertEqual(self.post("?enabled=true", "wrong")[0], 403)

    def test_state_lists_the_copies_and_the_last_result(self):
        self.post(f"?enabled=true&destination={self.destination}")
        self.make()
        backup.run(self.api.config, self.store, self.root, now=at("2026-11-08", "23:00:00"))
        shown = dispatch(self.api, "GET", "/api/v1/state")[1]["backup"]
        self.assertEqual(len(shown["copies"]), 2)
        self.assertTrue(shown["copies"][0].startswith("kandu-2026-11-08"))
        self.assertEqual(shown["last"]["type"], "backup_made")

    def test_a_timer_systemd_cannot_start_is_a_500_and_the_setting_is_saved(self):
        def refuse(config):
            raise subprocess.CalledProcessError(1, ["systemctl"])
        self.api.backup_timer = refuse
        status, body = self.post(f"?enabled=true&destination={self.destination}")
        self.assertEqual(status, 500)
        self.assertIn("timer", str(body))
        self.assertTrue(load_config(self.root / "local").settings.backup.enabled)


class GuardTest(Base):
    def test_a_second_backup_while_one_runs_is_refused(self):
        self.destination.mkdir()
        with open(self.destination / backup.LOCK, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(backup.BackupError) as caught:
                self.make()
        self.assertIn("another backup", str(caught.exception))
        self.assertEqual(backup.copies(self.destination), [])

    def test_a_copy_that_does_not_read_back_the_same_is_not_kept(self):
        first = self.make(day="2026-11-01").copy
        write = backup._write

        def write_and_misremember(root, partial):
            held = write(root, partial)
            held["local/kandu.yaml"] += 1
            return held

        with mock.patch.object(backup, "_write", write_and_misremember):
            with self.assertRaises(backup.BackupError) as caught:
                self.make(keep=1, day="2026-11-02")
        self.assertIn("read back", str(caught.exception))
        self.assertEqual(backup.copies(self.destination), [first])
        self.assertEqual([p for p in self.destination.iterdir() if p.name.endswith(".partial")], [])

    def test_a_crash_leftover_is_removed(self):
        self.destination.mkdir()
        leftover = self.destination / "kandu-2026-11-06-033000.tar.gz.partial"
        leftover.write_bytes(b"half a copy")
        made = self.make()
        self.assertFalse(leftover.exists())
        self.assertEqual(backup.copies(self.destination), [made.copy])


class MainTest(Base):
    def setUp(self):
        super().setUp()
        set_backup(False, None, str(self.destination), None, self.root / "local")
        self.enterContext(mock.patch.object(backup, "StateStore", lambda: self.store))
        self.enterContext(mock.patch.object(backup, "run", partial(backup.run, root=self.root)))

    def main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        argv = ["backup", *args, "--local", str(self.root / "local")]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = backup.main()
        return code, out.getvalue(), err.getvalue()

    def test_now_makes_a_copy_even_when_off_and_list_shows_it(self):
        code, out, _ = self.main("now")
        self.assertEqual(code, 0)
        [copy] = backup.copies(self.destination)
        self.assertEqual(self.main("list"), (0, f"{copy}\n", ""))

    def test_a_failed_now_exits_1(self):
        code, _, err = self.main("now", "--destination", str(self.root / "state" / "backups"))
        self.assertEqual(code, 1)
        self.assertIn("failed", err)
        self.assertEqual(backup.copies(self.root / "state" / "backups"), [])

    def test_restore_needs_the_copy_and_a_folder(self):
        for paths in ((), ("one",), ("one", "two", "three")):
            code, _, err = self.main("restore", *paths)
            self.assertEqual(code, 1, paths)
            self.assertIn("restore needs", err)

    def test_restore_puts_a_copy_into_an_empty_folder(self):
        made = self.make()
        target = self.tmp / "restored"
        code, out, _ = self.main("restore", str(made.copy), str(target))
        self.assertEqual(code, 0)
        same_tree(self, self.root / "local", target / "local")

    def test_restore_refuses_a_full_folder(self):
        made = self.make()
        target = self.tmp / "full"
        target.mkdir()
        (target / "mine.txt").write_text("keep me", encoding="utf-8")
        code, _, err = self.main("restore", str(made.copy), str(target))
        self.assertEqual(code, 1)
        self.assertIn("must be empty", err)
