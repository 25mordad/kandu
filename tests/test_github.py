import json
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from kandu import ghmeter
from kandu.config import Board
from kandu.ghlimit import FIRST_WAIT_SECONDS, LONGEST_WAIT_SECONDS, Gate, main as ghlimit_main, spent_limit_reset
from kandu.github import BOARD_QUERY, REPO_QUERY, run_gh, PR_FIELDS, PR_NODE, GitHub, GitHubError, blockers_in, task_blockers, branch_for, is_companion, slugify, transient, is_breaking

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


class TaskBlockersTest(unittest.TestCase):
    def test_the_section_and_the_relationships_join(self):
        body = "## Blocked by\n#2\n"
        self.assertEqual(task_blockers(body, "o/r", [{"repo": "acme/api", "number": 7}]), [("o/r", 2), ("acme/api", 7)])

    def test_a_blocker_named_in_both_counts_once(self):
        body = "## Blocked by\n#2\nAcme/API#7\n"
        relationships = [{"repo": "o/r", "number": 9}, {"repo": "o/r", "number": 2}, {"repo": "acme/api", "number": 7}]
        self.assertEqual(task_blockers(body, "o/r", relationships), [("o/r", 2), ("Acme/API", 7), ("o/r", 9)])

    def test_no_blockers_of_either_kind(self):
        self.assertEqual(task_blockers("## Blocked by\nnone\n", "o/r", []), [])
        self.assertEqual(task_blockers(None, "o/r", None), [])
        self.assertEqual(task_blockers("## Blocked by\n#2\n", "o/r"), [("o/r", 2)])

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
        self.assertIn("base=main", gh.calls[0])
        GitHub(gh).edit_pr_body("o/r", 12, body)
        self.assertEqual(gh.calls[1], ["api", "repos/o/r/pulls/12", "-X", "PATCH", "-f", f"body={body}"])

    def test_no_pr(self):
        self.assertIsNone(GitHub(FakeGh([])).find_pr("o/r", 12))

    def test_a_branch_tip_on_github_or_none(self):
        gh = FakeGh({"ref": "refs/heads/kandu/1-a", "object": {"sha": "abc"}})
        self.assertEqual(GitHub(gh).branch_tip("o/r", "kandu/1-a"), "abc")
        self.assertEqual(gh.calls, [["api", "repos/o/r/git/ref/heads/kandu/1-a"]])

        def missing(args):
            raise GitHubError("gh api repos/o/r/git/ref/heads/kandu/1-a: gh: Not Found (HTTP 404)")
        self.assertIsNone(GitHub(missing).branch_tip("o/r", "kandu/1-a"))

        def busy(args):
            raise GitHubError("gh api: HTTP 502")
        with self.assertRaises(GitHubError):
            GitHub(busy).branch_tip("o/r", "kandu/1-a")

    def test_delete_branch(self):
        gh = FakeGh()
        GitHub(gh).delete_branch("o/r", "kandu/1-a")
        self.assertEqual(gh.calls, [["api", "-X", "DELETE", "repos/o/r/git/refs/heads/kandu/1-a"]])

    def test_a_pr_says_when_it_merged(self):
        self.assertIn("mergedAt", PR_NODE)
        self.assertIn("mergedAt", PR_FIELDS.split(","))

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

    def test_the_board_read_keeps_each_cards_blocked_by_relationships(self):
        blocked = dict(ISSUE, blockedBy={"nodes": [{"number": 2, "repository": {"nameWithOwner": "o/r"}},
                                                   {"number": 9, "repository": {"nameWithOwner": "o/other"}}]})
        free = dict(ISSUE, number=4)  # GitHub sent no relationships
        gh = FakeGh(board_page([{"id": "card-3", "content": blocked}, {"id": "card-4", "content": free}]))
        items, _ = GitHub(gh).read_board(BOARD)
        self.assertIn("blockedBy", " ".join(gh.calls[0]))
        self.assertEqual(items[0].blocked_by, [{"repo": "o/r", "number": 2}, {"repo": "o/other", "number": 9}])
        self.assertEqual(items[1].blocked_by, [])

    def test_an_issue_says_why_it_closed_and_what_closed_it(self):
        def issue(closer, state="CLOSED", reason="COMPLETED"):
            node = {"number": 3, "title": "t", "body": "b", "url": "u", "state": state, "stateReason": reason,
                    "labels": {"nodes": []}, "timelineItems": {"nodes": [{"closer": closer}] if closer else []}}
            return GitHub(FakeGh({"data": {"repository": {"issue": node}}})).issue_closure("o/r", 3)
        by_pr = {"__typename": "PullRequest", "number": 9, "merged": True, "repository": {"nameWithOwner": "o/r"}}
        by_commit = {"__typename": "Commit", "oid": "abc1234", "repository": {"nameWithOwner": "o/r"}}
        self.assertEqual(issue(by_pr), {"reason": "COMPLETED", "by": {"repo": "o/r", "number": 9, "merged": True}})
        self.assertEqual(issue(by_commit), {"reason": "COMPLETED", "by": {"repo": "o/r", "sha": "abc1234", "merged": True}})
        self.assertEqual(issue(None, reason="NOT_PLANNED"), {"reason": "NOT_PLANNED", "by": None})
        self.assertIsNone(issue(by_pr, state="OPEN", reason=None))

    def test_an_issue_says_when_it_closed(self):
        node = {"number": 3, "title": "t", "body": "b", "url": "u", "state": "CLOSED", "stateReason": "NOT_PLANNED",
                "closedAt": "2026-11-02T09:00:00Z", "labels": {"nodes": []}}
        gh = FakeGh({"data": {"repository": {"issue": node}}})
        self.assertEqual(GitHub(gh).read_issue("o/r", 3)["closedAt"], "2026-11-02T09:00:00Z")
        self.assertIn("closedAt", " ".join(gh.calls[0]))

    def test_a_pr_is_closed_through_the_api(self):
        gh = FakeGh()
        GitHub(gh).close_pr("o/r", 7)
        self.assertEqual(gh.calls[0], ["api", "repos/o/r/pulls/7", "-X", "PATCH", "-f", "state=closed"])

    def test_an_issue_is_read_with_its_blocked_by_relationships(self):
        issue = {"number": 3, "title": "t", "body": "b", "url": "u", "state": "OPEN", "labels": {"nodes": []},
                 "blockedBy": {"nodes": [{"number": 2, "repository": {"nameWithOwner": "o/r"}}]}}
        gh = FakeGh({"data": {"repository": {"issue": issue}}})
        self.assertEqual(GitHub(gh).read_issue("o/r", 3)["blocked_by"], [{"repo": "o/r", "number": 2}])
        self.assertIn("blockedBy", " ".join(gh.calls[0]))
        repo = {"data": {"repository": {"defaultBranchRef": None,
                                        "issues": {"pageInfo": {"hasNextPage": False},
                                                   "nodes": [issue, dict(issue, number=4, blockedBy=None)]},
                                        "open": {"pageInfo": {"hasNextPage": False}, "nodes": []},
                                        "closed": {"nodes": []}}}}
        gh = FakeGh(repo)
        found = GitHub(gh).read_repo("o/r")
        self.assertIn("blockedBy", " ".join(gh.calls[0]))
        self.assertEqual([i["blocked_by"] for i in found["issues"]], [[{"repo": "o/r", "number": 2}], []])

    def test_removing_a_blocked_by_relationship_names_the_blocker_by_its_issue_id(self):
        gh = FakeGh({"id": 4242, "number": 2})
        GitHub(gh).remove_blocked_by("o/r", 3, "o/other", 2)
        self.assertEqual(gh.calls, [["api", "repos/o/other/issues/2"],
                                    ["api", "repos/o/r/issues/3/dependencies/blocked_by/4242", "-X", "DELETE"]])

    def test_one_request_reads_a_repo_in_the_shape_gh_shows(self):
        pr = {"number": 7, "title": "t", "state": "OPEN", "url": "u", "body": "", "changedFiles": 1,
              "headRefName": "kandu/3-a", "headRefOid": "abc", "isDraft": False, "isCrossRepository": False,
              "mergeable": "MERGEABLE", "updatedAt": "x", "labels": {"nodes": [{"name": "kandu"}]},
              "comments": {"nodes": [{"author": {"login": "me"}, "body": "hi", "createdAt": "y"}]},
              "files": {"nodes": [{"path": "a.py", "additions": 1, "deletions": 0}]},
              "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {"nodes": [
                  {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}]}}}}]},
              "reviewThreads": {"nodes": [{"isResolved": True, "resolvedBy": {"login": "other"}, "path": "a.py",
                                           "line": 4, "comments": {"nodes": [
                                               {"author": {"login": "me"}, "body": "why?", "createdAt": "z"}]}}]}}
        issue = {"number": 3, "title": "t", "body": None, "url": "u", "state": "OPEN", "updatedAt": "x",
                 "labels": {"nodes": [{"name": "agent"}]},
                 "comments": {"nodes": [{"author": None, "body": "a note", "createdAt": "w"}]}}
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
        self.assertEqual(got["reviewThreads"], [{"resolved": True, "resolved_by_other": True, "path": "a.py", "line": 4,
                                                 "comments": [{"author": {"login": "me"}, "body": "why?",
                                                               "createdAt": "z"}]}])
        self.assertEqual(found["issues"][0]["comments"], [{"author": {}, "body": "a note", "createdAt": "w"}])

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

    def test_one_issue_is_read_with_its_comments(self):
        issue = {"number": 3, "title": "t", "body": "b", "url": "u", "state": "OPEN", "labels": {"nodes": []},
                 "comments": {"nodes": [{"author": {"login": "me"}, "body": "hi", "createdAt": "y"}]}}
        got = GitHub(FakeGh({"data": {"repository": {"issue": issue}}})).read_issue("o/r", 3)
        self.assertEqual(got["comments"], [{"author": {"login": "me"}, "body": "hi", "createdAt": "y"}])

    def test_a_number_that_is_no_pr_reads_as_none(self):
        def gh(args):
            raise GitHubError("gh api graphql: Could not resolve to a PullRequest with the number of 3.")
        self.assertIsNone(GitHub(gh).read_pr("o/r", 3))

    def test_an_issue_is_created_through_rest(self):
        gh = FakeGh(None, {"html_url": "https://github.com/o/r/issues/5"})
        self.assertEqual(GitHub(gh).create_issue("o/r", "T", "B"), "https://github.com/o/r/issues/5")
        self.assertEqual(gh.calls[1][:4], ["api", "repos/o/r/issues", "-X", "POST"])
        self.assertIn("labels[]=agent", gh.calls[1])

    def test_a_task_done_by_hand_is_labelled_by_hand_and_not_agent(self):
        gh = FakeGh(None, {"html_url": "https://github.com/o/r/issues/5"})
        GitHub(gh).create_issue("o/r", "T", "B", by_hand=True)
        self.assertEqual(gh.calls[0][:3], ["label", "create", "by-hand"])
        self.assertIn("labels[]=by-hand", gh.calls[1])
        self.assertNotIn("labels[]=agent", gh.calls[1])

    def test_a_task_for_kandu_is_not_labelled_by_hand(self):
        gh = FakeGh(None, {"html_url": "https://github.com/o/r/issues/5"})
        GitHub(gh).create_issue("o/r", "T", "B")
        self.assertFalse(any("labels[]=by-hand" in call or "by-hand" in call for call in gh.calls))

    def test_a_breaking_task_is_labelled_breaking(self):
        gh = FakeGh(None, None, {"html_url": "https://github.com/o/r/issues/5"})
        GitHub(gh).create_issue("o/r", "T", "B", breaking=True)
        self.assertIn(["label", "create", "breaking"], [call[:3] for call in gh.calls])
        self.assertIn("labels[]=breaking", gh.calls[-1])
        self.assertIn("labels[]=agent", gh.calls[-1])

    def test_a_task_without_the_mark_gets_no_breaking_label(self):
        gh = FakeGh(None, {"html_url": "https://github.com/o/r/issues/5"})
        GitHub(gh).create_issue("o/r", "T", "B")
        self.assertFalse(any("breaking" in arg for call in gh.calls for arg in call))

    def test_a_breaking_pr_is_marked_once(self):
        gh = FakeGh(None, None)
        GitHub(gh).mark_breaking("o/r", {"number": 7, "labels": [{"name": "breaking"}]})
        self.assertEqual(gh.calls, [])
        GitHub(gh).mark_breaking("o/r", {"number": 7, "labels": []})
        self.assertEqual(gh.calls[0][:3], ["label", "create", "breaking"])
        self.assertEqual(gh.calls[1][1], "repos/o/r/issues/7/labels")

    def test_the_mark_is_read_back_from_a_merged_pr(self):
        self.assertTrue(is_breaking({"state": "MERGED", "labels": [{"name": "kandu"}, {"name": "breaking"}]}))
        self.assertTrue(is_breaking({"labels": ["breaking"]}))
        self.assertFalse(is_breaking({"state": "MERGED", "labels": [{"name": "kandu"}]}))
        self.assertFalse(is_breaking({}))

    def test_a_spec_gets_no_label(self):
        gh = FakeGh({"html_url": "https://github.com/o/r/issues/5"})
        GitHub(gh).create_issue("o/r", "T", "B", by_hand=True, spec=True)
        self.assertEqual(len(gh.calls), 1)
        self.assertFalse(any(arg.startswith("labels[]=") for arg in gh.calls[0]))

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


