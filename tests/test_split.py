import contextlib
import dataclasses
import io
import json
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from kandu import agents, boards, didban, priority, queen, sizing, split, supervisor
from kandu.config import BOARD_COLUMNS, Board
from kandu.github import GitHub, GitHubError, blockers_in
from kandu.tasks import TaskError, check_size

from test_supervisor import PR_OPENED, REPO, SHA1, Base, ok, report

import tempfile as _tempfile
from kandu import ghmeter as _ghmeter

SMALL, MEDIUM = sizing.LIMITS[sizing.SMALL], sizing.LIMITS[sizing.MEDIUM]
_ghmeter.LOG_DIR = Path(_tempfile.mkdtemp())  # the meter's lines stay out of state/


def body(criteria=1):
    boxes = "\n".join(f"- [ ] criterion {n}" for n in range(criteria))
    return (f"## Goal\nA goal.\n\n## Current behavior\nNow.\n\n## Acceptance criteria\n{boxes}\n\n## How to test\nRun it.\n\n"
            "## Out of scope\nNone.\n\n## Blocked by\nnone\n")


def part(title="One slice", size="Small", files=2, door="two-way", criteria=1):
    return {"title": title, "size": size, "files": files, "door": door, "body": body(criteria)}


def sized(size="Small", files=2, door="two-way", slices=(), forbidden=()):
    return {"size": size, "files": files, "door": door, "reason": "It is what it is.", "slices": list(slices),
            "forbidden": list(forbidden)}


LARGE = sized("Large", 40, "mixed", [part("First"), part("Second", door="one-way")])


class ScaleTest(unittest.TestCase):
    def test_the_criteria_are_counted_in_their_own_section(self):
        self.assertEqual(sizing.criteria_in(body(4) + "\n## Notes\n- [ ] not a criterion\n"), 4)
        self.assertEqual(sizing.criteria_list("## Acceptance criteria\n- [ ] One thing\n- [x]  Two\n"), ["One thing", "Two"])

    def test_the_smallest_size_comes_from_the_facts(self):
        self.assertEqual(sizing.smallest(SMALL["criteria"], SMALL["files"], "two-way"), "Small")
        self.assertEqual(sizing.smallest(SMALL["criteria"], SMALL["files"] + 1, "two-way"), "Medium")
        self.assertEqual(sizing.smallest(MEDIUM["criteria"], MEDIUM["files"], "two-way"), "Medium")
        self.assertEqual(sizing.smallest(MEDIUM["criteria"] + 1, 1, "two-way"), "Large")
        self.assertEqual(sizing.smallest(1, MEDIUM["files"] + 1, "one-way"), "Large")

    def test_a_mixed_door_is_large_however_small(self):
        self.assertEqual(sizing.smallest(1, 1, "mixed"), "Large")

    def test_a_task_with_too_many_criteria_cannot_be_called_small(self):
        with self.assertRaises(TaskError):
            check_size(body(SMALL["criteria"] + 1), "Small")
        check_size(body(SMALL["criteria"] + 1), "Medium")
        check_size(body(MEDIUM["criteria"] + 4), "Large")


