"""A machine's run temporary folder (#120): off by default, set per machine in the config, by `kandu.add` and from the
dashboard. What a run sees with it on is in test_runner.RunFolderTest."""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from kandu import add
from kandu.config import ConfigError, load_config, set_run_tmp
from kandu.dashboard import Api, dispatch
from kandu.state import StateStore

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "secret-token"


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.local = self.root / "local"
        self.local.mkdir()
        shutil.copy(ROOT / "config.example" / "kandu.example.yaml", self.local / "kandu.yaml")
        shutil.copy(ROOT / "config.example" / "projects.example.yaml", self.local / "projects.yaml")

    def machine(self, name):
        return load_config(self.local).machine(name)

    def kandu_yaml(self):
        return (self.local / "kandu.yaml").read_text()

    def write(self, text):
        (self.local / "kandu.yaml").write_text(text)


class ConfigTest(Base):
    def test_the_example_shows_it_off_with_a_generic_folder(self):
        laptop = self.machine("laptop")
        self.assertFalse(laptop.run_tmp)
        self.assertEqual(laptop.run_tmp_dir, "~/kandu-tmp")

    def test_a_machine_that_does_not_name_it_has_it_off(self):
        server = self.machine("server")
        self.assertFalse(server.run_tmp)
        self.assertIsNone(server.run_tmp_dir)

    def test_on_needs_a_folder_and_the_folder_must_be_a_real_one(self):
        text = self.kandu_yaml()
        cases = (("    always_on: true\n", "    always_on: true\n    run_tmp: true\n"),
                 ("    run_tmp_dir: ~/kandu-tmp", "    run_tmp_dir: relative/tmp"),
                 ("    run_tmp_dir: ~/kandu-tmp", "    run_tmp_dir: /"),
                 ("    run_tmp_dir: ~/kandu-tmp", '    run_tmp_dir: "~/"'),
                 ("    run_tmp_dir: ~/kandu-tmp", "    run_tmp_dir: ~/a/../b"),
                 ("    run_tmp_dir: ~/kandu-tmp", '    run_tmp_dir: "~/a b"'))
        for old, new in cases:
            with self.subTest(new=new):
                self.write(text.replace(old, new, 1))
                with self.assertRaises(ConfigError) as e:
                    load_config(self.local)
                self.assertTrue(any("run_tmp_dir" in p for p in e.exception.problems), e.exception.problems)

    def test_set_run_tmp_turns_it_on_and_sets_the_folder_of_that_machine_only(self):
        set_run_tmp("server", True, "/data/kandu-tmp", self.local)
        server, laptop = self.machine("server"), self.machine("laptop")
        self.assertEqual((server.run_tmp, server.run_tmp_dir), (True, "/data/kandu-tmp"))
        self.assertEqual((laptop.run_tmp, laptop.run_tmp_dir), (False, "~/kandu-tmp"))
        self.assertIn("# SSH alias from ~/.ssh/config", self.kandu_yaml())
        set_run_tmp("laptop", True, None, self.local)
        laptop = self.machine("laptop")
        self.assertEqual((laptop.run_tmp, laptop.run_tmp_dir), (True, "~/kandu-tmp"))
        set_run_tmp("server", False, None, self.local)
        self.assertEqual(self.machine("server").run_tmp_dir, "/data/kandu-tmp")
        self.assertFalse(self.machine("server").run_tmp)

    def test_a_machine_named_like_a_knowledge_source_changes_only_the_machine(self):
        set_run_tmp("server", True, "~/t", self.local)
        knowledge = yaml.safe_load(self.kandu_yaml())["didban"]["knowledge"]
        self.assertEqual(knowledge, [{"name": "notes", "machine": "server", "path": "~/notes"}])

    def test_nothing_is_written_when_a_value_is_wrong(self):
        before = self.kandu_yaml()
        for machine, enabled, folder in (("server", True, None), ("server", True, "tmp"), ("nowhere", True, "~/t")):
            with self.subTest(machine=machine, folder=folder), self.assertRaises(ConfigError):
                set_run_tmp(machine, enabled, folder, self.local)
        self.assertEqual(self.kandu_yaml(), before)