class FakeRun:
    """`subprocess.run` for `gh`: answers in turn, and keeps each call with its environment."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs.get("env") or {}))
        code, out, err = self.answers.pop(0)
        return subprocess.CompletedProcess(argv, code, out, err)


PRIMARY = (1, "", "GraphQL: API rate limit already exceeded for user ID 1.")
SECONDARY = (1, "", "gh: You have exceeded a secondary rate limit. Please wait a few minutes before you try again.")
ANSWER = (0, "{}", "")


class BackOffTest(unittest.TestCase):
    """A rate-limit answer stops every call until GitHub's time, instead of a retry that keeps the block going."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.now = 1000.0
        self.reset = None  # when GitHub says the spent limit comes back
        self.gate = Gate(self.tmp / "backoff.json", clock=lambda: self.now, lookup=lambda: self.reset)

    def call(self, run):
        return run_gh(["api", "graphql"], gate=self.gate, run=run, meter_log=self.tmp / "gh")

    def refused(self, run):
        with self.assertRaises(GitHubError) as caught:
            self.call(run)
        return caught.exception

    def test_a_rate_limit_answer_is_followed_by_a_wait_not_a_retry(self):
        run = FakeRun(PRIMARY, ANSWER)
        self.refused(run)
        again = self.refused(run)  # tried again at once: GitHub is not asked
        self.assertEqual(len(run.calls), 1)
        self.assertTrue(transient(again))  # so a queued change waits, and is not dropped
        self.now += FIRST_WAIT_SECONDS
        self.assertEqual(self.call(run), "{}")
        self.assertEqual(len(run.calls), 2)
        self.assertIsNone(self.gate.blocked())

    def test_the_wait_is_shared_by_every_process(self):
        self.refused(FakeRun(SECONDARY))
        other = FakeRun(ANSWER)
        with self.assertRaises(GitHubError):
            run_gh(["pr", "view"], gate=Gate(self.gate.path, clock=lambda: self.now, lookup=lambda: None), run=other)
        self.assertEqual(other.calls, [])

    def test_a_spent_limit_waits_until_github_says_it_comes_back(self):
        self.reset = 5000
        run = FakeRun(PRIMARY, ANSWER)
        self.refused(run)
        self.now = 4999
        self.refused(run)
        self.assertEqual(len(run.calls), 1)
        self.now = 5001
        self.call(run)
        self.assertEqual(len(run.calls), 2)

    def test_without_a_time_from_github_each_wait_in_a_row_is_longer(self):
        run = FakeRun(SECONDARY, SECONDARY, ANSWER, SECONDARY)
        self.refused(run)
        self.assertEqual(self.gate.blocked()["until"], self.now + FIRST_WAIT_SECONDS)
        self.now += FIRST_WAIT_SECONDS
        self.refused(run)
        self.assertEqual(self.gate.blocked()["until"], self.now + 2 * FIRST_WAIT_SECONDS)
        self.now += 2 * FIRST_WAIT_SECONDS
        self.call(run)  # a call that goes through ends the streak
        self.refused(run)
        self.assertEqual(self.gate.blocked()["until"], self.now + FIRST_WAIT_SECONDS)

    def test_the_wait_is_never_longer_than_an_hour(self):
        self.gate.path.write_text(json.dumps({"until": 0, "refusals": 30, "why": "x"}))
        self.refused(FakeRun(SECONDARY))
        self.assertEqual(self.gate.blocked()["until"], self.now + LONGEST_WAIT_SECONDS)

    def test_another_failure_sets_no_wait(self):
        run = FakeRun((1, "", "GraphQL: Could not resolve to an Issue"), ANSWER)
        self.refused(run)
        self.assertIsNone(self.gate.blocked())
        self.call(run)
        self.assertEqual(len(run.calls), 2)

    def test_an_unreadable_backoff_file_holds_nothing(self):
        self.gate.path.write_text("not json")
        self.assertEqual(self.call(FakeRun(ANSWER)), "{}")

    def test_the_meter_leaves_kandus_own_calls_to_this_gate(self):
        run = FakeRun(ANSWER)
        self.call(run)
        self.assertEqual(run.calls[0][1][ghmeter.GATE_ENV], "kandu")
        self.assertNotIn(ghmeter.GATE_ENV, ghmeter.env(base=run.calls[0][1]))  # an agent started from here keeps it

    def test_the_points_a_call_cost_are_written_beside_it(self):
        run = FakeRun((0, json.dumps({"data": {"rateLimit": {"cost": 7}}}), ""))
        self.call(run)
        call = run.calls[0][1][ghmeter.CALL_ENV]
        [day] = (self.tmp / "gh").iterdir()
        [line] = [json.loads(text) for text in day.read_text().splitlines()]
        self.assertEqual((line["type"], line["call"], line["points"]), ("cost", call, 7))

    def test_kandus_reads_ask_what_they_cost(self):
        for query in (BOARD_QUERY, REPO_QUERY):
            self.assertIn("rateLimit { cost }", query)
            self.assertEqual(query.count("{"), query.count("}"))

    def test_the_meter_records_an_agents_refusal(self):
        stdin = "gh: You have exceeded a secondary rate limit."
        with unittest.mock.patch("sys.stdin", __import__("io").StringIO(stdin)), \
                unittest.mock.patch("kandu.ghlimit.spent_limit_reset", lambda: None), \
                unittest.mock.patch("sys.stdout", __import__("io").StringIO()):
            self.assertEqual(ghlimit_main(["refused", str(self.gate.path)]), 0)
        self.assertEqual(json.loads(self.gate.path.read_text())["refusals"], 1)