class ReportTest(unittest.TestCase):
    def test_a_fair_report_passes(self):
        split.validate_report(sized(), body())
        split.validate_report(LARGE, body())

    def test_a_report_in_a_code_fence_is_read(self):
        self.assertEqual(split.parse_report('```json\n{"size": "Small"}\n```'), {"size": "Small"})
        self.assertEqual(split.parse_report('A line first.\n\n```json\n{"size": "Small"}\n```\n'), {"size": "Small"})

    def test_only_json_is_a_report(self):
        for text in ("not json", "[1]"):
            with self.assertRaises(split.ReportError):
                split.parse_report(text)

    def test_it_cannot_call_a_big_task_small(self):
        for report in (sized(files=SMALL["files"] + 1), sized("Medium", MEDIUM["files"] + 1), sized("Medium", 2, "mixed")):
            with self.assertRaises(split.ReportError):
                split.validate_report(report, body())

    def test_it_may_size_up_but_only_a_large_task_has_slices(self):
        split.validate_report(sized("Medium"), body())
        with self.assertRaises(split.ReportError):
            split.validate_report(sized(slices=[part(), part()]), body())

    def test_a_large_task_needs_slices_that_can_run(self):
        bad = ([], [part()], [part(), part(size="Large")], [part(), part(door="mixed")], [part(), part(files=SMALL["files"] + 1)],
               [part(), part(criteria=SMALL["criteria"] + 1)], [part(), {"title": "No body", "size": "Small", "files": 1, "door": "two-way"}],
               [part(), part() | {"body": "## Goal\nOnly this."}], [part(), part("عنوان")])
        for slices in bad:
            with self.subTest(slices=slices), self.assertRaises(split.ReportError):
                split.validate_report(sized("Large", 40, "mixed", slices), body())

    def test_it_names_only_the_projects_forbidden_areas(self):
        split.validate_report(sized(forbidden=["migrations/"]), body(), ["migrations/", "src/pay.py"])
        bad = (sized(forbidden=["src/other/"]), sized(forbidden=["migrations/", "docs/"]),
               sized(forbidden="migrations/"), sized(forbidden=[3]), {k: v for k, v in sized().items() if k != "forbidden"})
        for report in bad:
            with self.subTest(report=report), self.assertRaises(split.ReportError):
                split.validate_report(report, body(), ["migrations/", "src/pay.py"])

    def test_blockers_are_replaced_in_place(self):
        text = split.with_blockers(body(), ["acme-org/website#3"])
        self.assertIn("## Blocked by\nacme-org/website#3\n", text)
        self.assertNotIn("none", text)


class SizingTest(Base):
    def setUp(self):
        super().setUp()
        self.github.on_add = lambda key, size: None

    def test_a_task_is_sized_once(self):
        self.github.add(1, "Backlog", body=body())
        self.runner.steps = [ok(json.dumps(sized("Medium")))]
        self.cycle()
        self.cycle()
        self.assertEqual(len(self.runner.runs), 1)
        self.assertEqual(self.github.fields[-1][1:], ("Size", "Medium"))
        self.assertEqual(self.events("task_sized")[0]["run_id"][:2], "r-")
        self.assertEqual(self.events("run_finished")[0]["role"], "splitter")
        self.assertEqual(self.github.status(1), "Backlog")

    def large_two_way(self):
        self.github.add(1, "Backlog", body=body())
        self.runner.steps = [ok(json.dumps(sized("Large", 40, "mixed", [part("First"), part("Second")])))]
        with mock.patch.object(split, "approve") as approve:
            self.cycle()
        approve.assert_not_called()
        self.assertEqual(self.github.status(1), "Needs split")
        self.assertEqual([s["task"] for s in split.pending(self.store)], [f"{REPO}#1"])
        return [(e["decision"], e["reason"]) for e in self.events("decision")]

    def test_a_two_way_proposal_waits_for_the_user_while_the_user_accepts_splits(self):
        self.assertEqual(self.config.projects[0].split_approval, "user")
        self.assertEqual(self.large_two_way(), [("split_waits", "the user accepts this project's splits")])

    def test_a_two_way_proposal_waits_for_the_user_while_the_queen_cannot_accept_one(self):
        self.config.projects[0].split_approval = "queen"
        self.assertEqual(self.large_two_way(), [("split_waits", "the queen cannot accept a split yet")])

    def test_the_dashboards_choice_wins_over_the_registry(self):
        self.config.projects[0].split_approval = "queen"
        self.store.set_project_setting(self.config.projects[0].name, "split_approval", "user")
        self.assertEqual(self.large_two_way(), [("split_waits", "the user accepts this project's splits")])

    def test_every_unsized_task_is_sized_in_one_cycle(self):
        for number in range(1, 7):
            self.github.add(number, "Backlog", body=body())
        self.runner.steps = [ok(json.dumps(sized("Medium"))) for _ in range(6)]
        with mock.patch.object(supervisor, "ThreadPoolExecutor", wraps=supervisor.ThreadPoolExecutor) as pool:
            self.cycle()
        self.assertEqual(sorted(e["task"] for e in self.events("task_sized")),
                         sorted(f"{REPO}#{n}" for n in range(1, 7)))
        self.assertEqual(pool.call_args_list[0].kwargs["max_workers"], self.config.settings.supervisor.max_parallel_sizers)

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


