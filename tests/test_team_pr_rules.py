"""A team repo's PR test output: a `pr_test_output` section Kandu fills from its own test run (DESIGN §4)."""
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from kandu.config import ConfigError, load_config
from kandu.runner import RunnerError
from tests.test_supervisor import REPO, SHA1, Base, report

ROOT = Path(__file__).resolve().parent.parent
HEADING = "Local test output"


class TestOutputTest(Base):
    def setUp(self):
        super().setUp()
        self.config.projects[0].pr_test_output = HEADING
        self.github.add(1)
        self.tail = "Ran 3 tests in 0.1s\nOK"
        self.runner.log_tail = lambda log, lines: self.tail
        self.test = self.config.projects[0].test

    def opened_body(self):
        self.cycle()
        return self.github.opened[0][4]

    def test_the_pr_body_holds_kandus_own_test_run(self):
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]
        body = self.opened_body()
        self.assertIn(f"## {HEADING}\n`{self.test}` on this branch passed; the last lines:\n\n```\n"
                      "Ran 3 tests in 0.1s\nOK\n```\n", body)
        self.assertIn("Closes #1", body)
        self.assertIn((self.test, False), self.runner.commands)
        self.assertEqual(self.events("pr_test_output")[0]["code"], 0)

    def test_the_workers_own_text_in_the_section_is_replaced(self):
        def pushes():
            self.github.pushed[(REPO, 1)] = SHA1
            return ('{"status": "pushed", "pr_title": "Task", '
                    '"pr_body": "Does it.\\n\\n## Local test output\\nall fine, trust me\\n"}')
        self.runner.steps = [pushes, report(SHA1)]
        body = self.opened_body()
        self.assertNotIn("trust me", body)
        self.assertEqual(body.count(f"## {HEADING}"), 1)

    def test_a_failed_run_says_so(self):
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]
        self.runner.failing = {self.test}
        self.assertIn("failed with exit code 1", self.opened_body())

    def test_a_run_that_cannot_start_says_why(self):
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]

        def broken(*args):
            raise RunnerError("no checkout")
        self.runner.prepare_review = broken
        body = self.opened_body()
        self.assertIn(f"## {HEADING}\nKandu could not run `{self.test}` on this branch: no checkout", body)
        self.assertEqual(len(self.decisions("pr_test_output_not_run")), 1)

    def test_on_a_public_repo_a_line_with_a_private_word_is_left_out(self):
        (self.local / "private-words.txt").write_text("SecretName\n", encoding="utf-8")
        self.github.repo_is_public = lambda repo: True
        self.tail = "/home/secretname/site/test_a.py ok\nRan 1 test\nOK"
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]
        body = self.opened_body()
        self.assertNotIn("secretname", body.lower())
        self.assertIn("Ran 1 test\nOK", body)

    def test_without_the_setting_no_test_runs_before_the_pr(self):
        self.config.projects[0].pr_test_output = ""
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]
        self.assertEqual(self.opened_body(), "Does the `task`.\n\nCloses #1\n")
        self.assertEqual(self.events("pr_test_output"), [])
        self.assertNotIn("section from its own test run", self.runner.runs[0].prompt)

    def test_the_worker_is_told_to_leave_the_section_out(self):
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]
        self.cycle()
        self.assertIn(f"`## {HEADING}` section from its own test run", self.runner.runs[0].prompt)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        shutil.copy(ROOT / "config.example" / "kandu.example.yaml", self.local / "kandu.yaml")
        shutil.copy(ROOT / "config.example" / "projects.example.yaml", self.local / "projects.yaml")

    def project(self, **values):
        path = self.local / "projects.yaml"
        data = yaml.safe_load(path.read_text())
        data["projects"][0].update(values)
        path.write_text(yaml.safe_dump(data))

    def problems(self):
        with self.assertRaises(ConfigError) as caught:
            load_config(self.local)
        return caught.exception.problems

    def test_both_are_off_by_default_and_in_the_example(self):
        project = load_config(self.local).projects[0]
        self.assertEqual(project.pr_test_output, "")
        example = (ROOT / "config.example" / "projects.example.yaml").read_text(encoding="utf-8")
        self.assertIn('pr_test_output: "" ', example)

    def test_a_team_repo_sets_them(self):
        self.project(pr_test_output=HEADING)
        self.assertEqual(load_config(self.local).projects[0].pr_test_output, HEADING)

    def test_a_heading_with_a_mark_or_on_two_lines_is_a_problem(self):
        self.project(pr_test_output="## Local test output")
        self.assertTrue(any("pr_test_output: must be a heading's words" in p for p in self.problems()))
        self.project(pr_test_output="Local\ntest")
        self.assertTrue(any("pr_test_output" in p for p in self.problems()))

    def test_kandu_add_asks_about_them(self):
        add = (ROOT / ".claude" / "commands" / "kandu-add.md").read_text(encoding="utf-8")
        self.assertIn("`pr_test_output`", add)


if __name__ == "__main__":
    unittest.main()
