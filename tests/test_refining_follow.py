"""Didban and `/kandu-task` follow the refining way, and code holds grilling to its interview and its spec (DESIGN §2)."""
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kandu import intake, tasks
from kandu.state import StateStore
from tests.test_setup_commands import BODY, SPEC, project

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"
FINISHED = "✅ Login › Locked after five failures\n✂️ Password reset\n"


class Base(unittest.TestCase):
    way = "grilling"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        self.store = StateStore(self.local / "state")
        self.github = mock.Mock()
        self.github.repo_is_public.return_value = False
        self.github.create_issue.side_effect = lambda repo, *a, **k: \
            f"https://github.com/{repo}/issues/{10 + self.github.create_issue.call_count}"
        self.github.issue_text.return_value = {"title": "Spec", "body": SPEC, "url": "u"}
        self.github.issue_is_open.return_value = True
        self.github.send.return_value = (0, 0, 0)
        self.config = mock.Mock(projects=[project()])
        self.config.projects[0].name = "site"
        self.config.settings.refining.way = self.way

    def request(self, decision_map=FINISHED, project="site", text="Lock an account after failed logins"):
        request_id = intake.add(self.store, text)
        if decision_map is not None:
            intake.save_map(self.store, request_id, decision_map, project=project)
        return request_id

    def spec(self, request=None):
        return tasks.create_spec(self.config, self.github, "site", None, "A request", SPEC, self.local, self.store,
                                 request=request)


class GrillingSpecTest(Base):
    def test_a_spec_needs_the_request_it_comes_from(self):
        with self.assertRaisesRegex(tasks.TaskError, "under the grilling refining way a spec names the saved request"):
            self.spec()
        self.github.create_issue.assert_not_called()

    def test_a_request_that_is_not_saved_is_refused(self):
        with self.assertRaisesRegex(tasks.TaskError, "no saved request abc123"):
            self.spec("abc123")
        self.github.create_issue.assert_not_called()

    def test_an_interview_with_a_question_or_a_wait_left_is_refused_and_says_which(self):
        for left, said in (("❓ Login › How long is it locked", "1 ❓ and 0 ⏳"),
                           ("⏳ Login › Who unlocks it", "0 ❓ and 1 ⏳")):
            request = self.request(FINISHED + left + "\n", text=f"request {left}")
            with self.assertRaisesRegex(tasks.TaskError, re.escape(said)):
                self.spec(request)
        self.github.create_issue.assert_not_called()

    def test_a_request_with_no_saved_interview_or_no_decision_is_refused(self):
        with self.assertRaisesRegex(tasks.TaskError, "no ✅ decision"):
            self.spec(self.request(None))
        with self.assertRaisesRegex(tasks.TaskError, "no ✅ decision"):
            self.spec(self.request("✂️ Password reset\n", text="only out of scope"))
        self.github.create_issue.assert_not_called()

    def test_a_request_of_another_project_is_refused(self):
        with self.assertRaisesRegex(tasks.TaskError, "belongs to shop, not site"):
            self.spec(self.request(project="shop"))
        self.github.create_issue.assert_not_called()

    def test_a_finished_interview_makes_the_spec(self):
        url = self.spec(self.request())
        self.assertTrue(url.endswith("/issues/11"))
        self.assertEqual(tasks.specs(self.store), {"o/r#11"})


class RoundsSpecTest(Base):
    way = "rounds"

    def test_a_spec_needs_no_request(self):
        self.spec()
        self.github.create_issue.assert_called_once()

    def test_a_request_given_is_still_held_to_a_finished_interview(self):
        with self.assertRaisesRegex(tasks.TaskError, "not finished"):
            self.spec(self.request(FINISHED + "❓ Login › How long\n"))
        self.spec(self.request(text="another"))
        self.github.create_issue.assert_called_once()