class HoldTest(Base):
    """A task that needs a forbidden area waits for the user before any work starts."""

    def setUp(self):
        super().setUp()
        self.github.on_add = lambda key, size: None
        self.key = f"{REPO}#1"
        self.github.add(1, "Backlog", body=body())

    def hold(self):
        self.runner.steps = [ok(json.dumps(sized(forbidden=["migrations/"])))]
        self.cycle()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.argv", ["split", *args]), mock.patch.object(split, "StateStore", return_value=self.store), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = split.main()
        return code, out.getvalue(), err.getvalue()

    def test_a_held_task_stays_in_backlog_with_its_areas(self):
        self.hold()
        self.assertEqual(self.github.status(1), "Backlog")
        held = self.events("task_held")
        self.assertEqual([(e["task"], e["areas"], e["reason"]) for e in held],
                         [(self.key, ["migrations/"], "It is what it is.")])
        self.assertIn("forbidden area", " ".join(self.github.issue_comments))
        self.assertNotIn("migrations/", " ".join(self.github.issue_comments))
        self.cycle()
        self.assertEqual(len(self.runner.runs), 1)  # sized once, and no worker

    def test_a_large_held_task_is_not_split(self):
        self.runner.steps = [ok(json.dumps(LARGE | {"forbidden": ["migrations/"]}))]
        self.cycle()
        self.assertEqual(self.github.status(1), "Backlog")
        self.assertEqual(split.pending(self.store), [])

    def test_the_queen_never_sees_or_starts_a_held_task(self):
        self.config.settings.supervisor.queen = True
        self.github.add(2, "Backlog", body=body())
        self.store.record("task_sized", task=f"{REPO}#2", project="website", run_id=None, size="Small", files=1,
                          door="two-way", criteria=1, slices=0)
        release = {"task": self.key, "action": "release", "note": "Go."}
        self.runner.steps = [ok(json.dumps(sized(forbidden=["migrations/"]))),
                             ok(json.dumps({"brief": "Done.", "decisions": [release]}))]
        self.cycle()
        queen_run = self.runner.runs[1]
        backlog = [t["task"] for p in json.loads(queen_run.prompt.split("## Projects\n```json\n")[1].split("```")[0])
                   for t in p["backlog"]]
        self.assertEqual(backlog, [f"{REPO}#2"])
        self.assertEqual(self.github.status(1), "Backlog")
        self.assertIn(f"Refused: release of {self.key}", self.events("queen_finished")[0]["brief"])

    def test_a_released_task_is_sized_again(self):
        self.hold()
        code, out, _ = self.run_cli("held")
        self.assertEqual((code, out), (0, f"{self.key}: needs migrations/ (It is what it is.)\n"))
        self.assertEqual(self.run_cli("release", self.key)[0], 0)
        self.assertEqual(self.run_cli("held")[1], "")
        self.assertIsNone(split.current_size(self.store, self.key))
        self.runner.steps = [ok(json.dumps(sized()))]
        self.cycle()
        self.assertEqual(len(self.events("task_sized")), 2)
        self.assertIsNone(split.held(self.store, self.key))
        code, _, err = self.run_cli("release", self.key)
        self.assertEqual(code, 1)
        self.assertIn("not held", err)

    def test_a_task_that_needs_no_forbidden_area_goes_as_today(self):
        self.runner.steps = [ok(json.dumps(sized()))]
        self.cycle()
        self.assertEqual(self.events("task_held"), [])
        self.assertIsNone(split.held(self.store, self.key))
        self.assertEqual(self.github.issue_comments, [])

    def test_the_splitter_is_asked_for_the_areas(self):
        self.assertIn("forbidden", agents.SPLIT_SCHEMA["required"])
        self.assertIn("`forbidden`", agents.splitter_prompt(self.config.projects[0], REPO, 1, "en", self.local))


