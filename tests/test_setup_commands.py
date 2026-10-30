import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kandu import doctor, tasks
from kandu.boards import missing_columns
from kandu.config import BOARD_COLUMNS, BOARD_FIELDS, DEADLINE_FIELD, Board, Machine
from kandu.github import GitHub, GitHubError
from kandu.usage import Usage, UsageError

ROOT = Path(__file__).resolve().parent.parent
COMMANDS = ROOT / ".claude" / "commands"
BODY = "\n\n".join(f"## {s}\ntext" for s in tasks.required_sections())


def project(**changes):
    values = dict(name="site", repos=["o/r"], board=Board("o", 1))
    return mock.Mock(**(values | changes))


class TaskTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        self.github = mock.Mock()
        self.github.repo_is_public.return_value = False
        self.github.create_issue.return_value = "https://x/1"
        self.config = mock.Mock(projects=[project()])
        self.config.projects[0].name = "site"

    def create(self, name="site", repo=None, title="T", body=BODY, priority="High", kind="Bug", size="Small", deadline=None):
        return tasks.create_task(self.config, self.github, name, repo, title, body, self.local,
                                 priority, kind, size, deadline)

    def test_goes_on_the_board_in_backlog(self):
        self.assertEqual(self.create(), "https://x/1")
        self.github.add_to_board.assert_called_once()
        self.assertEqual(self.github.set_status.call_args.args[1:], ("https://x/1", "Backlog"))

    def test_fields_are_set_and_a_deadline_only_if_given(self):
        self.create()
        self.assertEqual([c.args[2:] for c in self.github.set_field.call_args_list],
                         [("Priority", "High"), ("Kind", "Bug"), ("Size", "Small")])
        self.github.set_field.reset_mock()
        self.create(deadline="2030-01-02")
        self.assertEqual(self.github.set_field.call_args.args[2:], ("Deadline", "2030-01-02"))

    def test_a_task_without_good_fields_is_not_created(self):
        for bad in ({"priority": ""}, {"kind": "Other"}, {"size": "Huge"}, {"deadline": "soon"}):
            with self.assertRaises(tasks.TaskError):
                self.create(**bad)
        self.github.create_issue.assert_not_called()

    def test_the_template_is_required(self):
        with self.assertRaisesRegex(tasks.TaskError, "Acceptance criteria"):
            self.create(body=BODY.replace("## Acceptance criteria", "## Other"))
        self.github.create_issue.assert_not_called()

    def test_unknown_project(self):
        with self.assertRaises(tasks.TaskError):
            self.create(name="other")

    def test_several_repos_must_be_named(self):
        self.config.projects[0].repos = ["o/a", "o/b"]
        with self.assertRaises(tasks.TaskError):
            self.create()
        self.create(repo="o/b")
        self.assertEqual(self.github.create_issue.call_args.args[0], "o/b")

    def test_a_repo_outside_the_project_is_refused(self):
        with self.assertRaises(tasks.TaskError):
            self.create(repo="x/y")

    def test_no_board(self):
        self.config.projects[0].board = None
        with self.assertRaises(tasks.TaskError):
            self.create()

    def test_a_private_word_cannot_reach_a_public_repo(self):
        (self.local / "private-words.txt").write_text("# note\nSecretName\n", encoding="utf-8")
        self.github.repo_is_public.return_value = True
        with self.assertRaises(tasks.TaskError) as caught:
            self.create(title="fix secretname page")
        self.assertNotIn("secretname", str(caught.exception).lower())
        self.github.create_issue.assert_not_called()

    def test_the_same_word_is_fine_on_a_private_repo(self):
        (self.local / "private-words.txt").write_text("SecretName\n", encoding="utf-8")
        self.create(title="fix SecretName page")
        self.github.create_issue.assert_called_once()


class GitHubBoardTest(unittest.TestCase):
    def test_column_names_are_mapped_both_ways(self):
        board = Board("o", 1, columns={"Ready": "To do"})
        reply = '{"items": [{"content": {"type": "Issue", "repository": "o/r", "number": 1, "title": "t", "url": "u"}, "status": "To do"}]}'
        run = mock.Mock(return_value=reply)
        github = GitHub(run)
        self.assertEqual(github.board_items(board)[0].status, "Ready")
        github.set_status(board, "u", "Ready")
        self.assertEqual(run.call_args.args[0][-1], "To do")

    def test_create_board_sets_the_columns(self):
        replies = ['{"number": 4}', '{"fields": [{"id": "F1", "name": "Status"}]}', "", "", "", "", ""]
        run = mock.Mock(side_effect=replies)
        self.assertEqual(GitHub(run).create_board("o", "T", BOARD_COLUMNS), 4)
        query = run.call_args_list[2].args[0][-1]
        self.assertIn('fieldId: "F1"', query)
        self.assertEqual(run.call_args.args[0][6], "Deadline")
        self.assertIn('name: "Needs info"', query)

    def test_board_columns(self):
        run = mock.Mock(return_value='{"fields": [{"name": "Status", "options": [{"name": "Ready"}]}]}')
        self.assertEqual(GitHub(run).board_columns(Board("o", 1)), ["Ready"])

    def test_missing_columns_respect_the_mapping(self):
        found = [c for c in BOARD_COLUMNS if c != "Ready"] + ["To do"]
        self.assertEqual(missing_columns(Board("o", 1), found), ["Ready"])
        self.assertEqual(missing_columns(Board("o", 1, columns={"Ready": "To do"}), found), [])


