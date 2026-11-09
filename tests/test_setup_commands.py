import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kandu import doctor, tasks
from kandu.boards import missing_columns
from kandu.config import BOARD_COLUMNS, BOARD_FIELDS, DEADLINE_FIELD, Board, Machine
from kandu.github import GitHub, GitHubError, blockers_in
from kandu.state import StateStore
from kandu.usage import Usage, UsageError

import tempfile as _tempfile
from kandu import ghmeter as _ghmeter
_ghmeter.LOG_DIR = Path(_tempfile.mkdtemp())  # the meter's lines stay out of state/

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

    def test_a_large_task_does_not_reach_the_board(self):
        with self.assertRaises(tasks.TaskError) as caught:
            self.create(size="Large")
        self.assertIn("split", str(caught.exception))
        self.github.create_issue.assert_not_called()

    def test_a_token_task_says_so(self):
        self.config.projects[0].task_tokens = {"A_KEY": "images"}
        tasks.create_task(self.config, self.github, "site", None, "T", BODY, self.local, "High", "Bug", "Small",
                          tokens=True)
        self.assertTrue(self.github.create_issue.call_args.kwargs["tokens"])

    def test_a_token_task_needs_a_project_with_tokens(self):
        self.config.projects[0].task_tokens = {}
        with self.assertRaises(tasks.TaskError):
            tasks.create_task(self.config, self.github, "site", None, "T", BODY, self.local, "High", "Bug", "Small",
                              tokens=True)
        self.github.create_issue.assert_not_called()

    def test_a_task_for_a_person_says_so(self):
        tasks.create_task(self.config, self.github, "site", None, "T", BODY, self.local, "High", "Bug", "Small",
                          by_hand=True)
        self.assertTrue(self.github.create_issue.call_args.kwargs["by_hand"])

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

    def test_a_task_not_in_english_is_not_created(self):
        for bad in ({"title": "کار تازه"}, {"body": BODY + "\nیک خط دیگر"}):
            with self.assertRaisesRegex(tasks.TaskError, "English"):
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


