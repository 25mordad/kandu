import json
import unittest
from unittest import mock

from kandu import agents, boards, didban, queen, sizing, split, supervisor
from kandu.config import Board
from kandu.github import GitHub
from kandu.tasks import TaskError, check_size

from test_supervisor import PR_OPENED, REPO, SHA1, Base, ok, report


def body(criteria=1):
    boxes = "\n".join(f"- [ ] criterion {n}" for n in range(criteria))
    return (f"## Goal\nA goal.\n\n## Current behavior\nNow.\n\n## Acceptance criteria\n{boxes}\n\n## How to test\nRun it.\n\n"
            "## Out of scope\nNone.\n\n## Blocked by\nnone\n")


def part(title="One slice", size="Small", files=2, door="two-way", criteria=1):
    return {"title": title, "size": size, "files": files, "door": door, "body": body(criteria)}


def sized(size="Small", files=2, door="two-way", slices=()):
    return {"size": size, "files": files, "door": door, "reason": "It is what it is.", "slices": list(slices)}


LARGE = sized("Large", 40, "mixed", [part("First"), part("Second", door="one-way")])


class ScaleTest(unittest.TestCase):
    def test_the_criteria_are_counted_in_their_own_section(self):
        self.assertEqual(sizing.criteria_in(body(4) + "\n## Notes\n- [ ] not a criterion\n"), 4)

    def test_the_smallest_size_comes_from_the_facts(self):
        self.assertEqual(sizing.smallest(3, 5, "two-way"), "Small")
        self.assertEqual(sizing.smallest(3, 6, "two-way"), "Medium")
        self.assertEqual(sizing.smallest(6, 1, "two-way"), "Large")
        self.assertEqual(sizing.smallest(1, 16, "one-way"), "Large")

    def test_a_mixed_door_is_large_however_small(self):
        self.assertEqual(sizing.smallest(1, 1, "mixed"), "Large")

    def test_a_task_with_too_many_criteria_cannot_be_called_small(self):
        with self.assertRaises(TaskError):
            check_size(body(4), "Small")
        check_size(body(4), "Medium")
        check_size(body(9), "Large")


class ReportTest(unittest.TestCase):
    def test_a_fair_report_passes(self):
        split.validate_report(sized(), body())
        split.validate_report(LARGE, body())

    def test_only_json_is_a_report(self):
        for text in ("not json", "[1]"):
            with self.assertRaises(split.ReportError):
                split.parse_report(text)

    def test_it_cannot_call_a_big_task_small(self):
        for report in (sized(files=6), sized("Medium", 16), sized("Medium", 2, "mixed")):
            with self.assertRaises(split.ReportError):
                split.validate_report(report, body())

    def test_it_may_size_up_but_only_a_large_task_has_slices(self):
        split.validate_report(sized("Medium"), body())
        with self.assertRaises(split.ReportError):
            split.validate_report(sized(slices=[part(), part()]), body())

    def test_a_large_task_needs_slices_that_can_run(self):
        bad = ([], [part()], [part(), part(size="Large")], [part(), part(door="mixed")], [part(), part(files=6)],
               [part(), part(criteria=4)], [part(), {"title": "No body", "size": "Small", "files": 1, "door": "two-way"}],
               [part(), part() | {"body": "## Goal\nOnly this."}], [part(), part("عنوان")])
        for slices in bad:
            with self.subTest(slices=slices), self.assertRaises(split.ReportError):
                split.validate_report(sized("Large", 40, "mixed", slices), body())

    def test_blockers_are_replaced_in_place(self):
        text = split.with_blockers(body(), ["acme-org/website#3"])
        self.assertIn("## Blocked by\nacme-org/website#3\n", text)
        self.assertNotIn("none", text)


