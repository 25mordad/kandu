import json
import unittest

from kandu.config import Board
from kandu.github import GitHub, GitHubError, blockers_in, branch_for, is_companion, slugify, transient

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

    def test_an_issue_says_why_it_closed_and_which_pr_closed_it(self):
        closer = {"__typename": "PullRequest", "number": 8, "merged": True, "repository": {"nameWithOwner": "o/r"}}
        node = ISSUE | {"state": "CLOSED", "stateReason": "COMPLETED",
                        "timelineItems": {"nodes": [{"closer": closer}]}}
        gh = FakeGh({"data": {"repository": {"issue": node}}}, {"data": {"repository": {"issue": ISSUE | {
            "state": "CLOSED", "stateReason": "NOT_PLANNED", "timelineItems": {"nodes": [{"closer": None}]}}}}},
            {"data": {"repository": {"issue": ISSUE | {"state": "OPEN"}}}})
        github = GitHub(gh)
        self.assertEqual(github.issue_closure("o/r", 3),
                         {"reason": "COMPLETED", "by": {"repo": "o/r", "number": 8, "merged": True}})
        self.assertEqual(github.issue_closure("o/r", 3), {"reason": "NOT_PLANNED", "by": None})
        self.assertIsNone(github.issue_closure("o/r", 3))

    def test_a_pr_is_closed_through_rest(self):
        gh = FakeGh()
        GitHub(gh).close_pr("o/r", 7)
        self.assertEqual(gh.calls[0], ["api", "repos/o/r/pulls/7", "-X", "PATCH", "-f", "state=closed"])

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

    def test_pr_text_goes_as_fields_never_as_shell_text(self):
        gh = FakeGh({"number": 12})
        body = "Uses `$(rm -rf ~)` and `x`.\n\nCloses #3\n"
        self.assertEqual(GitHub(gh).create_pr("o/r", "kandu/3-a", "main", "Add `x`", body), 12)
        self.assertEqual(gh.calls[0][:4], ["api", "repos/o/r/pulls", "-X", "POST"])
        self.assertIn(f"body={body}", gh.calls[0])
        self.assertIn("head=kandu/3-a", gh.calls[0])
        GitHub(gh).edit_pr_body("o/r", 12, body)
        self.assertEqual(gh.calls[1], ["api", "repos/o/r/pulls/12", "-X", "PATCH", "-f", f"body={body}"])

    def test_no_pr(self):
        self.assertIsNone(GitHub(FakeGh([])).find_pr("o/r", 12))

    def test_mark_pr_once(self):
        gh = FakeGh()
        GitHub(gh).mark_pr("o/r", {"number": 1, "labels": [{"name": "kandu"}]})
        self.assertEqual(gh.calls, [])
        GitHub(gh).mark_pr("o/r", {"number": 1, "labels": []})
        self.assertEqual([c[:2] for c in gh.calls], [["label", "create"], ["api", "repos/o/r/issues/1/labels"]])

    def test_companions_skip_the_pr_itself_and_forks(self):
        ours = {"number": 1, "headRefName": "kandu/1-add-field", "body": ""}
        gh = FakeGh(
            [{"number": 1, "headRefName": "kandu/1-add-field", "body": "", "isCrossRepository": False}],
            [{"number": 9, "headRefName": "kandu/9-add-field", "body": "", "isCrossRepository": False},
             {"number": 10, "headRefName": "kandu/10-add-field", "body": "", "isCrossRepository": True}],
        )
        self.assertEqual(GitHub(gh).companions("o/a", ours, ["o/a", "o/b"]), [("o/b", 9)])

    def test_the_board_read_keeps_card_and_field_ids(self):
        page = board_page([{"id": "card-3", "content": ISSUE, "status": {"name": "Ready"}}])
        page["data"]["repositoryOwner"]["projectV2"] |= {"id": "P1", "fields": {"nodes": [
            {"id": "F1", "name": "Status", "options": [{"id": "o1", "name": "Ready"}]}, {"id": "F2", "name": "Deadline"}, {}]}}
        items, meta = GitHub(FakeGh(page)).read_board(BOARD)
        self.assertEqual(items[0].item_id, "card-3")
        self.assertEqual(meta, {"project_id": "P1", "fields": {"Status": {"id": "F1", "options": {"Ready": "o1"}},
                                                               "Deadline": {"id": "F2", "options": {}}}})

    def test_one_request_reads_a_repo_in_the_shape_gh_shows(self):
        pr = {"number": 7, "title": "t", "state": "OPEN", "url": "u", "body": "", "changedFiles": 1,
              "headRefName": "kandu/3-a", "headRefOid": "abc", "isDraft": False, "isCrossRepository": False,
              "mergeable": "MERGEABLE", "updatedAt": "x", "labels": {"nodes": [{"name": "kandu"}]},
              "comments": {"nodes": [{"author": {"login": "me"}, "body": "hi", "createdAt": "y"}]},
              "files": {"nodes": [{"path": "a.py", "additions": 1, "deletions": 0}]},
              "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {"nodes": [
                  {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}]}}}}]},
              "reviewThreads": {"nodes": [{"isResolved": True, "resolvedBy": {"login": "other"},
                                           "comments": {"nodes": [{"author": {"login": "me"}}]}}]}}
        issue = {"number": 3, "title": "t", "body": None, "url": "u", "state": "OPEN", "updatedAt": "x",
                 "labels": {"nodes": [{"name": "agent"}]}}
        reply = {"data": {"repository": {"defaultBranchRef": {"name": "main"},
                                         "issues": {"pageInfo": {"hasNextPage": False}, "nodes": [issue]},
                                         "open": {"pageInfo": {"hasNextPage": False}, "nodes": [pr]},
                                         "closed": {"nodes": []}}}}
        gh = FakeGh(reply)
        found = GitHub(gh).read_repo("o/r", "2026-01-01T00:00:00Z")
        self.assertEqual(len(gh.calls), 1)
        self.assertIn("since=2026-01-01T00:00:00Z", gh.calls[0])
        self.assertEqual(found["default_branch"], "main")
        self.assertEqual(found["issues"][0]["body"], "")
        got = found["prs"][0]
        self.assertEqual((got["labels"], got["files"][0]["path"], got["comments"][0]["author"]["login"]),
                         ([{"name": "kandu"}], "a.py", "me"))
        self.assertEqual(got["statusCheckRollup"][0]["conclusion"], "SUCCESS")
        self.assertEqual(got["reviewThreads"], [{"resolved": True, "resolved_by_other": True}])

    def test_a_second_page_asks_only_for_what_has_more(self):
        page = lambda more, n: {"data": {"repository": {"defaultBranchRef": None,
            "issues": {"pageInfo": {"hasNextPage": more, "endCursor": "c"}, "nodes": [
                {"number": n, "title": "t", "body": "", "url": "u", "state": "OPEN", "labels": {"nodes": []}}]},
            "open": {"pageInfo": {"hasNextPage": False}, "nodes": []}, "closed": {"nodes": []}}}}
        gh = FakeGh(page(True, 1), page(False, 2))
        found = GitHub(gh).read_repo("o/r")
        self.assertEqual([i["number"] for i in found["issues"]], [1, 2])
        self.assertIn("open=false", gh.calls[1])
        self.assertIn("issuesAfter=c", gh.calls[1])

    def test_an_issue_is_created_through_rest(self):
        gh = FakeGh(None, {"html_url": "https://github.com/o/r/issues/5"})
        self.assertEqual(GitHub(gh).create_issue("o/r", "T", "B"), "https://github.com/o/r/issues/5")
        self.assertEqual(gh.calls[1][:4], ["api", "repos/o/r/issues", "-X", "POST"])
        self.assertIn("labels[]=agent", gh.calls[1])

    def test_a_lost_answer_finds_the_issue_it_made(self):
        def gh(args):
            gh.calls.append(args)
            if args[-1:] == ["labels[]=agent"]:
                raise GitHubError("gh api repos/o/r/issues: unexpected end of JSON input")
            if args[1].startswith("repos/o/r/issues?"):
                return json.dumps([{"html_url": "https://github.com/o/r/pull/6", "title": "T", "body": "B",
                                    "pull_request": {}},
                                   {"html_url": "https://github.com/o/r/issues/5", "title": "T", "body": "B"}])
            return ""
        gh.calls = []
        self.assertEqual(GitHub(gh, pause=lambda s: None).create_issue("o/r", "T", "B"),
                         "https://github.com/o/r/issues/5")
        self.assertEqual(sum(c[1] == "repos/o/r/issues" for c in gh.calls), 1)  # never sent twice

    def test_a_lost_answer_with_no_issue_says_it_is_safe_to_run_again(self):
        def gh(args):
            if args[-1:] == ["labels[]=agent"]:
                raise GitHubError("gh api repos/o/r/issues: HTTP 502")
            return json.dumps([{"html_url": "https://github.com/o/r/issues/4", "title": "Other", "body": "B"}]) \
                if args[1].startswith("repos/o/r/issues?") else ""
        with self.assertRaises(GitHubError) as caught:
            GitHub(gh, pause=lambda s: None).create_issue("o/r", "T", "B")
        self.assertIn("safe", str(caught.exception))

    def test_a_failed_look_says_to_check_first(self):
        def gh(args):
            if args[-1:] == ["labels[]=agent"] or args[1].startswith("repos/o/r/issues?"):
                raise GitHubError("gh api: connection reset")
            return ""
        with self.assertRaises(GitHubError) as caught:
            GitHub(gh, pause=lambda s: None).create_issue("o/r", "T", "B")
        self.assertIn("before running it again", str(caught.exception))

    def test_card_changes_go_in_one_request(self):
        gh = FakeGh()
        GitHub(gh).edit_cards("P", [("i1", "f1", "option", "o1"), ("i2", "f2", "date", "2026-01-02"),
                                    ("i3", "f3", "clear", None)])
        self.assertEqual(len(gh.calls), 1)
        query = gh.calls[0][-1]
        self.assertEqual((query.count("updateProjectV2ItemFieldValue"), query.count("clearProjectV2ItemFieldValue")), (2, 1))

    def test_what_passes_by_itself(self):
        for message in ("HTTP 502: Bad Gateway", "API rate limit exceeded", "You have exceeded a secondary rate limit",
                        "gh api: TimeoutExpired"):
            self.assertTrue(transient(GitHubError(message)), message)
        self.assertFalse(transient(GitHubError("Could not resolve to an issue")))


if __name__ == "__main__":
    unittest.main()