class TaskCommandTest(unittest.TestCase):
    """`python -m kandu.tasks`, as `/kandu-task` runs it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.body = Path(tmp.name) / "body.md"
        self.body.write_text(BODY, encoding="utf-8")
        self.github = mock.Mock()
        self.github.repo_is_public.return_value = False
        self.github.create_issue.return_value = "https://github.com/o/r/issues/1"
        self.github.send.return_value = (4, 0, 0)
        config = mock.Mock(projects=[project()])
        config.projects[0].name = "site"
        self.store = StateStore(Path(tmp.name) / "state")
        for target, value in (("load_config", config), ("open_github", self.github), ("StateStore", self.store)):
            patcher = mock.patch.object(tasks, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_command(self, *extra):
        argv = ["tasks", "--project", "site", "--priority", "High", "--kind", "Bug", "--size", "Small", *extra]
        with mock.patch("sys.argv", argv), mock.patch("sys.stdout") as out, mock.patch("sys.stderr") as err:
            code = tasks.main()
        text = lambda stream: "".join(c.args[0] for c in stream.write.call_args_list)
        return code, text(out), text(err)

    def new_task(self):
        return self.run_command("--title", "T", "--body-file", str(self.body))

    def test_the_fields_reach_github_before_it_returns(self):
        code, out, _ = self.new_task()
        self.assertEqual(code, 0)
        self.assertIn("issues/1", out)
        names = [c[0] for c in self.github.method_calls]
        self.assertLess(names.index("set_status"), names.index("send"))

    def blocked_by(self, blockers):
        self.body.write_text(re.sub(r"## Blocked by\ntext", f"## Blocked by\n{blockers}", BODY), encoding="utf-8")

    def test_a_task_that_names_a_split_task_waits_for_its_last_slice(self):
        self.store.record("task_split", task="o/r#5", project="site", children=["o/r#6", "o/r#7"])
        self.blocked_by("#5\n#3")
        code, _, err = self.new_task()
        self.assertEqual(code, 0)
        sent = self.github.create_issue.call_args.args[2]
        self.assertIn("## Blocked by\n#7\n#3", sent)
        self.assertIn("o/r#5 was split", err)
        self.assertIn("o/r#7", err)
        rewrite = self.store.last_event("blocker_rewritten", task="o/r#1")
        self.assertEqual((rewrite["old"], rewrite["new"], rewrite["when"]), ("o/r#5", "o/r#7", "defined"))

    def sent_blockers(self):
        return blockers_in(self.github.create_issue.call_args.args[2], "o/r")

    def test_a_slice_split_again_leads_on_to_its_own_last_slice(self):
        self.store.record("task_split", task="o/r#5", project="site", children=["o/r#6", "o/r#7"])
        self.store.record("task_split", task="o/r#7", project="site", children=["o/r#8", "o/r#9"])
        self.blocked_by("#5")
        code, _, _ = self.new_task()
        self.assertEqual(code, 0)
        self.assertEqual(self.sent_blockers(), [("o/r", 9)])
        rewrite = self.store.last_event("blocker_rewritten", task="o/r#1")
        self.assertEqual((rewrite["old"], rewrite["new"]), ("o/r#5", "o/r#9"))

    def test_a_split_that_loops_back_still_ends(self):
        self.store.record("task_split", task="o/r#5", project="site", children=["o/r#6", "o/r#7"])
        self.store.record("task_split", task="o/r#7", project="site", children=["o/r#8", "o/r#5"])
        self.blocked_by("#5")
        code, _, _ = self.new_task()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.sent_blockers()), 1)

    def test_the_authors_words_in_blocked_by_stay(self):
        self.store.record("task_split", task="o/r#5", project="site", children=["o/r#6", "o/r#7"])
        self.blocked_by("- #5 (needs the API first)\n<!-- ask about #5 -->\n- #3 the login page")
        code, _, _ = self.new_task()
        self.assertEqual(code, 0)
        sent = self.github.create_issue.call_args.args[2]
        self.assertIn("## Blocked by\n- #7 (needs the API first)\n<!-- ask about #5 -->\n- #3 the login page", sent)

    def test_the_last_slice_named_too_is_named_once_and_the_rest_stays(self):
        self.store.record("task_split", task="o/r#5", project="site", children=["o/r#6", "o/r#7"])
        self.blocked_by("- #5\n- #7\n- #3")
        code, _, _ = self.new_task()
        self.assertEqual(code, 0)
        self.assertEqual(self.sent_blockers(), [("o/r", 7), ("o/r", 3)])
        self.assertIn("## Blocked by\n- #7\n- #3", self.github.create_issue.call_args.args[2])

    def test_a_task_that_names_no_split_task_is_sent_as_written(self):
        self.store.record("task_split", task="o/r#5", project="site", children=["o/r#6", "o/r#7"])
        self.blocked_by("#3")
        code, _, err = self.new_task()
        self.assertEqual(code, 0)
        self.assertEqual(self.github.create_issue.call_args.args[2], self.body.read_text(encoding="utf-8"))
        self.assertNotIn("split", err)
        self.assertIsNone(self.store.last_event("blocker_rewritten"))

    def test_fields_still_waiting_are_reported(self):
        self.github.send.return_value = (2, 2, 0)
        code, _, err = self.new_task()
        self.assertEqual(code, 0)
        self.assertIn("2 change(s) still wait", err)

    def test_a_failed_board_step_names_the_issue_and_how_to_finish(self):
        self.github.add_to_board.side_effect = GitHubError("gh api graphql: Something went wrong")
        code, _, err = self.new_task()
        self.assertEqual(code, 1)
        self.assertIn("--place https://github.com/o/r/issues/1", err)
        self.github.send.assert_not_called()

    def test_place_finishes_an_existing_issue_without_a_new_one(self):
        code, out, _ = self.run_command("--place", "https://github.com/o/r/issues/1")
        self.assertEqual(code, 0)
        self.github.create_issue.assert_not_called()
        self.github.add_to_board.assert_called_once()
        self.github.send.assert_called_once()

    def test_the_command_makes_a_spec_without_fields(self):
        self.body.write_text(SPEC, encoding="utf-8")
        argv = ["tasks", "--project", "site", "--spec-issue", "--title", "T", "--body-file", str(self.body)]
        with mock.patch("sys.argv", argv), mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(tasks.main(), 0)
        self.assertTrue(self.github.create_issue.call_args.kwargs["by_hand"])
        self.github.add_to_board.assert_not_called()
        self.assertEqual(tasks.specs(self.store), {"o/r#1"})
        code, _, err = self.run_command("--spec-issue", "--title", "T", "--body-file", str(self.body))
        self.assertEqual(code, 1)
        self.assertIn("not sized", err)

    def test_a_spec_without_its_sections_is_refused_by_the_command(self):
        argv = ["tasks", "--project", "site", "--spec-issue", "--title", "T", "--body-file", str(self.body)]
        with mock.patch("sys.argv", argv), mock.patch("sys.stdout"), mock.patch("sys.stderr") as err:
            self.assertEqual(tasks.main(), 1)
        self.assertIn("Problem", "".join(c.args[0] for c in err.write.call_args_list))
        self.github.create_issue.assert_not_called()

    def test_the_command_links_a_task_to_its_spec(self):
        self.store.record("spec_created", task="o/r#4", project="site")
        self.github.issue_text.return_value = {"title": "S", "body": SPEC, "url": "u"}
        self.github.issue_is_open.return_value = True
        code, _, _ = self.run_command("--title", "T", "--body-file", str(self.body), "--spec", "#4")
        self.assertEqual(code, 0)
        self.assertTrue(self.github.create_issue.call_args.args[2].startswith("Spec: #4\n"))
        self.assertEqual(self.store.last_event("spec_named", task="o/r#1")["spec"], "o/r#4")

    def test_place_refuses_an_issue_of_another_project(self):
        code, _, err = self.run_command("--place", "https://github.com/x/y/issues/1")
        self.assertEqual(code, 1)
        self.github.add_to_board.assert_not_called()


SPEC = "## Problem\nP\n\n## Solution\nS\n\n## Decisions\n- D\n\n## Out of scope\nNo emails.\n"


class SpecTest(unittest.TestCase):
    """A spec issue holds a request of several tasks; each task names it."""

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
        self.config = mock.Mock(projects=[project()])
        self.config.projects[0].name = "site"

    def spec(self, body=SPEC):
        return tasks.create_spec(self.config, self.github, "site", None, "A request", body, self.local, self.store)

    def task(self, spec, body=BODY):
        return tasks.create_task(self.config, self.github, "site", None, "T", body, self.local, "High", "Bug",
                                 "Small", store=self.store, spec=spec)

    def test_a_spec_needs_all_four_sections(self):
        self.assertEqual(tasks.spec_sections(), ["Problem", "Solution", "Decisions", "Out of scope"])
        for section in tasks.spec_sections():
            with self.assertRaisesRegex(tasks.TaskError, section):
                self.spec(SPEC.replace(f"## {section}\n", "## Other\n"))
        self.github.create_issue.assert_not_called()

    def test_a_spec_is_checked_as_a_task_is(self):
        with self.assertRaisesRegex(tasks.TaskError, "English"):
            self.spec(SPEC + "\nیک خط")
        (self.local / "private-words.txt").write_text("SecretName\n", encoding="utf-8")
        self.github.repo_is_public.return_value = True
        with self.assertRaises(tasks.TaskError):
            self.spec(SPEC + "\nSecretName")
        self.github.create_issue.assert_not_called()

    def test_a_spec_has_no_agent_label_no_fields_and_no_card(self):
        self.assertEqual(self.spec(), "https://github.com/o/r/issues/11")
        self.assertTrue(self.github.create_issue.call_args.kwargs["by_hand"])
        self.github.add_to_board.assert_not_called()
        self.github.set_field.assert_not_called()
        self.assertEqual(tasks.specs(self.store), {"o/r#11"})

    def test_a_task_names_its_spec_and_takes_its_out_of_scope(self):
        self.spec()
        url = self.task("#11")
        sent = self.github.create_issue.call_args.args[2]
        self.assertTrue(sent.startswith("Spec: #11\n"))
        self.assertIn("## Out of scope\nNo emails.\n", sent)
        self.assertNotIn("## Out of scope\ntext", sent)
        self.assertEqual(sent.count("## Out of scope"), 1)
        self.github.issue_text.assert_called_with("o/r", 11)
        named = self.store.last_event("spec_named", task="o/r#12")
        self.assertEqual((named["spec"], named["project"]), ("o/r#11", "site"))
        self.assertEqual(url, "https://github.com/o/r/issues/12")

    def test_a_spec_in_another_repo_is_named_in_full(self):
        self.config.projects[0].repos = ["o/r", "o/b"]
        tasks.create_spec(self.config, self.github, "site", "o/b", "A request", SPEC, self.local, self.store)
        tasks.create_task(self.config, self.github, "site", "o/r", "T", BODY, self.local, "High", "Bug", "Small",
                          store=self.store, spec="o/b#11")
        self.assertTrue(self.github.create_issue.call_args.args[2].startswith("Spec: o/b#11\n"))

    def test_a_task_refuses_what_is_not_an_open_spec_of_its_project(self):
        self.spec()
        self.github.issue_is_open.return_value = False
        with self.assertRaisesRegex(tasks.TaskError, "closed"):
            self.task("#11")
        self.github.issue_is_open.return_value = True
        for bad in ("#5", "o/r#5", "nothing"):
            with self.assertRaises(tasks.TaskError):
                self.task(bad)
        self.store.record("spec_created", task="o/r#30", project="other")
        with self.assertRaises(tasks.TaskError):
            self.task("#30")
        self.assertEqual(self.github.create_issue.call_count, 1)


class GitHubBoardTest(unittest.TestCase):
    def test_column_names_are_mapped_both_ways(self):
        board = Board("o", 1, columns={"Ready": "To do"})
        content = {"number": 1, "title": "t", "url": "u", "repository": {"nameWithOwner": "o/r"}, "labels": {"nodes": []}}
        page = {"pageInfo": {"hasNextPage": False}, "nodes": [{"content": content, "status": {"name": "To do"}}]}
        reply = json.dumps({"data": {"repositoryOwner": {"projectV2": {"items": page}}}})
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
        self.assertEqual([r[0] for r in results], [doctor.FAIL, doctor.WARN, doctor.OK])

    def test_guard_terms_that_cannot_be_read(self):
        with tempfile.TemporaryDirectory() as local:
            (Path(local) / "projects.yaml").write_text("projects: [\n", encoding="utf-8")
            results = doctor.check_guard(".githooks", local)
        self.assertEqual(results[-1][0], doctor.FAIL)

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

    def check_project(self, copy=True, run="clone", tool=True, token=True, columns=BOARD_COLUMNS, github_error=False,
                      fields=(*BOARD_FIELDS, DEADLINE_FIELD), task_tokens=None):
        task_tokens = task_tokens or {}
        host = mock.Mock()
        host.absolute.side_effect = lambda p: p
        host.succeeds.side_effect = lambda command, cwd=None: (token if command[0] == "grep" else
                                                               copy if command[0] == "test" else tool)
        runner = mock.Mock(hosts={"m": host})
        github = mock.Mock()
        github.board_columns.return_value = list(columns)
        github.board_fields.return_value = list(fields)
        if github_error:
            github.board_columns.side_effect = GitHubError("x")
        item = mock.Mock(path="~/p", repos=["o/r"], run=run, machine="m", test="npm test", board=Board("o", 1),
                         secrets_from="~/p/.env", task_tokens=task_tokens)
        item.name = "site"
        return doctor.check_project(item, runner, github)

    def test_a_token_missing_from_the_secrets_file_fails(self):
        self.assertIn(doctor.FAIL, {r[0] for r in self.check_project(task_tokens={"A_KEY": "images"}, token=False)})
        self.assertEqual({r[0] for r in self.check_project(task_tokens={"A_KEY": "images"})}, {doctor.OK})

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
        self.assertEqual(len(re.findall(r"^   \d+\. ", setup, re.MULTILINE)), 13)
        self.assertEqual(len(re.findall(r"^   \d+\. ", add, re.MULTILINE)), 17)

    def test_add_never_creates_a_board_unasked(self):
        self.assertIn("only if the user said yes", (COMMANDS / "kandu-add.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