class DoctorTest(unittest.TestCase):
    def test_missing_tool(self):
        results = doctor.check_tools(which=lambda t: None if t == "gh" else "/bin/x")
        self.assertIn((doctor.FAIL, "tool gh"), results)

    def test_gh_scope(self):
        self.assertEqual(doctor.check_gh_scope("Token scopes: 'gist', 'project', 'repo'")[0], doctor.OK)
        self.assertEqual(doctor.check_gh_scope("Token scopes: 'repo'")[0], doctor.FAIL)

    def test_guard(self):
        with tempfile.TemporaryDirectory() as local:
            results = doctor.check_guard("", local)
        self.assertEqual([r[0] for r in results], [doctor.FAIL, doctor.WARN])

    def test_dashboard_token(self):
        with tempfile.TemporaryDirectory() as local, mock.patch.dict("os.environ", clear=True):
            self.assertEqual(doctor.check_dashboard_token(local)[0], doctor.WARN)
            (Path(local) / ".env").write_text("KANDU_DASHBOARD_TOKEN=\n", encoding="utf-8")
            self.assertEqual(doctor.check_dashboard_token(local)[0], doctor.WARN)
            (Path(local) / ".env").write_text("KANDU_DASHBOARD_TOKEN=abc\n", encoding="utf-8")
            self.assertEqual(doctor.check_dashboard_token(local)[0], doctor.OK)

    def test_usage(self):
        self.assertEqual(doctor.check_usage("subscription", lambda: Usage(1, 2))[0], doctor.OK)

        def broken():
            raise UsageError("x")
        self.assertEqual(doctor.check_usage("subscription", broken)[0], doctor.FAIL)
        self.assertEqual(doctor.check_usage("api_key", broken)[0], doctor.OK)

    def test_a_machine_that_may_sleep_is_only_a_warning(self):
        runner = mock.Mock()
        runner.reachable.return_value = False
        self.assertEqual(doctor.check_machine(Machine("a", "local", False), runner)[0], doctor.WARN)
        self.assertEqual(doctor.check_machine(Machine("a", "local", True), runner)[0], doctor.FAIL)

    def check_project(self, copy=True, run="clone", tool=True, columns=BOARD_COLUMNS, github_error=False,
                      fields=(*BOARD_FIELDS, DEADLINE_FIELD)):
        host = mock.Mock()
        host.absolute.side_effect = lambda p: p
        host.succeeds.side_effect = lambda command, cwd=None: copy if command[0] == "test" else tool
        runner = mock.Mock(hosts={"m": host})
        github = mock.Mock()
        github.board_columns.return_value = list(columns)
        github.board_fields.return_value = list(fields)
        if github_error:
            github.board_columns.side_effect = GitHubError("x")
        item = mock.Mock(path="~/p", repos=["o/r"], run=run, machine="m", test="npm test", board=Board("o", 1))
        item.name = "site"
        return doctor.check_project(item, runner, github)

    def test_healthy_project(self):
        self.assertEqual({r[0] for r in self.check_project()}, {doctor.OK})

    def test_clone_project_with_no_copy_is_fine(self):
        self.assertEqual({r[0] for r in self.check_project(copy=False)}, {doctor.OK})

    def test_in_place_project_with_no_copy_fails(self):
        self.assertIn(doctor.FAIL, {r[0] for r in self.check_project(copy=False, run="in-place")})

    def test_missing_fields_fail(self):
        self.assertTrue(any(level == doctor.FAIL and "Size" in text
                            for level, text in self.check_project(fields=["Priority", "Kind", "Deadline"])))

    def test_missing_columns_fail(self):
        results = self.check_project(columns=["Ready"])
        self.assertTrue(any(level == doctor.FAIL and "Blocked" in text for level, text in results))

    def test_unreadable_board_fails(self):
        self.assertIn(doctor.FAIL, {r[0] for r in self.check_project(github_error=True)})

    def test_missing_test_tool_warns(self):
        self.assertIn(doctor.WARN, {r[0] for r in self.check_project(tool=False)})


class CommandsTest(unittest.TestCase):
    def test_every_command_exists_and_reads_the_shared_rules(self):
        for name in ("setup", "add", "doctor", "task", "config"):
            text = (COMMANDS / f"kandu-{name}.md").read_text(encoding="utf-8")
            self.assertIn("docs/COMMANDS.md", text, name)
            self.assertTrue(text.startswith("---\ndescription:"), name)

    def test_setup_and_add_ask_every_question_of_the_design(self):
        setup = (COMMANDS / "kandu-setup.md").read_text(encoding="utf-8")
        add = (COMMANDS / "kandu-add.md").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"^   \d+\. ", setup, re.MULTILINE)), 8)
        self.assertEqual(len(re.findall(r"^   \d+\. ", add, re.MULTILINE)), 14)

    def test_add_never_creates_a_board_unasked(self):
        self.assertIn("only if the user said yes", (COMMANDS / "kandu-add.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
