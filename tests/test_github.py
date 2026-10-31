import json
import unittest

from kandu.config import Board
from kandu.github import GitHub, blockers_in, branch_for, is_companion, slugify

BOARD = Board(owner="acme", number=1)


class FakeGh:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        return json.dumps(self.replies.pop(0)) if self.replies else ""


class NamesTest(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(slugify("Fix the Login: page!"), "fix-the-login-page")
        self.assertEqual(branch_for(12, "Fix login"), "kandu/12-fix-login")

    def test_a_title_with_no_latin_letters(self):
        self.assertEqual(branch_for(12, "درست کردن ورود"), "kandu/12-task")

    def test_long_title_is_cut_cleanly(self):
        self.assertLessEqual(len(slugify("word " * 40)), 40)
        self.assertFalse(slugify("word " * 40).endswith("-"))


class BlockedByTest(unittest.TestCase):
    def test_none(self):
        self.assertEqual(blockers_in("## Goal\nx\n\n## Blocked by\nnone\n", "o/r"), [])
        self.assertEqual(blockers_in("no section", "o/r"), [])
        self.assertEqual(blockers_in(None, "o/r"), [])

    def test_same_and_other_repo(self):
        body = "## Blocked by\n- #12\n- acme/api#7\n"
        self.assertEqual(blockers_in(body, "o/r"), [("o/r", 12), ("acme/api", 7)])

    def test_references_outside_the_section_do_not_count(self):
        body = "## Goal\nsee #1\n\n## Blocked by\n#2\n\n## Next\n#3\n"
        self.assertEqual(blockers_in(body, "o/r"), [("o/r", 2)])

    def test_the_templates_comment_does_not_count(self):
        self.assertEqual(blockers_in("## Blocked by\n<!-- like #5 -->\nnone\n", "o/r"), [])


class CompanionTest(unittest.TestCase):
    def pr(self, branch, body="", number=1):
        return {"headRefName": branch, "body": body, "number": number}

    def test_same_slug(self):
        self.assertTrue(is_companion(self.pr("kandu/1-add-field"), "o/a", self.pr("kandu/9-add-field"), "o/b"))

    def test_the_fallback_slug_says_nothing(self):
        self.assertFalse(is_companion(self.pr("kandu/1-task"), "o/a", self.pr("kandu/9-task"), "o/b"))

    def test_a_body_names_the_other(self):
        ours = self.pr("kandu/1-a", "Needs o/b#9")
        self.assertTrue(is_companion(ours, "o/a", self.pr("x", number=9), "o/b"))
        theirs = self.pr("x", "Goes with https://github.com/o/a/pull/1", number=9)
        self.assertTrue(is_companion(self.pr("kandu/1-a"), "o/a", theirs, "o/b"))

    def test_a_longer_number_is_another_pr(self):
        self.assertFalse(is_companion(self.pr("kandu/1-a", "o/b#90"), "o/a", self.pr("x", number=9), "o/b"))

    def test_unrelated(self):
        self.assertFalse(is_companion(self.pr("kandu/1-a"), "o/a", self.pr("kandu/2-b", number=2), "o/b"))


class GitHubTest(unittest.TestCase):
    def test_board_items_are_issues_only(self):
        issue = {"type": "Issue", "repository": "o/r", "number": 3, "title": "t", "url": "u", "body": "b"}
        gh = FakeGh({"items": [
            {"content": issue, "status": "Ready", "labels": ["agent"]},
            {"content": {"type": "DraftIssue", "title": "x"}, "status": "Ready"},
            {"content": {"type": "PullRequest", "number": 4}, "status": "Ready"},
        ]})
        items = GitHub(gh).board_items(BOARD)
        self.assertEqual([(i.repo, i.number, i.status, i.labels) for i in items], [("o/r", 3, "Ready", ["agent"])])

    def test_board_queue_is_issues_only_across_pages(self):
        issue = {"number": 3, "title": "t", "url": "u", "repository": {"nameWithOwner": "o/r"},
                 "labels": {"nodes": [{"name": "agent"}]}}
        board_page = lambda nodes, more: {"data": {"repositoryOwner": {"projectV2": {"items": {
            "pageInfo": {"hasNextPage": more, "endCursor": "c"}, "nodes": nodes}}}}}
        gh = FakeGh(
            board_page([{"content": issue, "fieldValueByName": {"name": "Ready"}}, {"content": {}}], True),
            board_page([{"content": issue | {"number": 4}, "fieldValueByName": None}], False),
        )
        items = GitHub(gh).board_queue(BOARD)
        self.assertEqual([(i.repo, i.number, i.status, i.labels) for i in items],
                         [("o/r", 3, "Ready", ["agent"]), ("o/r", 4, "", ["agent"])])
        self.assertEqual(gh.calls[1][-2:], ["-f", "after=c"])

    def test_set_status(self):
        gh = FakeGh()
        GitHub(gh).set_status(BOARD, "https://x/1", "In progress")
        self.assertEqual(gh.calls[0], ["project", "item-edit", "1", "--owner", "acme", "--url", "https://x/1",
                                       "--field", "Status", "--value", "In progress"])

    def test_find_pr_takes_the_newest_own_branch(self):
        gh = FakeGh([
            {"number": 5, "headRefName": "kandu/12-a", "isCrossRepository": False},
            {"number": 8, "headRefName": "kandu/12-renamed", "isCrossRepository": False},
            {"number": 9, "headRefName": "kandu/12-a", "isCrossRepository": True},
            {"number": 10, "headRefName": "kandu/123-a", "isCrossRepository": False},
        ], {"number": 8})
        self.assertEqual(GitHub(gh).find_pr("o/r", 12), {"number": 8})
        self.assertEqual(gh.calls[1][:3], ["pr", "view", "8"])

    def test_no_pr(self):
        self.assertIsNone(GitHub(FakeGh([])).find_pr("o/r", 12))

    def test_mark_pr_once(self):
        gh = FakeGh()
        GitHub(gh).mark_pr("o/r", {"number": 1, "labels": [{"name": "kandu"}]})
        self.assertEqual(gh.calls, [])
        GitHub(gh).mark_pr("o/r", {"number": 1, "labels": []})
        self.assertEqual([c[:2] for c in gh.calls], [["label", "create"], ["pr", "edit"]])

    def test_companions_skip_the_pr_itself_and_forks(self):
        ours = {"number": 1, "headRefName": "kandu/1-add-field", "body": ""}
        gh = FakeGh(
            [{"number": 1, "headRefName": "kandu/1-add-field", "body": "", "isCrossRepository": False}],
            [{"number": 9, "headRefName": "kandu/9-add-field", "body": "", "isCrossRepository": False},
             {"number": 10, "headRefName": "kandu/10-add-field", "body": "", "isCrossRepository": True}],
        )
        self.assertEqual(GitHub(gh).companions("o/a", ours, ["o/a", "o/b"]), [("o/b", 9)])


if __name__ == "__main__":
    unittest.main()