class SizingTest(Base):
    def setUp(self):
        super().setUp()
        self.github.on_add = lambda key, size: None

    def test_a_task_is_sized_once_and_read_only(self):
        self.github.add(1, "Backlog", body=body())
        self.runner.steps = [ok(json.dumps(sized("Medium")))]
        self.cycle()
        self.cycle()
        self.assertEqual(len(self.runner.runs), 1)
        self.assertNotIn("Edit", self.runner.runs[0].allowed)
        self.assertIn("Edit", self.runner.runs[0].denied)
        self.assertEqual(self.github.fields[-1][1:], ("Size", "Medium"))
        self.assertEqual(self.events("task_sized")[0]["run_id"][:2], "r-")
        self.assertEqual(self.events("run_finished")[0]["role"], "splitter")
        self.assertEqual(self.github.status(1), "Backlog")

    def test_a_large_task_waits_for_the_user(self):
        self.github.add(1, "Backlog", body=body())
        self.runner.steps = [ok(json.dumps(LARGE))]
        self.cycle()
        self.assertEqual(self.github.status(1), "Needs split")
        self.assertEqual([s["task"] for s in split.pending(self.store)], [f"{REPO}#1"])
        self.assertNotIn("First", " ".join(self.github.issue_comments))

    def test_a_bad_report_goes_to_a_person_and_costs_nothing_more(self):
        self.github.add(1, "Backlog", body=body())
        self.runner.steps = [ok("I think it is fine.")]
        self.cycle()
        self.cycle()
        self.assertEqual(self.github.status(1), "Needs info")
        self.assertEqual(len(self.runner.runs), 1)
        self.assertEqual(self.events("task_sized"), [])

    def test_moved_back_it_is_sized_again(self):
        self.github.add(1, "Backlog", body=body())
        self.runner.steps = [ok("nonsense"), ok(json.dumps(sized()))]
        self.cycle()
        self.github.statuses[f"https://github.com/{REPO}/issues/1"] = "Backlog"
        self.cycle()
        self.assertEqual(len(self.events("task_sized")), 1)

    def test_a_project_over_its_daily_limit_waits(self):
        self.github.add(1, "Backlog", body=body())
        self.config.projects[0].daily_token_limit = 0
        self.cycle()
        self.assertEqual(self.runner.runs, [])

    def test_a_pr_bigger_than_its_size_is_only_a_warning(self):
        self.sized(f"{REPO}#1")
        self.github.add(1, "Ready")
        def worker():
            self.github.open_pr(REPO, 1)["changedFiles"] = 44
            return PR_OPENED
        self.config.projects[0].auto_merge = False
        self.runner.steps = [worker, report(SHA1)]
        self.cycle()
        self.assertEqual([e["decision"] for e in self.events("decision") if e["decision"] == "oversized"], ["oversized"])
        self.assertEqual(self.github.status(1), "In review")


class QueenLockTest(unittest.TestCase):
    item = mock.Mock(status="Backlog", size="Small")

    def why(self, item=None, sized=None, group=None):
        return queen.why_no_release(item or self.item, True, 0, 100, sized if sized is not None else {"size": "Small"}, group)

    def test_a_sized_task_may_go(self):
        self.assertIsNone(self.why())

    def test_no_size_in_the_log_no_release(self):
        self.assertIn("not sized", queen.why_no_release(self.item, True, 0, 100, None, None))

    def test_the_log_beats_a_board_field_someone_edited(self):
        self.assertIn("Large", self.why(sized={"size": "Large"}))

    def test_a_group_comes_first(self):
        self.assertIn("group", self.why(group="acme-org/website#9"))


class GroupTest(Base):
    def setUp(self):
        super().setUp()
        self.github.on_add = lambda key, size: None
        self.github.add(10, "Needs split", body=body().replace("none\n", "acme-org/website#2\n"), priority="High",
                        size="Large", deadline="2026-12-01")
        self.key = f"{REPO}#10"
        self.store.write_split(self.key, {"title": "Big", "reason": "Too much.",
                                          "slices": [part("First"), part("Second", door="one-way")]})
        self.created = []
        patcher = mock.patch.object(split, "create_task", side_effect=self.create)
        patcher.start()
        self.addCleanup(patcher.stop)

    def create(self, config, github, project, repo, title, text, local_dir, priority, kind, size, deadline):
        number = 11 + len(self.created)
        self.created.append(SlimIssue(number, title, text, priority, size, deadline))
        github.add(number, "Backlog", body=text, size=size)
        return f"https://github.com/{repo}/issues/{number}"

    def approve(self, slices=None):
        return split.approve(self.config, self.github, self.store, self.key, self.local, slices)

    def test_approving_chains_the_slices_after_the_blockers_of_the_task(self):
        self.assertEqual(self.approve(), [f"{REPO}#11", f"{REPO}#12"])
        first, second = self.created
        self.assertIn("## Blocked by\nacme-org/website#2\n", first.body)
        self.assertIn(f"## Blocked by\n{REPO}#11\n", second.body)
        self.assertIn("Part of #10", second.body)
        self.assertEqual((first.priority, first.deadline), ("High", "2026-12-01"))

    def test_each_slice_is_sized_so_it_costs_no_second_run(self):
        self.approve()
        self.assertEqual([e["door"] for e in self.events("task_sized")], ["two-way", "one-way"])
        self.assertIsNotNone(self.store.last_event("task_split", task=self.key))

    def test_it_happens_once_and_only_from_the_column(self):
        self.approve()
        with self.assertRaises(TaskError):
            self.approve()
        self.assertEqual(split.pending(self.store), [])

    def test_the_user_can_change_the_slices_but_not_past_the_rules(self):
        self.assertEqual(len(self.approve([part("A"), part("B"), part("C")])), 3)
        self.assertEqual(self.created[2].title, "C")

    def test_edited_slices_are_checked_too(self):
        with self.assertRaises(split.ReportError):
            self.approve([part("Alone")])
        self.assertEqual(self.created, [])

    def test_a_task_out_of_the_column_cannot_be_split(self):
        self.github.statuses[f"https://github.com/{REPO}/issues/10"] = "Backlog"
        with self.assertRaises(TaskError):
            self.approve()

    def test_nothing_else_starts_in_the_project_until_the_group_is_done(self):
        self.approve()
        self.github.add(30, "Backlog", size="Small")
        self.sized(f"{REPO}#30")
        sup = self.new_supervisor()
        project = self.config.projects[0]
        self.assertEqual(sup._group_ahead(project, f"{REPO}#30"), self.key)
        self.assertIsNone(sup._group_ahead(project, f"{REPO}#12"))
        self.github.open_issues -= {(REPO, 11), (REPO, 12)}
        self.assertIsNone(sup._group_ahead(project, f"{REPO}#30"))

    def test_the_parent_closes_with_the_last_slice(self):
        self.approve()
        sup = self.new_supervisor()
        task = supervisor.Task(self.config.projects[0], REPO, 11, "t", "u", "In review", "b")
        sup._close_group(task)
        self.assertEqual(self.github.closed, [])
        self.github.open_issues.discard((REPO, 11))
        task.number = 12
        sup._close_group(task)
        self.assertEqual(self.github.closed, [(REPO, 10)])
        self.assertEqual(self.github.status(10), "Done")


