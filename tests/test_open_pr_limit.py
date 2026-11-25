"""A project's open-PR limit: at it, no task of the project starts from `Ready` and none is sized."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from kandu import add, openprs
from kandu.config import ConfigError, load_config
from kandu.dashboard import Api
from kandu.github import PR_FIELDS, PR_NODE, GitHub
from kandu.mirror import Mirror
from test_supervisor import ACCOUNT, BLOCKER, EXAMPLES, KEY, REPO, SHA1, SHA2, Base, report
from test_github import FakeGh
from test_mirror import FakeRemote, pr as mirror_pr

ROOT = Path(__file__).resolve().parent.parent
LOOKS = ("dashboard.html", "classic.html", "classic-new.html")


def other_pr(number, author, state="OPEN"):
    """A PR on the repo that no task on the board names: a person's, or one Kandu opened for an older task."""
    return {"number": number, "state": state, "author": {"login": author}, "headRefName": f"someone/{number}",
            "url": f"https://github.com/{REPO}/pull/{number}"}


class LimitBase(Base):
    def setUp(self):
        super().setUp()
        self.project = self.config.projects[0]
        self.project.deploy = None
        self.others = []
        theirs = lambda repo: [p for r, p in self.others if r == repo and p["state"] == "OPEN"]
        ours = lambda repo: [p for (r, _), p in self.github.prs.items() if r == repo and p["state"] == "OPEN"]
        self.github.open_prs = lambda repo: ours(repo) + theirs(repo)
        opens = self.github.open_pr

        def by_the_account(*args, **kwargs):  # a PR Kandu opens is by its account
            pr = opens(*args, **kwargs)
            pr["author"] = {"login": ACCOUNT}
            return pr
        self.github.open_pr = by_the_account

    def an_open_pr(self, number=40, author=ACCOUNT):
        pr = other_pr(number, author)
        self.others.append((REPO, pr))
        return pr


class WithoutALimitTest(LimitBase):
    def test_a_ready_task_starts_beside_an_open_pr_as_before(self):
        self.an_open_pr()
        self.github.add(1)
        self.runner.steps = [self.worker_makes_pr()]
        self.cycle()
        self.assertEqual(len(self.runner.runs), 1)
        self.assertEqual(self.events(openprs.HELD), [])


class AtTheLimitTest(LimitBase):
    def setUp(self):
        super().setUp()
        self.project.max_open_prs = 1

    def test_no_ready_task_starts_and_the_hold_is_logged_once(self):
        self.an_open_pr(author=ACCOUNT.upper())  # the account is compared without case
        self.github.add(1)
        self.cycle()
        self.cycle()
        self.assertEqual(self.runner.runs, [])
        self.assertEqual(self.github.status(1), "Ready")
        held = self.events(openprs.HELD)
        self.assertEqual([(e["project"], e["prs"], e["limit"]) for e in held], [("website", [f"{REPO}#40"], 1)])

    def test_no_backlog_task_is_sized(self):
        self.github.on_add = lambda key, size: None
        self.an_open_pr()
        self.github.add(2, status="Backlog")
        self.runner.steps = ["a splitter's reply"]
        self.cycle()
        self.assertEqual(self.runner.steps, ["a splitter's reply"])  # no splitter ran
        self.assertEqual(self.events("phase_changed"), [])

    def test_a_pr_by_another_author_does_not_count(self):
        self.an_open_pr(author="someone-else")
        self.github.add(1)
        self.runner.steps = [self.worker_makes_pr()]
        self.cycle()
        self.assertEqual(len(self.runner.runs), 1)
        self.assertEqual(self.events(openprs.HELD), [])

    def test_once_the_pr_is_merged_the_next_task_starts_in_the_next_cycle_and_the_end_is_logged_once(self):
        pr = self.an_open_pr()
        self.github.add(1)
        self.cycle()
        self.assertEqual(self.runner.runs, [])
        pr["state"] = "MERGED"
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]
        self.cycle()
        self.assertEqual(len(self.runner.runs), 2)
        self.assertEqual([e["project"] for e in self.events(openprs.FREED)], ["website"])
        self.cycle()  # task 1's new PR, which waits for the user, holds the project again
        self.assertEqual(len(self.events(openprs.FREED)), 1)
        self.assertEqual([e["prs"] for e in self.events(openprs.HELD)], [[f"{REPO}#40"], [f"{REPO}#7"]])

    def test_a_pr_opened_this_cycle_holds_the_next_ready_task(self):
        self.project.max_parallel_workers = 1
        self.github.add(1)
        self.github.add(2)
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]
        self.cycle()
        self.assertEqual(len(self.runner.runs), 2)  # task 1's worker and review; task 2 never started
        self.assertEqual(self.github.status(2), "Ready")
        self.assertEqual([e["prs"] for e in self.events(openprs.HELD)], [[f"{REPO}#7"]])

    def test_work_on_the_open_pr_still_runs(self):
        # The project's one open PR is this task's: its review, fix round, second review and merge gate go on.
        self.github.add(1, status="In review")
        self.github.open_pr(REPO, 1, SHA1)
        self.github.add(2)
        fixed = [{"n": 1, "result": "fixed", "body": "ok"}]
        self.runner.steps = [report(SHA1, [BLOCKER]), self.worker_pushes(SHA2), report(SHA2, earlier=fixed)]
        self.cycle()
        self.assertEqual(self.runner.steps, [])
        self.assertEqual([e["head_sha"] for e in self.events("review_finished")], [SHA1, SHA2])
        self.assertEqual([e["task"] for e in self.decisions("approved")], [KEY])  # the merge gate looked at it
        self.assertEqual(self.github.status(2), "Ready")