class AddMachineTest(Base):
    def test_a_new_machine_has_it_off_unless_a_folder_is_given(self):
        add.add_machine("desk", "desk", False, self.local, probe=lambda machine: True)
        self.assertFalse(self.machine("desk").run_tmp)
        add.add_machine("studio", "studio", False, self.local, probe=lambda machine: True, run_tmp_dir="~/kandu-tmp")
        studio = self.machine("studio")
        self.assertEqual((studio.run_tmp, studio.run_tmp_dir), (True, "~/kandu-tmp"))

    def test_a_bad_folder_adds_nothing(self):
        before = self.kandu_yaml()
        with self.assertRaises(add.AddError):
            add.add_machine("desk", "desk", False, self.local, probe=lambda machine: True, run_tmp_dir="tmp")
        self.assertEqual(self.kandu_yaml(), before)


class ApiTest(Base):
    def setUp(self):
        super().setUp()
        self.api = Api(load_config(self.local), StateStore(self.root / "state"), mock.Mock(), self.root / "logs",
                       TOKEN, timer_on=lambda: True, local_dir=self.local)

    def post(self, target, token=TOKEN):
        return dispatch(self.api, "POST", target, token)

    def machines(self):
        return {m["name"]: m for m in dispatch(self.api, "GET", "/api/v1/state")[1]["machines"]}

    def test_state_says_each_machine_has_it_off(self):
        machines = self.machines()
        self.assertEqual((machines["server"]["run_tmp"], machines["server"]["run_tmp_dir"]), (False, None))
        self.assertEqual((machines["laptop"]["run_tmp"], machines["laptop"]["run_tmp_dir"]), (False, "~/kandu-tmp"))

    def test_the_owner_turns_it_on_and_sets_the_folder(self):
        status, body = self.post("/api/v1/machines/server/run-tmp?enabled=true&folder=%2Fdata%2Fkandu-tmp")
        self.assertEqual(status, 200)
        self.assertEqual((body["run_tmp"], body["run_tmp_dir"]), (True, "/data/kandu-tmp"))
        self.assertEqual(self.machines()["server"]["run_tmp"], True)
        server = self.machine("server")
        self.assertEqual((server.run_tmp, server.run_tmp_dir), (True, "/data/kandu-tmp"))
        event = self.api.store.last_event("run_tmp_changed")
        self.assertEqual((event["machine"], event["enabled"], event["folder"]), ("server", True, "/data/kandu-tmp"))
        status, body = self.post("/api/v1/machines/server/run-tmp?enabled=false")
        self.assertEqual((status, body["run_tmp"], body["run_tmp_dir"]), (200, False, "/data/kandu-tmp"))
        self.assertFalse(self.machine("server").run_tmp)

    def test_a_wrong_request_changes_nothing(self):
        before = self.kandu_yaml()
        self.assertEqual(self.post("/api/v1/machines/server/run-tmp?enabled=true&folder=~/t", None)[0], 403)
        self.assertEqual(self.post("/api/v1/machines/nowhere/run-tmp?enabled=true&folder=~/t")[0], 404)
        self.assertEqual(self.post("/api/v1/machines/server/run-tmp?enabled=maybe")[0], 400)
        status, body = self.post("/api/v1/machines/server/run-tmp?enabled=true")
        self.assertEqual(status, 400)
        self.assertIn("run_tmp_dir", body["error"])
        self.assertEqual(self.kandu_yaml(), before)

    def test_a_machine_added_from_the_dashboard_may_have_it_on(self):
        with mock.patch("kandu.add._answers", return_value=True):
            status, _ = self.post("/api/v1/machines?name=desk&ssh=desk&always_on=false&run_tmp_dir=~/kandu-tmp")
        self.assertEqual(status, 200)
        self.assertEqual((self.machines()["desk"]["run_tmp"], self.machines()["desk"]["run_tmp_dir"]),
                         (True, "~/kandu-tmp"))


if __name__ == "__main__":
    unittest.main()