class SlimIssue:
    def __init__(self, number, title, body, priority, size, deadline):
        self.number, self.title, self.body, self.priority, self.size, self.deadline = number, title, body, priority, size, deadline


class ProposalStoreTest(Base):
    def test_a_proposal_is_private_and_readable(self):
        self.store.write_split("o/r#1", {"title": "T", "reason": "R", "slices": []})
        self.assertEqual(self.store.read_split("o/r#1")["reason"], "R")
        self.assertEqual(self.store._split_file("o/r#1").stat().st_mode & 0o777, 0o600)
        self.assertIsNone(self.store.read_split("o/r#2"))


class SplitterPromptTest(Base):
    def test_it_gets_the_scale_from_code(self):
        prompt = agents.splitter_prompt(self.config.projects[0], REPO, 1, self.local)
        self.assertIn(f"and {sizing.LIMITS['Medium']['files']} changed files", prompt)
        self.assertIn("## Acceptance criteria", prompt)

    def test_it_changes_nothing(self):
        allowed, denied = agents.splitter_tools()
        self.assertFalse({"Edit", "Write"} & set(allowed))
        self.assertTrue({"Edit", "Write", "NotebookEdit"} <= set(denied))
        self.assertFalse([a for a in allowed if "push" in a or "comment" in a])


class DidbanTest(unittest.TestCase):
    brain = mock.Mock(reach="ssh host", install_dir="~/kandu")

    def run_with(self, returncode, stdout=""):
        result = mock.Mock(returncode=returncode, stdout=stdout, stderr="")
        with mock.patch("kandu.didban.subprocess.run", return_value=result):
            return didban.waiting_splits(self.brain)

    def test_it_opens_with_what_waits(self):
        self.assertEqual(self.run_with(0, "a/b#1: Big (3 slices)\na/b#2: Bigger (2 slices)\n"),
                         "a/b#1: Big (3 slices); a/b#2: Bigger (2 slices)")
        self.assertEqual(self.run_with(0), "none")

    def test_a_failure_is_not_read_as_nothing_waiting(self):
        self.assertEqual(self.run_with(1), "could not be read")


class ColumnTest(unittest.TestCase):
    def test_a_new_column_keeps_the_ids_of_the_others(self):
        github = GitHub()
        fields = {"fields": [{"id": "F", "name": "Status", "options": [{"id": "o1", "name": "Backlog"}]}]}
        with mock.patch.object(github, "_json", return_value=fields), mock.patch.object(github, "_run") as run:
            github.add_columns(Board("acme-org", 1), ["Needs split"])
        query = run.call_args.args[0][-1]
        self.assertIn('{id: "o1", name: "Backlog"', query)
        self.assertIn('{name: "Needs split"', query)

    def test_a_board_without_the_column_is_found(self):
        self.assertIn("Needs split", boards.missing_columns(Board("acme-org", 1), ["Backlog"]))


if __name__ == "__main__":
    unittest.main()
