import json
import tempfile
import unittest

from kandu.config import Board
from kandu.github import BOARD_CACHE_SECONDS, BoardCache, GitHub, GitHubError, blockers_in, branch_for, is_companion, slugify

BOARD = Board(owner="acme", number=1)
ISSUE = {"number": 3, "title": "t", "url": "u", "body": "b", "repository": {"nameWithOwner": "o/r"},
         "labels": {"nodes": [{"name": "agent"}]}}


def board_page(nodes, more=False):
    return {"data": {"repositoryOwner": {"projectV2": {"items": {
        "pageInfo": {"hasNextPage": more, "endCursor": "c"}, "nodes": nodes}}}}}


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


class BoardCacheTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.now = 1000.0
        self.cache = BoardCache(folder.name, clock=lambda: self.now)
        self.page = board_page([{"content": ISSUE, "status": {"name": "Backlog"}}])

    def test_every_process_shares_one_read(self):
        gh = FakeGh(self.page)
        GitHub(gh, self.cache).board_items(BOARD)
        other = GitHub(FakeGh(), BoardCache(self.cache.root, clock=self.cache.clock))
        self.assertEqual(other.board_items(BOARD)[0].labels, ["agent"])
        self.assertEqual(len(gh.calls), 1)

    def test_a_copy_too_old_is_read_again(self):
        gh = FakeGh(self.page, self.page)
        github = GitHub(gh, self.cache)
        github.board_items(BOARD)
        self.now += BOARD_CACHE_SECONDS
        github.board_items(BOARD)
        self.assertEqual(len(gh.calls), 2)

    def test_a_write_lands_in_the_copy_and_keeps_its_age(self):
        gh = FakeGh(self.page, "", "", self.page)
        github = GitHub(gh, self.cache)
        github.board_items(BOARD)
        github.set_status(BOARD, "u", "Ready")
        github.set_field(BOARD, "u", "Size", "Small")
        item = github.board_items(BOARD)[0]
        self.assertEqual((item.status, item.size), ("Ready", "Small"))
        self.assertEqual(len(gh.calls), 3)
        self.now += BOARD_CACHE_SECONDS
        self.assertEqual(github.board_items(BOARD)[0].status, "Backlog")

    def test_a_refused_write_leaves_the_copy_alone(self):
        replies = [json.dumps(self.page)]

        def run(args):
            if args[0] == "project":
                raise GitHubError("limit")
            return replies.pop(0)

        github = GitHub(run, self.cache)
        github.board_items(BOARD)
        with self.assertRaises(GitHubError):
            github.set_status(BOARD, "u", "Ready")
        self.assertEqual(github.board_items(BOARD)[0].status, "Backlog")

    def test_what_the_copy_cannot_follow_drops_it(self):
        for change in (lambda g: g.add_to_board(BOARD, "u"), lambda g: g.close_issue("o/r", 3),
                       lambda g: g.edit_issue("o/r", 3, "t", "b")):
            gh = FakeGh(self.page, "", self.page)
            github = GitHub(gh, self.cache)
            self.cache.forget()
            github.board_items(BOARD)
            change(github)
            github.board_items(BOARD)
            self.assertEqual(len(gh.calls), 3)

    def test_a_broken_copy_is_read_again(self):
        gh = FakeGh(self.page)
        self.cache.root.joinpath(f"{BOARD.owner}-{BOARD.number}.json").write_text("{", encoding="utf-8")
        self.assertEqual(GitHub(gh, self.cache).board_items(BOARD)[0].number, 3)


class GitHubTest(unittest.TestCase):
    def test_board_items_are_issues_only_across_pages(self):
        gh = FakeGh(board_page([{"content": ISSUE, "status": {"name": "Ready"}, "size": {"name": "Small"},
                                 "deadline": {"date": "2026-01-02"}}, {"content": {}}], True),
                    board_page([{"content": ISSUE | {"number": 4}, "status": None}], False))
        items = GitHub(gh).board_items(BOARD)
        self.assertEqual([(i.repo, i.number, i.status, i.labels) for i in items],
                         [("o/r", 3, "Ready", ["agent"]), ("o/r", 4, "", ["agent"])])
        self.assertEqual((items[0].body, items[0].size, items[0].deadline), ("b", "Small", "2026-01-02"))
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
