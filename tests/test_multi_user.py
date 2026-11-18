"""A project several people share, each with a Kandu of their own (`multi_user`, DESIGN §3)."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from kandu.config import Board, ConfigError, load_config, set_project_values
from kandu.github import BoardItem, GitHub, for_assignee
from kandu.mirror import Mirror

EXAMPLES = Path(__file__).resolve().parent.parent / "config.example"


def card(number, assignees):
    return BoardItem("acme-org/website", number, f"Task {number}", f"https://github.com/acme-org/website/issues/{number}",
                     "Backlog", ["agent"], "", assignees=assignees)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        shutil.copy(EXAMPLES / "kandu.example.yaml", self.local / "kandu.yaml")
        shutil.copy(EXAMPLES / "projects.example.yaml", self.local / "projects.yaml")
        self.account = load_config(self.local).settings.github.account

    def edit(self, change):
        path = self.local / "projects.yaml"
        data = yaml.safe_load(path.read_text())
        change(data["projects"])
        path.write_text(yaml.safe_dump(data, sort_keys=False))

    def problems(self):
        with self.assertRaises(ConfigError) as caught:
            load_config(self.local)
        return "\n".join(caught.exception.problems)

    def test_off_by_default_so_assignees_do_not_matter(self):
        self.assertEqual([p.board.assignee for p in load_config(self.local).projects], ["", ""])

    def test_on_the_board_answers_only_to_the_install_s_own_account(self):
        def on(projects):
            for p in projects:
                p["multi_user"] = True
        self.edit(on)
        self.assertEqual({p.board.assignee for p in load_config(self.local).projects}, {self.account})

    def test_projects_sharing_a_board_must_agree(self):
        self.edit(lambda projects: projects[1].update(multi_user=True))
        self.assertIn("must all be multi_user or none", self.problems())

    def test_the_assignee_is_never_written_by_hand(self):
        self.edit(lambda projects: projects[0]["board"].update(assignee="someone"))
        self.assertIn("set multi_user: true instead", self.problems())

    def test_it_needs_a_board(self):
        def no_board(projects):
            projects[0].pop("board")
            projects[0]["multi_user"] = True
        self.edit(no_board)
        self.assertIn("multi_user: needs a board", self.problems())

    def test_the_dashboard_can_turn_it_on(self):
        def same_family(projects):
            projects[1]["family"] = "other"
            projects[1].pop("board")
        self.edit(same_family)
        set_project_values("website", {"multi_user": True}, self.local)
        self.assertEqual(load_config(self.local).projects[0].board.assignee, self.account)


class FilterTest(unittest.TestCase):
    def test_only_cards_assigned_to_the_login_without_case(self):
        items = [card(1, ["Alice"]), card(2, ["bob"]), card(3, []), card(4, ["bob", "alice"])]
        self.assertEqual([i.number for i in for_assignee(Board("o", 1, assignee="alice"), items)], [1, 4])

    def test_a_board_with_no_assignee_sees_every_card(self):
        items = [card(1, ["alice"]), card(2, [])]
        self.assertEqual(for_assignee(Board("o", 1), items), items)

    def test_the_board_read_keeps_each_card_s_assignees(self):
        node = {"id": "card-1", "status": {"name": "Backlog"},
                "content": {"number": 1, "title": "T", "url": "u", "body": "", "closedAt": None,
                            "repository": {"nameWithOwner": "o/r"}, "labels": {"nodes": []},
                            "assignees": {"nodes": [{"login": "alice"}]}}}
        answer = {"data": {"repositoryOwner": {"projectV2": {"id": "P", "fields": {"nodes": []}, "items": {
            "nodes": [node, {**node, "id": "card-2", "content": {**node["content"], "number": 2,
                                                                  "assignees": {"nodes": []}}}],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}}
        github = GitHub(lambda args: json.dumps(answer))
        items, _ = github.read_board(Board("o", 1))
        self.assertEqual([i.assignees for i in items], [["alice"], []])
        self.assertEqual([i.number for i in github.board_items(Board("o", 1, assignee="alice"))], [1])

    def test_the_copy_filters_too(self):
        class Remote:
            def read_board(self, board):
                return [card(1, ["alice"]), card(2, ["bob"])], {"project_id": "P", "fields": {}}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mirror = Mirror(Remote(), tmp.name)
        self.assertEqual([i.number for i in mirror.board_items(Board("o", 1, assignee="bob"))], [2])
        self.assertEqual([i.number for i in mirror.board_items(Board("o", 1))], [1, 2])


class CreateTest(unittest.TestCase):
    def test_a_new_issue_is_assigned_so_its_install_still_sees_it(self):
        calls = []
        def gh(args):
            calls.append(args)
            return json.dumps({"html_url": "https://github.com/o/r/issues/5"})
        GitHub(gh).create_issue("o/r", "T", "B", assignee="alice")
        self.assertIn("assignees[]=alice", calls[-1])
        GitHub(gh).create_issue("o/r", "T", "B")
        self.assertFalse(any(a.startswith("assignees[]") for a in calls[-1]))


if __name__ == "__main__":
    unittest.main()