class PriorityTest(unittest.TestCase):
    def task(self, number, status="Ready", priority="Medium", rank=1):
        return supervisor.Task(mock.Mock(priority=rank), REPO, number, "t", "u", status, "b", priority)

    def test_high_goes_first_but_after_what_has_begun(self):
        tasks = [self.task(1), self.task(2, "Ready", "High", rank=2), self.task(3, "In progress")]
        self.assertEqual([t.number for t in sorted(tasks, key=lambda t: t.order)], [3, 2, 1])

    def test_raising_a_task_sets_its_field_and_wakes_the_queen(self):
        project = mock.Mock(repos=[REPO], board=Board("o", 1))
        project.name = "website"
        github, store = mock.Mock(), mock.Mock()
        github.board_items.return_value = [mock.Mock(repo=REPO, number=7, url="u")]
        priority.raise_task(mock.Mock(projects=[project]), github, store, f"{REPO}#7")
        github.set_field.assert_called_once_with(project.board, "u", "Priority", "High")
        store.record.assert_called_once_with("priority_raised", task=f"{REPO}#7", project="website")

    def test_a_raised_task_is_worth_a_look_the_same_day(self):
        looked = [{"type": "queen_finished", "ts": "2026-10-31T01:00:00+01:00", "backlog": [f"{REPO}#7"]}]
        self.assertFalse(queen.worth_a_look(looked, [], [f"{REPO}#7"], "2026-10-31"))
        self.assertTrue(queen.worth_a_look(looked, [{"type": "priority_raised"}], [f"{REPO}#7"], "2026-10-31"))