def rate_limit(graphql_remaining=4000, graphql_reset=5000, core_remaining=4500, core_reset=6000):
    """An answer shaped like GitHub's `gh api rate_limit`."""
    return json.dumps({
        "resources": {
            "core": {"limit": 5000, "used": 5000 - core_remaining, "remaining": core_remaining,
                     "reset": core_reset},
            "search": {"limit": 30, "used": 30, "remaining": 0, "reset": 9999},
            "graphql": {"limit": 5000, "used": 5000 - graphql_remaining, "remaining": graphql_remaining,
                        "reset": graphql_reset},
        },
        "rate": {"limit": 5000, "used": 5000 - core_remaining, "remaining": core_remaining, "reset": core_reset},
    })


class SpentLimitResetTest(unittest.TestCase):
    """GitHub's own rate_limit answer says when a spent primary limit comes back."""

    def test_a_spent_graphql_limit_comes_back_at_its_reset(self):
        run = FakeRun((0, rate_limit(graphql_remaining=0, graphql_reset=5000), ""))
        self.assertEqual(spent_limit_reset(run=run), 5000)
        self.assertEqual(run.calls[0][0], ["gh", "api", "rate_limit"])

    def test_a_spent_core_limit_comes_back_at_its_reset(self):
        run = FakeRun((0, rate_limit(core_remaining=0, core_reset=6000), ""))
        self.assertEqual(spent_limit_reset(run=run), 6000)

    def test_when_both_are_spent_the_later_reset_wins(self):
        run = FakeRun((0, rate_limit(graphql_remaining=0, graphql_reset=7000,
                                     core_remaining=0, core_reset=6000), ""))
        self.assertEqual(spent_limit_reset(run=run), 7000)

    def test_none_spent_is_none(self):
        # search is spent in this answer too; only graphql and core stop Kandu.
        self.assertIsNone(spent_limit_reset(run=FakeRun((0, rate_limit(), ""))))

    def test_an_unreadable_answer_is_none(self):
        for out in ("", "not json", "{}", json.dumps({"resources": None}), json.dumps([1]),
                    json.dumps({"resources": {"graphql": {"remaining": 0}}})):
            with self.subTest(out=out):
                self.assertIsNone(spent_limit_reset(run=FakeRun((1, out, "HTTP 502"))))

    def test_gh_that_cannot_run_is_none(self):
        def broken(argv, **kwargs):
            raise OSError("gh not found")
        self.assertIsNone(spent_limit_reset(run=broken))

    def test_gh_that_times_out_is_none(self):
        def slow(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
        self.assertIsNone(spent_limit_reset(run=slow))

    def test_a_gate_with_github_s_answer_waits_until_the_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = FakeRun((0, rate_limit(graphql_remaining=0, graphql_reset=5000), ""))
            gate = Gate(Path(tmp) / "backoff.json", clock=lambda: 1000.0, lookup=lambda: spent_limit_reset(run=run))
            self.assertGreaterEqual(gate.refused("API rate limit already exceeded")["until"], 5000)
