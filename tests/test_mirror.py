import json
import tempfile
import unittest
from copy import deepcopy
from types import SimpleNamespace

from kandu.config import Board
from kandu.github import BoardItem, GitHubError
from kandu.mirror import FRESH_SECONDS, MISSING_CARD_TRIES, Mirror
from kandu.sync import sync

BOARD = Board(owner="acme", number=1, columns={"Ready": "Todo"})
REPO = "acme/app"
URL = f"https://github.com/{REPO}/issues/3"
ACCOUNT = "kandu-bot"
META = {"project_id": "P", "fields": {
    "Status": {"id": "FS", "options": {"Backlog": "s-b", "Todo": "s-r", "In progress": "s-p", "Done": "s-d"}},
    "Size": {"id": "FZ", "options": {"Small": "z-s", "Medium": "z-m"}},
    "Deadline": {"id": "FD", "options": {}}}}


def pr(number=7, issue=3, state="OPEN", head="abc", comments=()):
    return {"number": number, "title": "t", "state": state, "url": "u", "body": "", "changedFiles": 1,
            "headRefName": f"kandu/{issue}-task", "headRefOid": head, "isDraft": False, "isCrossRepository": False,
            "mergeable": "MERGEABLE", "labels": [], "comments": list(comments), "files": [], "statusCheckRollup": [],
            "reviewThreads": []}


class FakeRemote:
    """GitHub as the copy sees it: what it holds, and every call made to it."""

    def __init__(self):
        self.items = [BoardItem(REPO, 3, "Task 3", URL, "Backlog", ["agent"], "body", item_id="card-3")]
        self.issues = {3: {"number": 3, "title": "Task 3", "body": "body", "url": URL, "state": "OPEN",
                           "labels": ["agent"], "comments": [{"author": {"login": "user"}, "body": "a note",
                                                              "createdAt": "2026-11-01T10:00:00Z"}]}}
        self.prs = {}
        self.calls = []
        self.fail = None  # a function of the call's name that may raise

    def _call(self, name, *args):
        self.calls.append((name, *args))
        if self.fail is not None:
            self.fail(name, args)

    def read_board(self, board):
        self._call("read_board")
        return deepcopy(self.items), META

    def read_repo(self, repo, since=None):
        self._call("read_repo", since)
        return {"default_branch": "main", "issues": deepcopy(list(self.issues.values())),
                "prs": deepcopy(list(self.prs.values()))}

    def read_issue(self, repo, number):
        self._call("read_issue", number)
        return deepcopy(self.issues.get(number))

    def read_pr(self, repo, number):
        self._call("read_pr", number)
        return deepcopy(self.prs.get(number))

    def edit_cards(self, project_id, edits):
        self._call("edit_cards", list(edits))
        names = {v: k for k, v in META["fields"]["Status"]["options"].items()}
        for item_id, field, kind, value in edits:
            item = next(i for i in self.items if i.item_id == item_id)
            if field == "FS":
                item.status = names[value]

    def add_card(self, project_id, url):
        self._call("add_card", url)
        return "card-new"

    def comment_issue(self, repo, number, body):
        self._call("comment", number, body)
        for kind in (self.prs, self.issues):
            if number in kind:
                kind[number]["comments"].append({"author": {"login": ACCOUNT}, "body": body})

    def close_issue(self, repo, number):
        self._call("close", number)
        self.issues[number]["state"] = "CLOSED"

    def edit_issue(self, repo, number, title, body):
        self._call("edit", number)

    def add_label(self, repo, number, label, description):
        self._call("label", number, label)

    def create_issue(self, repo, title, body, by_hand=False, tokens=False):
        self._call("create_issue", title)
        return f"https://github.com/{repo}/issues/9"

    def merge_pr(self, repo, number, head_sha):
        self._call("merge", number, head_sha)
        self.prs[number]["state"] = "MERGED"


class MirrorTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = folder.name
        self.now = 1000.0
        self.remote = FakeRemote()
        self.mirror = self.open()

    def open(self):
        return Mirror(self.remote, self.root, ACCOUNT, clock=lambda: self.now, pause=lambda seconds: None)

    def names(self):
        return [call[0] for call in self.remote.calls]

    def status(self, mirror=None):
        return (mirror or self.mirror).board_items(BOARD)[0].status

    def dropped(self):
        try:
            with open(f"{self.root}/dropped.jsonl", encoding="utf-8") as log:
                return [json.loads(line) for line in log]
        except OSError:
            return []

    # Reading.

    def test_every_process_reads_the_same_copy(self):
        self.mirror.board_items(BOARD)
        self.mirror.issue_text(REPO, 3)
        self.open().board_items(BOARD)
        self.open().issue_is_open(REPO, 3)
        self.assertEqual(self.names(), ["read_board", "read_repo"])

    def test_a_boards_columns_and_fields_come_from_the_copy(self):
        self.mirror.board_items(BOARD)
        calls = len(self.remote.calls)
        layout = self.open().board_layout(BOARD)
        self.assertEqual(layout["columns"], ["Backlog", "Ready", "In progress", "Done"])  # "Todo" is this board's "Ready"
        self.assertEqual(layout["fields"]["Status"]["options"]["Todo"], "s-r")  # what a card change on GitHub needs
        self.assertEqual(layout["fields"]["Deadline"], {"id": "FD", "options": {}})
        self.assertEqual(layout["project_id"], "P")
        self.assertEqual(len(self.remote.calls), calls)

    def test_a_board_the_copy_lacks_is_read_once_for_its_layout(self):
        self.mirror.board_layout(BOARD)
        self.mirror.board_layout(BOARD)
        self.assertEqual(self.names(), ["read_board"])

    def test_an_issue_the_copy_lacks_is_read_once(self):
        self.mirror.issue_is_open(REPO, 3)
        self.remote.issues[4] = dict(self.remote.issues[3], number=4, state="CLOSED")
        self.assertFalse(self.mirror.issue_is_open(REPO, 4))
        self.assertFalse(self.open().issue_is_open(REPO, 4))
        self.assertEqual(self.names(), ["read_repo", "read_issue"])

    def test_a_missing_issue_is_an_error(self):
        with self.assertRaises(GitHubError):
            self.mirror.issue_text(REPO, 99)

    def test_a_repo_is_read_again_only_for_what_changed(self):
        self.mirror.default_branch(REPO)
        self.now += 600
        self.mirror.refresh_repo(REPO)
        since = self.remote.calls[-1][1]
        self.assertTrue(since.endswith("Z"))
        self.assertLess(since, "1970-01-01T00:25:00Z")  # a little before the last read

    def test_a_young_copy_is_not_read_again(self):
        config = SimpleNamespace(projects=[SimpleNamespace(board=BOARD, repos=[REPO])])
        self.mirror.refresh(config)
        self.now += FRESH_SECONDS - 1
        self.mirror.refresh(config)
        self.now += 1
        self.mirror.refresh(config)
        self.assertEqual(self.names(), ["read_board", "read_repo"] * 2)

    def test_a_refused_read_keeps_the_copy_and_goes_on(self):
        config = SimpleNamespace(projects=[SimpleNamespace(board=BOARD, repos=[REPO])])
        self.mirror.refresh(config)

        def board_down(name, args):
            if name == "read_board":
                raise GitHubError("HTTP 502")

        self.remote.fail = board_down
        failed = self.mirror.refresh(config, max_age=0)
        self.assertEqual(len(failed), 1)
        self.assertEqual(self.status(), "Backlog")
        self.assertEqual(self.names()[-1], "read_repo")

    def test_find_pr_and_companions_come_from_the_copy(self):
        self.remote.prs = {7: pr(), 8: pr(8, 4) | {"headRefName": "kandu/4-task"}, 9: pr(9, 3, state="CLOSED")}
        self.assertEqual(self.mirror.find_pr(REPO, 3)["number"], 9)
        found = self.mirror.find_pr(REPO, 4)
        self.assertEqual(self.mirror.companions(REPO, found, [REPO]), [])
        self.remote.prs[7]["headRefName"] = "kandu/3-add-field"
        self.mirror.refresh_repo(REPO)
        ours = pr(1) | {"headRefName": "kandu/1-add-field"}
        self.assertEqual(self.mirror.companions(REPO, ours, [REPO]), [(REPO, 7)])
        self.assertEqual(self.names(), ["read_repo", "read_repo"])

    def test_a_pr_gone_from_the_open_ones_is_read_alone(self):
        self.remote.prs = {7: pr()}
        self.mirror.find_pr(REPO, 3)
        self.remote.prs = {}
        self.mirror.refresh_repo(REPO)
        self.remote.prs = {7: pr(state="MERGED")}
        self.assertEqual(self.mirror.pr(REPO, 7)["state"], "MERGED")
        self.assertEqual(self.names()[-1], "read_pr")

    # The queue.

    def test_a_card_change_shows_at_once_and_goes_later(self):
        self.mirror.set_status(BOARD, URL, "Ready")
        self.assertEqual(self.status(self.open()), "Ready")
        self.assertEqual(self.names(), ["read_board"])
        self.assertEqual(self.mirror.send(), (1, 0, 0))
        self.assertEqual(self.remote.calls[-1], ("edit_cards", [("card-3", "FS", "option", "s-r")]))
        self.assertEqual(self.mirror.outbox(), [])

    def test_the_last_change_to_a_field_is_the_one_sent(self):
        self.mirror.set_status(BOARD, URL, "Ready")
        self.mirror.set_status(BOARD, URL, "In progress")
        self.mirror.set_field(BOARD, URL, "Size", "Small")
        entries = self.mirror.outbox()
        self.assertEqual([(e["field"], e["value"], e["before"]) for e in entries],
                         [("Status", "In progress", "Backlog"), ("Size", "Small", "")])
        self.mirror.send()
        self.assertEqual(len(self.remote.calls[-1][1]), 2)  # one request for both

    def test_the_local_change_wins(self):
        self.mirror.set_status(BOARD, URL, "Ready")
        self.remote.items[0].status = "Done"  # changed on GitHub meanwhile
        self.mirror.refresh_board(BOARD)
        self.assertEqual(self.status(), "Ready")
        self.assertEqual(len(self.mirror.outbox()), 1)
        self.assertEqual(self.mirror.send(), (1, 0, 0))
        self.assertEqual(self.dropped(), [])

    def test_a_change_waiting_survives_a_read(self):
        self.mirror.set_status(BOARD, URL, "Ready")
        self.mirror.refresh_board(BOARD)
        self.assertEqual(self.status(), "Ready")
        self.assertEqual(len(self.mirror.outbox()), 1)

    def test_a_change_github_already_holds_leaves_quietly(self):
        self.mirror.set_status(BOARD, URL, "Ready")
        self.remote.items[0].status = "Ready"
        self.mirror.refresh_board(BOARD)
        self.assertEqual((self.mirror.outbox(), self.dropped()), ([], []))

    def test_a_refusal_that_passes_keeps_the_change(self):
        self.mirror.set_status(BOARD, URL, "Ready")
        self.remote.fail = lambda name, args: (_ for _ in ()).throw(GitHubError("HTTP 503"))
        self.assertEqual(self.mirror.send(), (0, 1, 0))
        self.assertEqual(self.mirror.outbox()[0]["tries"], 1)
        self.remote.fail = None
        self.assertEqual(self.mirror.send(), (1, 0, 0))

    def test_one_bad_change_does_not_hold_back_the_rest(self):
        other = "https://github.com/acme/app/issues/4"
        self.remote.items.append(BoardItem(REPO, 4, "Task 4", other, "Backlog", ["agent"], "", item_id="card-4"))
        self.mirror.set_status(BOARD, URL, "Ready")
        self.mirror.set_status(BOARD, other, "Ready")

        def card_4_refused(name, args):
            if name == "edit_cards" and any(edit[0] == "card-4" for edit in args[0]):
                raise GitHubError("Could not resolve to a node")

        self.remote.fail = card_4_refused
        self.assertEqual(self.mirror.send(), (1, 0, 1))
        self.assertEqual(self.remote.items[0].status, "Todo")  # the board's own name for Ready
        self.assertEqual(len(self.dropped()), 1)

    def test_an_unknown_option_is_dropped(self):
        self.mirror.set_field(BOARD, URL, "Size", "Huge")
        self.assertEqual(self.mirror.send(), (0, 0, 1))

    def test_a_card_not_on_the_board_waits_then_is_dropped(self):
        stranger = "https://github.com/acme/app/issues/50"
        self.mirror.set_status(BOARD, stranger, "Ready")
        for _ in range(MISSING_CARD_TRIES - 1):
            self.assertEqual(self.mirror.send(), (0, 1, 0))
        self.assertEqual(self.mirror.send(), (0, 0, 1))

    def test_a_dates_and_an_empty_value(self):
        self.mirror.set_field(BOARD, URL, "Deadline", "2026-12-01")
        self.mirror.set_field(BOARD, URL, "Size", "")
        self.mirror.send()
        self.assertEqual(self.remote.calls[-1][1], [("card-3", "FD", "date", "2026-12-01"), ("card-3", "FZ", "clear", None)])

    def test_a_comment_on_a_pr_shows_at_once_and_only_once(self):
        self.remote.prs = {7: pr()}
        self.mirror.comment_pr(REPO, 7, "Review round 1")
        self.assertEqual(self.mirror.pr(REPO, 7)["comments"][0]["author"]["login"], ACCOUNT)
        self.mirror.refresh_repo(REPO)
        self.assertEqual(len(self.mirror.pr(REPO, 7)["comments"]), 1)
        self.mirror.send()
        self.mirror.refresh_repo(REPO)
        self.assertEqual(len(self.mirror.pr(REPO, 7)["comments"]), 1)
        self.assertIn(("comment", 7, "Review round 1"), self.remote.calls)

    def test_a_closed_issue_is_closed_in_the_copy_before_it_is_sent(self):
        self.mirror.board_items(BOARD)
        self.mirror.close_issue(REPO, 3)
        self.assertFalse(self.mirror.issue_is_open(REPO, 3))
        self.assertTrue(self.mirror.board_items(BOARD)[0].closed_at)
        self.mirror.refresh_repo(REPO)
        self.assertFalse(self.mirror.issue_is_open(REPO, 3))
        self.mirror.send()
        self.assertEqual(self.remote.issues[3]["state"], "CLOSED")

    def test_marking_a_pr_twice_queues_it_once(self):
        self.remote.prs = {7: pr()}
        self.mirror.mark_pr(REPO, self.mirror.pr(REPO, 7))
        self.mirror.mark_pr(REPO, self.mirror.pr(REPO, 7))
        self.assertEqual(len(self.mirror.outbox()), 1)

    # One item, as `kandu.show` prints it.

    def test_an_issue_comes_from_the_copy_with_its_comments(self):
        self.mirror.refresh_repo(REPO)
        calls = len(self.remote.calls)
        item = self.open().item(REPO, 3)
        self.assertEqual((item["kind"], item["title"]), ("issue", "Task 3"))
        self.assertEqual([c["body"] for c in item["comments"]], ["a note"])
        self.assertEqual(len(self.remote.calls), calls)

    def test_a_pr_comes_from_the_copy_with_its_threads_and_checks(self):
        self.remote.prs = {7: dict(pr(comments=[{"author": {"login": "user"}, "body": "looks fine"}]),
                                   statusCheckRollup=[{"__typename": "CheckRun", "name": "ci", "conclusion": "SUCCESS"}],
                                   reviewThreads=[{"resolved": False, "resolved_by_other": False, "path": "a.py",
                                                   "line": 2, "comments": [{"author": {"login": "r"}, "body": "why?"}]}])}
        self.mirror.refresh_repo(REPO)
        calls = len(self.remote.calls)
        item = self.open().item(REPO, 7)
        self.assertEqual(item["kind"], "pr")
        self.assertEqual(item["comments"][0]["body"], "looks fine")
        self.assertEqual(item["reviewThreads"][0]["comments"][0]["body"], "why?")
        self.assertEqual(item["statusCheckRollup"][0]["conclusion"], "SUCCESS")
        self.assertEqual(len(self.remote.calls), calls)

    def test_an_item_the_copy_lacks_is_read_once_and_kept(self):
        self.mirror.refresh_repo(REPO)
        self.remote.issues[4] = dict(self.remote.issues[3], number=4, title="Old")
        self.remote.prs[8] = pr(number=8, issue=4, state="MERGED")
        self.assertEqual(self.mirror.item(REPO, 4)["title"], "Old")
        self.assertEqual(self.mirror.item(REPO, 8)["kind"], "pr")
        calls = len(self.remote.calls)
        self.assertEqual(self.open().item(REPO, 4)["comments"][0]["body"], "a note")
        self.assertEqual(self.open().item(REPO, 8)["number"], 8)
        self.assertEqual(len(self.remote.calls), calls)
        self.assertEqual(self.names(), ["read_repo", "read_issue", "read_issue", "read_pr"])

    def test_an_issue_kept_without_comments_is_read_once(self):
        self.mirror.refresh_repo(REPO)
        path = f"{self.root}/repos/acme__app.json"
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        del data["issues"]["3"]["comments"]  # a copy written before issues kept their comments
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.assertEqual(self.mirror.item(REPO, 3)["comments"][0]["body"], "a note")
        self.mirror.item(REPO, 3)
        self.assertEqual(self.names(), ["read_repo", "read_issue"])

    def test_a_refresh_keeps_each_issues_blocked_by_relationships(self):
        blockers = [{"repo": REPO, "number": 2}, {"repo": "acme/lib", "number": 5}]
        self.remote.issues[3]["blocked_by"] = blockers
        self.remote.items[0].blocked_by = blockers
        self.remote.issues[4] = dict(self.remote.issues[3], number=4, url=URL[:-1] + "4", blocked_by=[])
        self.remote.items.append(BoardItem(REPO, 4, "Task 4", URL[:-1] + "4", "Backlog", ["agent"], "", item_id="c4"))
        self.mirror.refresh(SimpleNamespace(projects=[SimpleNamespace(board=BOARD, repos=[REPO])]), max_age=0)
        calls = len(self.remote.calls)
        mirror = self.open()
        cards = {item.number: item.blocked_by for item in mirror.board_items(BOARD)}
        self.assertEqual(cards, {3: blockers, 4: []})
        self.assertEqual(mirror.item(REPO, 3)["blocked_by"], blockers)
        self.assertEqual(mirror.item(REPO, 4)["blocked_by"], [])
        self.assertEqual(len(self.remote.calls), calls)

    def test_an_issues_text_carries_its_kept_relationships(self):
        blockers = [{"repo": REPO, "number": 2}]
        self.remote.issues[3]["blocked_by"] = blockers
        self.mirror.refresh_repo(REPO)
        calls = len(self.remote.calls)
        self.assertEqual(self.open().issue_text(REPO, 3)["blocked_by"], blockers)
        self.assertEqual(len(self.remote.calls), calls)

    def test_a_board_read_gives_a_kept_issue_its_cards_new_blocked_by(self):
        self.mirror.refresh_repo(REPO)
        self.mirror.refresh_board(BOARD)
        blockers = [{"repo": REPO, "number": 2}]
        self.remote.items[0].blocked_by = blockers  # a new relationship on GitHub; the issue's updatedAt stays
        self.remote.read_repo = lambda repo, since=None: {"default_branch": "main", "issues": [], "prs": []}
        self.now += 300
        self.mirror.refresh(SimpleNamespace(projects=[SimpleNamespace(board=BOARD, repos=[REPO])]), max_age=0)
        mirror = self.open()
        self.assertEqual(mirror.board_items(BOARD)[0].blocked_by, blockers)
        self.assertEqual(mirror.item(REPO, 3)["blocked_by"], blockers)

    def test_a_copy_kept_before_relationships_loads_with_none(self):
        self.remote.issues[3]["blocked_by"] = [{"repo": REPO, "number": 2}]
        self.remote.items[0].blocked_by = [{"repo": REPO, "number": 2}]
        self.mirror.refresh_repo(REPO)
        self.mirror.refresh_board(BOARD)
        for path, strip in ((f"{self.root}/repos/acme__app.json", lambda d: d["issues"]["3"]),
                            (f"{self.root}/boards/acme-1.json", lambda d: d["items"][0])):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            del strip(data)["blocked_by"]  # as a copy written before issues kept their relationships
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        calls = len(self.remote.calls)
        mirror = self.open()
        self.assertEqual(mirror.board_items(BOARD)[0].blocked_by, [])
        self.assertEqual(mirror.item(REPO, 3)["blocked_by"], [])
        self.assertEqual(mirror.issue_text(REPO, 3)["title"], "Task 3")
        self.assertEqual(len(self.remote.calls), calls)

    def test_an_item_github_does_not_have_is_an_error(self):
        with self.assertRaises(GitHubError):
            self.mirror.item(REPO, 99)

    def test_a_comment_on_an_issue_shows_at_once_and_only_once(self):
        self.mirror.comment_issue(REPO, 3, "Blocked: needs a key")
        self.assertEqual(self.mirror.item(REPO, 3)["comments"][-1]["author"]["login"], ACCOUNT)
        self.mirror.send()
        self.mirror.refresh_repo(REPO)
        self.assertEqual([c["body"] for c in self.mirror.item(REPO, 3)["comments"]], ["a note", "Blocked: needs a key"])

    # Writes that go at once.

    def test_a_new_issue_goes_at_once_and_its_card_too(self):
        url = self.mirror.create_issue(REPO, "New", "Body")
        self.assertEqual(self.mirror.issue_text(REPO, 9)["title"], "New")
        self.mirror.add_to_board(BOARD, url)
        self.mirror.set_status(BOARD, url, "Backlog")
        self.assertEqual(self.names(), ["create_issue", "read_repo", "read_board", "add_card"])
        card = next(i for i in self.mirror.board_items(BOARD) if i.url == url)
        self.assertEqual((card.item_id, card.status, card.title), ("card-new", "Backlog", "New"))

    def test_a_merge_goes_at_once_and_is_read_back(self):
        self.remote.prs = {7: pr()}
        self.mirror.merge_pr(REPO, 7, "abc")
        self.assertEqual(self.mirror.pr(REPO, 7)["state"], "MERGED")
        self.assertEqual(self.names(), ["merge", "read_pr", "read_repo", "read_repo"])

    def test_a_refused_read_after_a_merge_does_not_undo_it(self):
        self.remote.prs = {7: pr()}
        self.mirror.find_pr(REPO, 3)
        self.remote.fail = lambda name, args: name.startswith("read") and (_ for _ in ()).throw(GitHubError("HTTP 502"))
        self.mirror.merge_pr(REPO, 7, "abc")
        self.assertEqual(self.remote.prs[7]["state"], "MERGED")

    # Sync.

    def test_sync_reads_before_it_sends(self):
        config = SimpleNamespace(projects=[SimpleNamespace(board=BOARD, repos=[REPO])])
        self.mirror.set_status(BOARD, URL, "Ready")
        self.remote.items[0].status = "Ready"  # GitHub already holds it, so nothing is sent
        self.assertIn("sent 0, waiting 0, dropped 0", sync(config, self.mirror))
        self.assertNotIn("edit_cards", self.names())

    def test_sync_sends_the_local_change_over_github(self):
        config = SimpleNamespace(projects=[SimpleNamespace(board=BOARD, repos=[REPO])])
        self.mirror.set_status(BOARD, URL, "Ready")
        self.remote.items[0].status = "Done"
        self.assertIn("sent 1, waiting 0, dropped 0", sync(config, self.mirror))
        self.assertIn("edit_cards", self.names())


if __name__ == "__main__":
    unittest.main()