class CommandBase(Base):
    def setUp(self):
        super().setUp()
        self.body = self.local / "body.md"
        self.body.write_text(BODY, encoding="utf-8")
        for target, value in (("load_config", self.config), ("open_github", self.github), ("StateStore", self.store)):
            patcher = mock.patch.object(tasks, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_command(self, *extra, fields=True):
        argv = ["tasks", "--project", "site", *(["--priority", "High", "--kind", "Bug", "--size", "Small"] if fields else []),
                *extra]
        with mock.patch("sys.argv", argv), mock.patch("sys.stdout") as out, mock.patch("sys.stderr") as err:
            code = tasks.main()
        text = lambda stream: "".join(c.args[0] for c in stream.write.call_args_list)
        return code, text(out), text(err)

    def new_task(self, *extra):
        return self.run_command("--title", "T", "--body-file", str(self.body), *extra)


class GrillingTaskCommandTest(CommandBase):
    def test_a_task_without_its_spec_is_refused(self):
        code, _, err = self.new_task()
        self.assertEqual(code, 1)
        self.assertIn("under the grilling refining way a task from refining names its spec", err)
        self.github.create_issue.assert_not_called()

    def test_a_task_of_a_spec_is_made(self):
        self.store.record("spec_created", task="o/r#4", project="site")
        code, _, _ = self.new_task("--spec", "#4")
        self.assertEqual(code, 0)
        self.github.create_issue.assert_called_once()

    def test_a_by_hand_task_and_placing_an_existing_issue_are_not_affected(self):
        self.assertEqual(self.new_task("--by-hand")[0], 0)
        self.assertEqual(self.run_command("--place", "https://github.com/o/r/issues/1")[0], 0)

    def test_the_command_passes_the_request_to_the_spec(self):
        self.body.write_text(SPEC, encoding="utf-8")
        args = ("--spec-issue", "--title", "S", "--body-file", str(self.body))
        code, _, err = self.run_command(*args, fields=False)
        self.assertEqual(code, 1)
        self.assertIn("names the saved request", err)
        code, out, _ = self.run_command(*args, "--request", self.request(), fields=False)
        self.assertEqual(code, 0)
        self.assertIn("/issues/", out)

    def test_request_goes_only_with_a_spec(self):
        with self.assertRaises(SystemExit):
            self.new_task("--request", "abc123")
        self.github.create_issue.assert_not_called()

    def test_the_tasks_code_makes_are_not_refining(self):
        # The queen, the splitter, Bring in updates and /kandu-add call create_task, never the command's check.
        url = tasks.create_task(self.config, self.github, "site", None, "T", BODY, self.local, "High", "Bug", "Small",
                                store=self.store)
        self.assertIn("/issues/", url)


class RoundsTaskCommandTest(CommandBase):
    way = "rounds"

    def test_a_task_without_a_spec_is_made_as_before(self):
        self.assertEqual(self.new_task()[0], 0)
        self.github.create_issue.assert_called_once()


class PromptsTest(unittest.TestCase):
    def read(self, path):
        return (ROOT / path).read_text(encoding="utf-8")

    def test_kandu_task_reads_the_way_and_takes_it(self):
        text = self.read(".claude/commands/kandu-task.md")
        self.assertIn("`.venv/bin/python -m kandu.config --refining-way` on the brain", text)
        self.assertLess(text.index("`grilling`"), text.index("`prompts/grilling.md`"))
        self.assertLess(text.index("`prompts/to-spec.md`"), text.index("`prompts/to-ticket.md`"))
        self.assertLess(text.index("`rounds`"), text.index("`prompts/defining.md`"))

    def test_the_two_commands_still_work_when_typed(self):
        self.assertIn("`prompts/grilling.md`", self.read(".claude/commands/kandu-grill-with-docs.md"))
        self.assertIn("`prompts/to-spec.md`", self.read(".claude/commands/kandu-to-issue.md"))
        self.assertNotIn("refining", self.read(".claude/commands/kandu-grill-with-docs.md"))
        self.assertNotIn("refining", self.read(".claude/commands/kandu-to-issue.md"))

    def test_didban_refines_requests_and_findings_the_installs_way(self):
        text = self.read("prompts/didban.md")
        way = text.split("## The refining way\n")[1].split("\n## ")[0]
        self.assertIn("`python -m kandu.config --refining-way` on the brain", way)
        for words in ("`rounds`: define the tasks as `prompts/defining.md` says", "`grilling`: follow `prompts/grilling.md`",
                      "`prompts/to-spec.md` and `prompts/to-ticket.md`"):
            self.assertIn(words, way)
        findings = text.split("## Bazras's findings\n")[1].split("\n## ")[0]
        request = text.split("## Taking a request\n")[1].split("\n## ")[0]
        for section in (findings, request):
            self.assertIn('the install\'s refining way, as "The refining way" below says', section)

    def test_to_issue_names_the_request_of_its_spec(self):
        self.assertIn("adding `--request ID` for the request of step 1", self.read("prompts/to-spec.md"))

    def test_claude_md_says_requests_become_tasks_only_with_kandu_task(self):
        self.assertIn("Turn a request into tasks only with `/kandu-task`, never by hand", self.read("CLAUDE.md"))


if __name__ == "__main__":
    unittest.main()
