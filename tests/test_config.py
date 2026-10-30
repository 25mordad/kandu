import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from kandu.config import ConfigError, load_config

EXAMPLES = Path(__file__).resolve().parent.parent / "config.example"


class ConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        shutil.copy(EXAMPLES / "kandu.example.yaml", self.local / "kandu.yaml")
        shutil.copy(EXAMPLES / "projects.example.yaml", self.local / "projects.yaml")

    def edit(self, filename, change):
        path = self.local / filename
        data = yaml.safe_load(path.read_text())
        change(data)
        path.write_text(yaml.safe_dump(data))

    def problems(self):
        with self.assertRaises(ConfigError) as caught:
            load_config(self.local)
        return "\n".join(caught.exception.problems)

    def test_examples_are_valid(self):
        config = load_config(self.local)
        self.assertEqual(config.settings.brain.machine, "server")
        self.assertEqual([p.name for p in config.projects], ["website", "internal-tool"])
        self.assertEqual(config.projects[0].board.number, 1)
        self.assertEqual(config.settings.didban.knowledge[0].name, "notes")

    def test_empty_registry_is_valid(self):
        self.edit("projects.yaml", lambda d: d.update(projects=[]))
        self.assertEqual(load_config(self.local).projects, [])

    def test_didban_section_is_optional(self):
        self.edit("kandu.yaml", lambda d: d.pop("didban"))
        self.assertEqual(load_config(self.local).settings.didban.knowledge, [])

    def test_missing_file(self):
        (self.local / "projects.yaml").unlink()
        self.assertIn("file not found", self.problems())

    def test_invalid_yaml(self):
        (self.local / "kandu.yaml").write_text("brain: [")
        self.assertIn("invalid YAML", self.problems())

    def test_run_location_is_never_defaulted(self):
        self.edit("projects.yaml", lambda d: d["projects"][0].pop("run"))
        self.assertIn("projects[0].run: missing", self.problems())

    def test_invalid_run(self):
        self.edit("projects.yaml", lambda d: d["projects"][0].update(run="anywhere"))
        self.assertIn("must be one of clone, in-place", self.problems())

    def test_clone_must_be_on_the_brain(self):
        self.edit("projects.yaml", lambda d: d["projects"][0].update(machine="laptop"))
        self.assertIn("run: clone must be on the brain's machine", self.problems())

    def test_unknown_machine(self):
        self.edit("projects.yaml", lambda d: d["projects"][1].update(machine="ghost"))
        self.assertIn("unknown machine 'ghost'", self.problems())

    def test_brain_must_be_a_known_machine(self):
        self.edit("kandu.yaml", lambda d: d["brain"].update(machine="ghost"))
        self.assertIn("brain.machine: unknown machine", self.problems())

    def test_brain_must_be_local(self):
        self.edit("kandu.yaml", lambda d: d["brain"].update(machine="laptop"))
        self.assertIn("must have reach: local", self.problems())

    def test_didban_source_needs_a_known_machine(self):
        self.edit("kandu.yaml", lambda d: d["didban"]["knowledge"][0].update(machine="ghost"))
        self.assertIn("didban.knowledge[0].machine", self.problems())

    def test_unknown_key_catches_typos(self):
        self.edit("kandu.yaml", lambda d: d["supervisor"].update(auto_merg=True))
        self.assertIn("supervisor.auto_merg: unknown key", self.problems())

    def test_wrong_type(self):
        self.edit("kandu.yaml", lambda d: d["dashboard"].update(port="8790"))
        self.assertIn("dashboard.port: must be int", self.problems())

    def test_bool_is_not_an_int(self):
        self.edit("kandu.yaml", lambda d: d["supervisor"].update(max_parallel_workers=True))
        self.assertIn("supervisor.max_parallel_workers: must be int", self.problems())

    def test_percent_range(self):
        self.edit("kandu.yaml", lambda d: d["claude"]["limits"].update(weekly_percent=120))
        self.assertIn("weekly_percent: must be between 1 and 100", self.problems())

    def test_duplicate_project_name(self):
        self.edit("projects.yaml", lambda d: d["projects"][1].update(name="website"))
        self.assertIn("duplicate name", self.problems())

    def test_telegram_section_required_for_telegram(self):
        self.edit("kandu.yaml", lambda d: d["notify"].pop("telegram"))
        self.assertIn("notify.telegram: required", self.problems())

    def test_all_problems_are_reported_together(self):
        self.edit("kandu.yaml", lambda d: d["claude"].update(auth="magic"))
        self.edit("projects.yaml", lambda d: d["projects"][1].update(machine="ghost"))
        problems = self.problems()
        self.assertIn("claude.auth", problems)
        self.assertIn("unknown machine 'ghost'", problems)

    def test_project_auto_merge_overrides_supervisor(self):
        self.edit("kandu.yaml", lambda d: d["supervisor"].update(auto_merge=True))
        config = load_config(self.local)
        self.assertFalse(config.auto_merge_for(config.projects[0]))
        self.assertTrue(config.auto_merge_for(config.projects[1]))

    def test_machine_lookup(self):
        config = load_config(self.local)
        self.assertFalse(config.machine("laptop").always_on)


if __name__ == "__main__":
    unittest.main()