class ReworkWhileHeldTest(LimitBase):
    def setUp(self):
        super().setUp()
        self.project.max_open_prs = 1
        self.config.settings.supervisor.queen = True
        self.config.settings.supervisor.queen_max_retries = 1
        self.github.add(1, status="Blocked", body="## Blocked by\nnone\n")
        self.store.record("task_finished", task=KEY, project="website", outcome="blocked")

    def test_the_queens_rework_on_the_open_pr_runs(self):
        self.github.open_pr(REPO, 1, SHA1)
        self.github.add(2)
        step = {"task": KEY, "action": "rework", "note": "Rename the helper."}
        refused, _ = self.sup._carry_out([step], [])
        self.assertEqual(refused, [])
        none = json.dumps({"brief": "Read the rework.", "decisions": []})
        self.runner.steps = [none, self.worker_pushes(SHA2), report(SHA2), none]
        self.cycle()
        self.assertEqual(self.runner.steps, [])
        self.assertIn("Rename the helper.", self.runner.runs[1].prompt)
        self.assertEqual(self.github.status(2), "Ready")


class CountTest(unittest.TestCase):
    def project(self, repos):
        return type("P", (), {"repos": repos, "max_open_prs": 1})()

    def test_only_open_prs_by_the_account_on_the_projects_repos_count(self):
        prs = {"o/a": [other_pr(1, "Me"), other_pr(2, "you"), {"number": 3, "author": None}],
               "o/b": [other_pr(4, "me")], "o/c": [other_pr(5, "me")]}
        github = type("G", (), {"open_prs": lambda self, repo: prs[repo]})()
        self.assertEqual(openprs.open_prs(github, self.project(["o/a", "o/b"]), "me"), ["o/a#1", "o/b#4"])


class ConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        shutil.copy(EXAMPLES / "kandu.example.yaml", self.local / "kandu.yaml")
        shutil.copy(EXAMPLES / "projects.example.yaml", self.local / "projects.yaml")

    def with_limit(self, value):
        data = yaml.safe_load((self.local / "projects.yaml").read_text(encoding="utf-8"))
        data["projects"][0]["max_open_prs"] = value
        (self.local / "projects.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        return load_config(self.local)

    def test_no_limit_by_default(self):
        self.assertTrue(all(p.max_open_prs is None for p in load_config(self.local).projects))

    def test_a_whole_number_of_at_least_one_is_taken(self):
        self.assertEqual(self.with_limit(2).projects[0].max_open_prs, 2)

    def test_anything_else_is_refused(self):
        for value in (0, -1, 1.5, "two", True):
            with self.subTest(value=value), self.assertRaises(ConfigError) as caught:
                self.with_limit(value)
            self.assertIn("max_open_prs", str(caught.exception))

    def test_the_example_shows_it_with_a_comment(self):
        text = (EXAMPLES / "projects.example.yaml").read_text(encoding="utf-8")
        line = next(l for l in text.splitlines() if "max_open_prs" in l)
        self.assertIn("#", line)

    def test_kandu_add_writes_it_only_when_given(self):
        board = type("B", (), {"owner": "o", "number": 1, "columns": {}})()
        fields = {"name": "n", "family": "f", "repo": "o/r", "run": "clone", "machine": "m", "path": "p", "test": "t"}
        self.assertNotIn("max_open_prs", add._entry(fields, board))
        self.assertEqual(add._entry({**fields, "max_open_prs": "1"}, board)["max_open_prs"], 1)
        with self.assertRaises(add.AddError):
            add._entry({**fields, "max_open_prs": "one"}, board)


class CopyTest(unittest.TestCase):
    def test_a_pr_is_read_with_its_author(self):
        self.assertIn("author { login }", PR_NODE)
        self.assertIn("author", PR_FIELDS.split(","))
        node = {"number": 7, "title": "t", "state": "OPEN", "url": "u", "body": "", "changedFiles": 1,
                "headRefName": "kandu/3-a", "headRefOid": "abc", "isDraft": False, "isCrossRepository": False,
                "mergeable": "MERGEABLE", "updatedAt": "x", "author": {"login": "me"}, "labels": {"nodes": []},
                "comments": {"nodes": []}, "files": {"nodes": []}, "commits": {"nodes": []},
                "reviewThreads": {"nodes": []}}
        reply = {"data": {"repository": {"defaultBranchRef": {"name": "main"},
                                         "issues": {"pageInfo": {"hasNextPage": False}, "nodes": []},
                                         "open": {"pageInfo": {"hasNextPage": False}, "nodes": [node]},
                                         "closed": {"nodes": [dict(node, number=6, state="MERGED", author=None)]}}}}
        found = GitHub(FakeGh(reply)).read_repo("o/r")
        self.assertEqual([p["author"] for p in found["prs"]], [{"login": "me"}, {}])

    def test_the_copy_names_its_open_prs_without_asking_github(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        remote = FakeRemote()
        remote.prs = {7: mirror_pr(7), 8: mirror_pr(8, state="MERGED"), 9: mirror_pr(9)}
        mirror = Mirror(remote, tmp.name, "me", clock=lambda: 1000.0)
        self.assertEqual(mirror.open_prs("acme/app"), [])  # nothing copied yet: nothing to count, and no read
        self.assertEqual(remote.calls, [])
        mirror.refresh_repo("acme/app")
        del remote.prs[9]  # closed on GitHub: the next read marks it stale
        mirror.refresh_repo("acme/app")
        calls = len(remote.calls)
        self.assertEqual([p["number"] for p in mirror.open_prs("acme/app")], [7])
        self.assertEqual(len(remote.calls), calls)


class DashboardTest(LimitBase):
    def board(self):
        board = Api.__new__(Api)
        board.config, board.store, board.github = self.config, self.store, self.github
        return board

    def test_it_names_the_open_prs_while_the_project_waits(self):
        self.project.max_open_prs = 1
        self.assertEqual(self.board()._open_prs_held(), {})
        self.an_open_pr()
        held = self.board()._open_prs_held()
        self.assertEqual(held, {"website": {"limit": 1, "prs": [f"{REPO}#40"]}})

    def test_a_project_without_a_limit_is_never_held(self):
        self.an_open_pr()
        self.assertEqual(self.board()._open_prs_held(), {})

    def test_every_look_shows_the_notice_in_english_and_persian(self):
        for look in LOOKS:
            page = (ROOT / "kandu" / look).read_text(encoding="utf-8")
            with self.subTest(look=look):
                self.assertIn('id="openprsheld"', page)
                self.assertIn("renderOpenPrsHeld(s.open_prs_held);", page)
                self.assertEqual(page.count('"open_prs_held_title":'), 2)
                self.assertEqual(page.count(f'"{openprs.HELD}":'), 1)  # the log's Persian name
                self.assertEqual(page.count(f'"{openprs.FREED}":'), 1)


if __name__ == "__main__":
    unittest.main()
