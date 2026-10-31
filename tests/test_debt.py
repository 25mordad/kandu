import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kandu import debt, didban
from kandu.config import TECHNICAL_DEBT, Board
from kandu.github import BoardItem
from kandu.tasks import TaskError, required_sections

BODY = "\n\n".join(f"## {s}\ntext" for s in required_sections())
TASK = "o/r#1"


def item(status=TECHNICAL_DEBT, number=1, repo="o/r", labels=("agent",)):
    return BoardItem(repo, number, f"Task {number}", f"https://x/{number}", status, list(labels), "", "", "", "", "")


class DebtTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        self.board = Board("o", 1)
        project = mock.Mock(repos=["o/r"], board=self.board)
        project.name = "site"
        self.config = mock.Mock(projects=[project])
        self.github = mock.Mock()
        self.github.repo_is_public.return_value = False
        self.github.create_issue.return_value = "https://x/1"
        self.github.board_items.return_value = [item()]

    def promote(self, **changes):
        fields = dict(title="T", body=BODY, priority="High", kind="Bug", size="Small", deadline=None) | changes
        debt.promote(self.config, self.github, TASK, fields["title"], fields["body"], self.local,
                     fields["priority"], fields["kind"], fields["size"], fields["deadline"])

    def test_a_parked_task_goes_to_its_column_without_fields(self):
        self.assertEqual(debt.add(self.config, self.github, "site", None, "T", "Later.", self.local), "https://x/1")
        self.assertEqual(self.github.set_status.call_args.args[1:], ("https://x/1", TECHNICAL_DEBT))
        self.github.set_field.assert_not_called()

    def test_a_parked_task_still_has_a_text_in_english(self):
        for body in ("  ", "فارسی"):
            with self.assertRaises(TaskError):
                debt.add(self.config, self.github, "site", None, "T", body, self.local)
        self.github.create_issue.assert_not_called()

    def test_a_public_repo_refuses_a_private_word(self):
        self.github.repo_is_public.return_value = True
        (self.local / "private-words.txt").write_text("secretname\n", encoding="utf-8")
        with self.assertRaises(TaskError):
            debt.add(self.config, self.github, "site", None, "T", "About secretname.", self.local)

    def test_list_shows_only_parked_agent_tasks_of_registered_repos(self):
        self.github.board_queue.return_value = [item(), item("Backlog", 2), item(number=3, labels=()), item(number=4, repo="x/y")]
        self.assertEqual([i.number for i in debt.parked(self.config, self.github)], [1])

    def test_promote_moves_the_column_last(self):
        self.promote()
        names = [call[0] for call in self.github.method_calls if call[0] != "repo_is_public" and call[0] != "board_items"]
        self.assertEqual(names, ["edit_issue", "set_field", "set_field", "set_field", "set_status"])
        self.assertEqual(self.github.set_status.call_args.args[1:], ("https://x/1", "Backlog"))

    def test_promote_holds_the_task_to_the_rules_of_any_task(self):
        for changes in ({"body": "Just a note."}, {"priority": ""}, {"title": "فارسی"}):
            with self.assertRaises(TaskError):
                self.promote(**changes)
        self.github.edit_issue.assert_not_called()
        self.github.set_status.assert_not_called()

    def test_promote_takes_only_a_parked_task(self):
        self.github.board_items.return_value = [item("Ready")]
        with self.assertRaises(TaskError):
            self.promote()

    def test_the_cli_reads_the_body_from_stdin(self):
        argv = ["debt", "--local", str(self.local), "add", "--project", "site", "--title", "T", "--body-file", "-"]
        with mock.patch("sys.argv", argv), mock.patch("sys.stdin", io.StringIO("Later.")), \
                mock.patch("kandu.debt.load_config", return_value=self.config), \
                mock.patch("kandu.debt.GitHub", return_value=self.github), mock.patch("sys.stdout"):
            self.assertEqual(debt.main(), 0)
        self.assertEqual(self.github.create_issue.call_args.args[:3], ("o/r", "T", "Later."))


class DidbanOpensWithThemTest(unittest.TestCase):
    def test_they_reach_the_session(self):
        result = mock.Mock(returncode=0, stdout=f"{TASK}: Later\n", stderr="")
        brain = mock.Mock(reach="ssh host", install_dir="~/kandu")
        with mock.patch("kandu.didban.subprocess.run", return_value=result) as run:
            self.assertEqual(didban.waiting_debt(brain), f"{TASK}: Later")
        self.assertIn("kandu.debt list", run.call_args.args[0][-1])
        session = mock.Mock(brain=brain)
        self.assertIn(f"Technical debt: {TASK}: Later", didban.system_prompt(session, "ok", debt=f"{TASK}: Later"))


if __name__ == "__main__":
    unittest.main()
