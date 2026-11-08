import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from kandu import show
from kandu.github import GitHubError
from kandu.mirror import Mirror
from tests.test_mirror import ACCOUNT, REPO, FakeRemote, pr


class ShowTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.remote = FakeRemote()
        self.remote.issues[3]["body"] = "## Goal\nDo it."
        self.remote.prs = {7: dict(
            pr(comments=[{"author": {"login": "user"}, "body": "looks fine", "createdAt": "2026-11-02T09:00:00Z"}]),
            title="Do it", body="Closes #3", url="https://github.com/acme/app/pull/7",
            statusCheckRollup=[{"__typename": "CheckRun", "name": "tests", "status": "COMPLETED",
                                "conclusion": "FAILURE"},
                               {"__typename": "StatusContext", "context": "deploy", "state": "PENDING"}],
            reviewThreads=[{"resolved": False, "resolved_by_other": False, "path": "app.py", "line": 12,
                            "comments": [{"author": {"login": "reviewer"}, "body": "Why a loop here?",
                                          "createdAt": "2026-11-02T10:00:00Z"}]}])}
        self.mirror = Mirror(self.remote, folder.name, ACCOUNT, clock=lambda: 1000.0, pause=lambda s: None)
        self.mirror.refresh_repo(REPO)
        self.calls = len(self.remote.calls)

    def test_an_issue_is_printed_with_its_comments(self):
        text = show.render(self.mirror.item(REPO, 3))
        self.assertTrue(text.startswith("issue acme/app#3: Task 3\n"))
        self.assertIn("state: OPEN", text)
        self.assertIn("labels: agent", text)
        self.assertIn("## Goal\nDo it.", text)
        self.assertIn("comments: 1", text)
        self.assertIn("--- user, 2026-11-01T10:00:00Z\na note", text)
        self.assertEqual(len(self.remote.calls), self.calls)

    def test_a_pr_is_printed_with_comments_threads_and_checks(self):
        text = show.render(self.mirror.item(REPO, 7))
        self.assertTrue(text.startswith("pr acme/app#7: Do it\n"))
        self.assertIn("branch: kandu/3-task @ abc", text)
        self.assertIn("Closes #3", text)
        self.assertIn("checks: 2", text)
        self.assertIn("- tests: FAILURE", text)
        self.assertIn("- deploy: PENDING", text)
        self.assertIn("review threads: 1", text)
        self.assertIn("- app.py:12, open", text)
        self.assertIn("  --- reviewer, 2026-11-02T10:00:00Z\n  Why a loop here?", text)
        self.assertIn("comments: 1", text)
        self.assertIn("--- user, 2026-11-02T09:00:00Z\nlooks fine", text)
        self.assertEqual(len(self.remote.calls), self.calls)

    def test_the_item_is_named_as_repo_number_or_its_url(self):
        self.assertEqual(show.parse_item("acme/app#3"), ("acme/app", 3))
        self.assertEqual(show.parse_item("https://github.com/acme/app/pull/7"), ("acme/app", 7))
        self.assertEqual(show.parse_item("https://github.com/acme/app/issues/3"), ("acme/app", 3))
        self.assertIsNone(show.parse_item("app#3"))

    def run_main(self, *args):
        out = io.StringIO()
        with mock.patch.object(show, "load_config"), mock.patch.object(show, "open_github", return_value=self.mirror), \
                redirect_stdout(out):
            code = show.main(list(args))
        return code, out.getvalue()

    def test_the_command_prints_from_the_copy_and_asks_github_nothing(self):
        code, out = self.run_main("acme/app#7")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("pr acme/app#7: Do it\n"))
        self.assertEqual(len(self.remote.calls), self.calls)

    def test_json_prints_the_item_as_the_copy_holds_it(self):
        code, out = self.run_main("acme/app#3", "--json")
        item = json.loads(out)
        self.assertEqual((code, item["kind"], item["comments"][0]["body"]), (0, "issue", "a note"))

    def test_an_item_github_does_not_have_fails(self):
        code, out = self.run_main("acme/app#99")
        self.assertEqual((code, out), (1, ""))

    def test_a_refused_read_fails_without_a_trace(self):
        def down(name, args):
            raise GitHubError("HTTP 502")
        self.remote.fail = down
        code, _ = self.run_main("acme/app#42")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