class QueenLockTest(unittest.TestCase):
    item = mock.Mock(status="Backlog", size="Small")

    def why(self, item=None, sized=None):
        return queen.why_no_release(item or self.item, True, 0, 100, sized if sized is not None else {"size": "Small"})

    def test_a_sized_task_may_go(self):
        self.assertIsNone(self.why())

    def test_no_size_in_the_log_no_release(self):
        self.assertIn("not sized", queen.why_no_release(self.item, True, 0, 100, None, None))

    def test_the_log_beats_a_board_field_someone_edited(self):
        self.assertIn("Large", self.why(sized={"size": "Large"}))

    def retry(self, sized, role=None):
        events = [{"type": "task_finished", "task": "a/b#1", "outcome": "failed"}]
        if role:
            events.append({"type": "run_finished", "task": "a/b#1", "role": role})
        return queen.why_no_retry(events, "a/b#1", 2, True, False, sized)

    def test_a_retry_cannot_pass_the_size_lock(self):
        self.assertIn("Large", self.retry({"size": "Large"}))
        self.assertIn("not sized", self.retry(None))
        self.assertIn("not sized", self.retry(None, "splitter"))

    def test_a_task_older_than_the_splitter_may_be_retried(self):
        self.assertIsNone(self.retry(None, "worker"))
        self.assertIsNone(self.retry({"size": "Small"}))


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
        self.placed = []
        placer = mock.patch.object(split, "place_task", side_effect=lambda github, board, url, *rest: self.placed.append(url))
        placer.start()
        self.addCleanup(placer.stop)
        self.board_fails = False

    def create(self, config, github, project, repo, title, text, local_dir, priority, kind, size, deadline, created=None,
               store=None):
        number = 11 + len(self.created)
        self.created.append(SlimIssue(number, title, text, priority, size, deadline))
        url = f"https://github.com/{repo}/issues/{number}"
        if created is not None:
            created(url)
        if self.board_fails:
            raise GitHubError("gh project item-edit: API rate limit exceeded")
        github.add(number, "Backlog", body=text, size=size)
        return url

    def approve(self, slices=None):
        return split.approve(self.config, self.github, self.store, self.key, self.local, slices)

    def test_the_users_text_stays_out_of_the_issues(self):
        text = {"reason": "R", "slices": [{"title": "Yek", "body": "B1"}, {"title": "Do", "body": "B2"}]}
        self.store.write_split(self.key, self.store.read_split(self.key) | {"user_text": text})
        self.approve()
        self.assertNotIn("Yek", "".join(f"{issue.title}{issue.body}" for issue in self.created))

    def test_a_yes_holds_only_for_the_proposal_it_saw(self):
        for seen in ("", "old"):
            with self.assertRaisesRegex(TaskError, "changed since"):
                split.approve(self.config, self.github, self.store, self.key, self.local, seen=seen)
        self.assertEqual(self.created, [])
        seen = split.fingerprint(self.store.read_split(self.key))
        split.approve(self.config, self.github, self.store, self.key, self.local, seen=seen)
        self.assertEqual(len(self.created), 2)

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

    def test_a_retry_after_a_board_failure_places_the_slice_and_copies_none(self):
        self.board_fails = True
        with self.assertRaises(GitHubError):
            self.approve()
        self.assertEqual([e["task"] for e in self.events("slice_created")], [f"{REPO}#11"])
        self.board_fails = False
        self.assertEqual(self.approve(), [f"{REPO}#11", f"{REPO}#12"])
        self.assertEqual([issue.number for issue in self.created], [11, 12])
        self.assertEqual(self.placed, [f"https://github.com/{REPO}/issues/11"])

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

    def set_status(self, number, status):
        self.github.statuses[f"https://github.com/{REPO}/issues/{number}"] = status

    def second_group(self):
        """A group made after the first, as the board had it when a later split was approved."""
        self.github.add(30, "Backlog", size="Medium")
        self.github.add(31, "Backlog", size="Medium")
        self.store.record("task_split", task=f"{REPO}#29", project=self.config.projects[0].name,
                          children=[f"{REPO}#30", f"{REPO}#31"])

    def release(self, *numbers):
        decisions = [{"task": f"{REPO}#{n}", "action": "release", "note": "Go."} for n in numbers]
        return self.new_supervisor()._carry_out(decisions, [])

    def first_in_line(self):
        sup = self.new_supervisor()
        pairs = list(sup._board_items())
        items = {f"{item.repo}#{item.number}": (project, item) for project, item in pairs}
        return sup._first_in_line(self.config.projects[0], items, sup._startable(pairs))

    def finish_first_slice(self):
        self.github.open_issues.discard((REPO, 11))
        self.set_status(11, "Done")

    def test_slices_and_other_tasks_go_side_by_side(self):
        self.approve()
        self.second_group()
        self.github.add(40, "Backlog", size="Small")
        self.sized(f"{REPO}#30")
        self.sized(f"{REPO}#40")
        self.assertEqual(self.first_in_line(), [])
        refused, released = self.release(40, 30, 11, 12)
        self.assertEqual((refused, sorted(released)), ([], sorted(f"{REPO}#{n}" for n in (40, 30, 11, 12))))

    def test_high_comes_between_two_slices_once_the_worker_is_done(self):
        self.approve()
        self.github.add(40, "Backlog", size="Small", priority="High")
        self.sized(f"{REPO}#40")
        self.finish_first_slice()
        self.assertEqual(self.first_in_line(), [f"{REPO}#40"])
        refused, released = self.release(12, 40)
        self.assertEqual(released, [f"{REPO}#40"])
        self.assertIn(f"{REPO}#40 goes first", refused[0])

    def test_a_task_nothing_can_start_goes_before_nothing(self):
        self.approve()
        self.github.add(40, "Backlog", size="Large", priority="High")
        self.sized(f"{REPO}#40", "Large")
        self.github.open_issues.discard(("acme-org/website", 2))
        self.assertEqual(self.release(11), ([], [f"{REPO}#11"]))

    def test_the_queen_can_release_past_a_parked_group(self):
        self.approve()
        self.second_group()
        self.set_status(11, "Technical debt")
        self.set_status(12, "Technical debt")
        self.sized(f"{REPO}#30")
        sup = self.new_supervisor()
        decision = {"task": f"{REPO}#30", "action": "release", "note": "Go."}
        refused, released = sup._carry_out([decision], [])
        self.assertEqual((refused, released), ([], [f"{REPO}#30"]))
        self.assertEqual(self.github.status(30), "Ready")

    def test_the_parent_closes_with_the_last_slice(self):
        self.approve()
        sup = self.new_supervisor()
        sup._close_groups()
        self.assertEqual(self.github.closed, [])
        self.github.open_issues -= {(REPO, 11), (REPO, 12)}
        sup._close_groups()
        sup._close_groups()
        self.assertEqual(self.github.closed, [(REPO, 10)])
        self.assertEqual(self.github.status(10), "Done")

    def test_a_failure_closing_the_group_does_not_undo_the_slice(self):
        self.approve()
        self.github.open_issues -= {(REPO, 11), (REPO, 12)}
        sup = self.new_supervisor()
        project = dataclasses.replace(self.config.projects[0], deploy=None)
        task = supervisor.Task(project, REPO, 12, "t", f"https://github.com/{REPO}/issues/12", "In review", "b")
        with mock.patch.object(self.github, "close_issue", side_effect=GitHubError("down")):
            sup._land(task)
        self.assertEqual(self.github.status(12), "Done")
        self.assertEqual(self.github.closed, [])
        sup._close_groups()
        self.assertEqual(self.github.closed, [(REPO, 10)])

    def test_the_parent_is_not_a_blocker_of_its_slices(self):
        self.approve()
        first, second = self.created
        self.assertEqual(blockers_in(first.body, REPO), [("acme-org/website", 2)])
        self.assertEqual(blockers_in(second.body, REPO), [(REPO, 11)])

    def test_a_private_word_stops_the_split_before_the_first_issue(self):
        (Path(self.local) / "private-words.txt").write_text("criterion\n", encoding="utf-8")
        with mock.patch.object(self.github, "repo_is_public", return_value=True), self.assertRaises(TaskError):
            self.approve()
        self.assertEqual(self.created, [])

    def test_a_retry_carries_on_after_the_slices_that_exist(self):
        real = split.create_task.side_effect
        def fail_on_second(*args, **kwargs):
            if len(self.created) == 1:
                raise TaskError("gh failed")
            return real(*args, **kwargs)
        split.create_task.side_effect = fail_on_second
        with self.assertRaises(TaskError):
            self.approve()
        self.assertIsNone(self.store.last_event("task_split", task=self.key))
        split.create_task.side_effect = real
        self.assertEqual(self.approve(), [f"{REPO}#11", f"{REPO}#12"])
        self.assertEqual([c.title for c in self.created], ["First", "Second"])
        self.assertIn(f"## Blocked by\n{REPO}#11\n", self.created[1].body)

    def waiting(self, number, blockers, status="Backlog"):
        self.github.add(number, status, body=body().replace("none\n", f"{blockers}\n"))

    def body_of(self, number):
        return self.github.issue_text(REPO, number)["body"]

    def rewrites(self):
        return [(e["task"], e["old"], e["new"], e["when"]) for e in self.events("blocker_rewritten")]

    def test_tasks_that_waited_for_the_split_task_wait_for_its_last_slice(self):
        self.waiting(20, "#10")
        self.waiting(21, f"acme-org/website#2\n{REPO}#10")
        self.approve()
        self.assertEqual(blockers_in(self.body_of(20), REPO), [(REPO, 12)])
        self.assertEqual(blockers_in(self.body_of(21), REPO), [("acme-org/website", 2), (REPO, 12)])
        self.assertIn("## Goal\nA goal.", self.body_of(20))
        self.assertEqual(sorted(self.rewrites()), [(f"{REPO}#20", f"{REPO}#10", f"{REPO}#12", "split"),
                                                   (f"{REPO}#21", f"{REPO}#10", f"{REPO}#12", "split")])

    def test_a_task_that_names_a_task_not_split_is_left_as_it_was(self):
        self.waiting(20, "acme-org/website#2")
        self.waiting(21, "none")
        self.waiting(22, "#9")
        before = [self.body_of(n) for n in (20, 21, 22)]
        self.approve()
        self.assertEqual([self.body_of(n) for n in (20, 21, 22)], before)
        self.assertEqual(self.rewrites(), [])

    def test_a_closed_task_keeps_its_blockers(self):
        self.waiting(20, "#10", status="Done")
        before = self.body_of(20)
        self.approve()
        self.assertEqual(self.body_of(20), before)

    def test_a_task_that_names_the_last_slice_too_names_it_once(self):
        self.waiting(20, "#10\n#12")
        self.approve()
        self.assertEqual(blockers_in(self.body_of(20), REPO), [(REPO, 12)])

    def test_a_retry_after_a_failed_slice_rewrites_the_waiting_tasks(self):
        self.waiting(20, "#10")
        real = split.create_task.side_effect
        split.create_task.side_effect = lambda *a, **k: (_ for _ in ()).throw(TaskError("gh failed")) \
            if len(self.created) == 1 else real(*a, **k)
        with self.assertRaises(TaskError):
            self.approve()
        split.create_task.side_effect = real
        self.approve()
        self.assertEqual(blockers_in(self.body_of(20), REPO), [(REPO, 12)])
        self.assertEqual(self.rewrites(), [(f"{REPO}#20", f"{REPO}#10", f"{REPO}#12", "split")])

    def test_a_retry_after_a_failed_rewrite_names_no_slice_twice(self):
        self.waiting(20, "#10")
        self.waiting(21, "#10\n#12")
        real = self.github.edit_issue
        edits = []
        def fail_on_second(repo, number, title, text):
            edits.append(number)
            if len(edits) == 2:
                raise GitHubError("gh issue edit: down")
            real(repo, number, title, text)
        with mock.patch.object(self.github, "edit_issue", side_effect=fail_on_second), self.assertRaises(GitHubError):
            self.approve()
        self.assertIsNone(self.store.last_event("task_split", task=self.key))
        self.assertEqual(self.approve(), [f"{REPO}#11", f"{REPO}#12"])
        for number in (20, 21):
            self.assertEqual(blockers_in(self.body_of(number), REPO), [(REPO, 12)])
        self.assertEqual(sorted(self.rewrites()), [(f"{REPO}#20", f"{REPO}#10", f"{REPO}#12", "split"),
                                                   (f"{REPO}#21", f"{REPO}#10", f"{REPO}#12", "split")])

    def test_declining_sends_it_back_to_be_sized_again(self):
        self.store.record("task_sized", task=self.key, project="website", run_id=None, size="Large", files=40,
                          door="mixed", criteria=9, slices=2, now=datetime(2026, 1, 1).astimezone())
        split.decline(self.config, self.github, self.store, self.key)
        self.assertEqual(self.github.status(10), "Backlog")
        self.assertIsNone(split.current_size(self.store, self.key))
        self.assertEqual(split.pending(self.store), [])
        with self.assertRaises(TaskError):
            split.decline(self.config, self.github, self.store, self.key)

    def run_cli(self, *args, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.argv", ["split", *args, "--local", str(self.local)]), \
                mock.patch.object(split, "StateStore", return_value=self.store), \
                mock.patch.object(split, "open_github", return_value=self.github), \
                mock.patch.object(split, "load_config", return_value=self.config), \
                mock.patch("sys.stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = split.main()
        return code, out.getvalue(), err.getvalue()

    def test_the_command_lists_shows_and_approves(self):
        code, out, _ = self.run_cli("list")
        self.assertEqual((code, out), (0, f"{self.key}: Big (2 slices)\n"))
        code, out, _ = self.run_cli("show", self.key)
        self.assertEqual((code, json.loads(out)["reason"]), (0, "Too much."))
        code, out, _ = self.run_cli("approve", self.key)
        self.assertEqual((code, out), (0, f"{REPO}#11\n{REPO}#12\n"))
        code, _, err = self.run_cli("approve", self.key)
        self.assertEqual(code, 1)
        self.assertIn("not waiting", err)

    def test_the_command_takes_edited_slices_on_stdin(self):
        code, out, _ = self.run_cli("approve", self.key, "--file", "-", stdin=json.dumps([part("A"), part("B"), part("C")]))
        self.assertEqual((code, out.count("\n")), (0, 3))
        self.assertEqual(self.created[2].title, "C")

    def test_the_command_declines(self):
        self.assertEqual(self.run_cli("decline", self.key)[0], 0)
        self.assertEqual(self.github.status(10), "Backlog")
        self.assertEqual(self.run_cli("show", self.key)[0], 1)


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

    def test_it_opens_with_the_held_tasks(self):
        result = mock.Mock(returncode=0, stdout="a/b#1: needs migrations/ (It changes the schema.)\n", stderr="")
        with mock.patch("kandu.didban.subprocess.run", return_value=result) as run:
            facts = didban.waiting(self.brain, "ok")
        self.assertIn("kandu.split held", " ".join(str(c) for c in run.call_args_list))
        self.assertEqual(facts["held"], "a/b#1: needs migrations/ (It changes the schema.)")
        prompt = didban.system_prompt(mock.Mock(brain=self.brain), "ok", **facts)
        self.assertIn("a/b#1: needs migrations/", prompt)
        self.assertIn("kandu.split release", prompt)
        self.assertEqual(didban.waiting(self.brain, "unreachable")["held"], "could not be read")


class ColumnTest(unittest.TestCase):
    def test_a_new_column_keeps_the_ids_of_the_others(self):
        github = GitHub()
        fields = {"fields": [{"id": "F", "name": "Status", "options": [{"id": "o1", "name": "Backlog"}]}]}
        options = {"data": {"node": {"options": [{"id": "o1", "name": "Backlog", "color": "BLUE", "description": "Approved"}]}}}
        with mock.patch.object(github, "_json", side_effect=[fields, options]), mock.patch.object(github, "_run") as run:
            github.add_columns(Board("acme-org", 1), ["Needs split"])
        query = run.call_args.args[0][-1]
        self.assertIn('{id: "o1", name: "Backlog", color: BLUE, description: "Approved"}', query)
        self.assertIn('{name: "Needs split"', query)

    def test_a_board_without_the_column_is_found(self):
        self.assertIn("Needs split", boards.missing_columns(Board("acme-org", 1), ["Backlog"]))

    def add_missing(self, config, found):
        github = mock.Mock()
        github.board_columns.return_value = found
        out = io.StringIO()
        with mock.patch("sys.argv", ["boards", "--owner", "acme-org", "--add-missing", "1"]), \
                mock.patch.object(boards, "GitHub", return_value=github), \
                mock.patch.object(boards, "load_config", return_value=config), contextlib.redirect_stdout(out):
            code = boards.main()
        return code, github, out.getvalue()

    def test_the_command_reads_the_mapping_of_a_board_in_the_config(self):
        mapped = Board("acme-org", 1, columns={"Backlog": "Todo"})
        config = mock.Mock(projects=[mock.Mock(board=mapped)])
        found = ["Todo", *(c for c in BOARD_COLUMNS if c not in ("Backlog", "Needs split"))]
        code, github, _ = self.add_missing(config, found)
        self.assertEqual(code, 0)
        github.add_columns.assert_called_once_with(mapped, ["Needs split"])

    def test_the_command_adds_nothing_to_a_complete_board(self):
        code, github, out = self.add_missing(mock.Mock(projects=[]), list(BOARD_COLUMNS))
        self.assertEqual(code, 0)
        github.add_columns.assert_not_called()
        self.assertIn("nothing is missing", out)



class UserTextTest(unittest.TestCase):
    def report(self, text):
        return {"slices": [{}, {}], "user_text": text}

    def test_a_whole_text_is_kept(self):
        text = {"reason": "R", "slices": [{"title": "A", "body": "B"}, {"title": "C", "body": "D"}]}
        self.assertEqual(split.user_text(self.report(text)), text)

    def test_a_bad_one_falls_back_to_the_english(self):
        for text in (None, "text", {"reason": "R"}, {"reason": "R", "slices": [{"title": "A", "body": "B"}]},
                     {"reason": "R", "slices": [{"title": "A"}, {"title": "C", "body": "D"}]}):
            self.assertIsNone(split.user_text(self.report(text)))


if __name__ == "__main__":
    unittest.main()
