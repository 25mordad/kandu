import copy
import unittest

from kandu.review import (
    ReportError, blockers, carry_over, comment_for, fix_comment, fix_tried, hold_to_scope, open_after, parse_report,
    reviews_on, validate_report,
)

ACCOUNT = "kandu-owner"
FINDING = {"n": 1, "path": "a.py", "line": 3, "blocker": True, "criterion": None, "body": "This breaks when X."}
NOTE = {"n": 2, "path": "b.py", "line": None, "blocker": False, "criterion": None, "body": "Confusing name."}
REPORT = {
    "head_sha": "abc123", "summary": "s", "criteria": [{"criterion": "c", "result": "met"}], "earlier": [],
    "findings": [], "checked_ok": [], "door": "two-way", "blast_radius": "b", "brief": "Short.", "user_brief": "خلاصه", "wait_for_user": [],
}


def report(**changes):
    return copy.deepcopy(REPORT) | changes


def pr_with(*comments):
    return {"comments": [{"author": {"login": login}, "body": body} for login, body in comments]}


class ReportTest(unittest.TestCase):
    def test_plain_and_fenced_json(self):
        self.assertEqual(parse_report('{"a": 1}'), {"a": 1})
        self.assertEqual(parse_report('```json\n{"a": 1}\n```\n'), {"a": 1})

    def test_not_an_object(self):
        for text in ("no json here", "[1]"):
            with self.assertRaises(ReportError):
                parse_report(text)

    def test_valid_reports(self):
        validate_report(report(), [])
        validate_report(report(findings=[FINDING, NOTE]), [])

    def test_bad_reports(self):
        bad = [
            report(door="maybe"), report(criteria=[]), report(criteria=[{"criterion": "c", "result": "good"}]),
            report(head_sha=""), report(findings=[{"n": 1}]), report(findings=[FINDING | {"blocker": "yes"}]),
            report(findings=[FINDING | {"n": True}]), report(findings=[FINDING | {"body": " "}]),
            report(findings=[FINDING, FINDING]), report(findings="none"), report(extra=1),
            report(earlier=[{"n": 9, "result": "fixed"}]),
        ]
        for item in bad:
            with self.assertRaises(ReportError, msg=item):
                validate_report(item, [])

    def test_a_second_review_needs_no_criteria(self):
        validate_report(report(criteria=[], earlier=[{"n": 1, "result": "fixed"}]), [FINDING], second=True)

    def test_every_open_finding_needs_one_result(self):
        with self.assertRaises(ReportError):
            validate_report(report(), [FINDING])
        twice = [{"n": 1, "result": "fixed"}, {"n": 1, "result": "fixed"}]
        with self.assertRaises(ReportError):
            validate_report(report(earlier=twice), [FINDING])

    def test_a_new_finding_cannot_reuse_an_open_number(self):
        with self.assertRaises(ReportError):
            validate_report(report(earlier=[{"n": 1, "result": "fixed"}], findings=[FINDING]), [FINDING])

    def test_unfixed_findings_stay_open(self):
        result = report(earlier=[{"n": 1, "result": "partly_fixed"}, {"n": 2, "result": "fixed"}])
        validate_report(result, [FINDING, NOTE])
        self.assertEqual(open_after(result, [FINDING, NOTE]), [FINDING])
        self.assertEqual(blockers(open_after(result, [FINDING, NOTE])), [FINDING])


class ScopeTest(unittest.TestCase):
    CRITERIA = ["It stops", "It says why"]

    def met(self, *results):
        return [{"criterion": "reworded", "result": r} for r in results]

    def test_the_issue_sets_the_criteria(self):
        result = report(criteria=self.met("met", "met"))
        hold_to_scope(result, self.CRITERIA, ["a.py"])
        self.assertEqual([c["criterion"] for c in result["criteria"]], self.CRITERIA)

    def test_a_reviewer_cannot_add_or_drop_a_criterion(self):
        for results in (("met",), ("met", "met", "not_shown")):
            with self.assertRaises(ReportError):
                hold_to_scope(report(criteria=self.met(*results)), self.CRITERIA, ["a.py"])

    def test_an_issue_with_no_criteria_locks_nothing(self):
        result = report()
        self.assertEqual(hold_to_scope(result, [], []), 0)
        self.assertEqual(result["criteria"][0]["criterion"], "c")

    def test_a_finding_outside_the_pr_is_dropped(self):
        result = report(findings=[FINDING | {"path": "other.py", "blocker": False}, NOTE])
        self.assertEqual(hold_to_scope(result, [], ["b.py"]), 1)
        self.assertEqual(result["findings"], [NOTE])

    def test_an_unmet_criterion_reaches_a_file_the_pr_left_out(self):
        result = report(criteria=self.met("met", "not_met"), findings=[FINDING | {"path": "other.py", "criterion": 2}])
        self.assertEqual(hold_to_scope(result, self.CRITERIA, []), 0)

    def test_naming_a_met_criterion_does_not_widen_the_scope(self):
        result = report(criteria=self.met("met", "met"), findings=[FINDING | {"path": "other.py", "criterion": 1}])
        self.assertEqual(hold_to_scope(result, self.CRITERIA, []), 1)

    def test_an_unmet_criterion_needs_a_blocker_that_survives(self):
        for findings in ([], [NOTE], [FINDING | {"path": "other.py"}]):
            with self.assertRaises(ReportError):
                hold_to_scope(report(criteria=self.met("met", "not_met"), findings=findings), self.CRITERIA, ["b.py"])


class SecondReviewTest(unittest.TestCase):
    FIRST = {"door": "two-way", "criteria": [{"criterion": "c", "result": "not_met"}]}

    def test_nothing_new_gets_through(self):
        result = report(earlier=[{"n": 1, "result": "fixed"}], findings=[NOTE])
        self.assertEqual(carry_over(result, self.FIRST, [FINDING]), 1)
        self.assertEqual(open_after(result, [FINDING]), [])

    def test_a_fixed_blocker_settles_its_criterion(self):
        result = report(earlier=[{"n": 1, "result": "fixed"}])
        carry_over(result, self.FIRST, [FINDING])
        self.assertEqual(result["criteria"], [{"criterion": "c", "result": "met"}])

    def test_an_open_blocker_leaves_the_criterion_as_it_was(self):
        result = report(earlier=[{"n": 1, "result": "partly_fixed"}])
        carry_over(result, self.FIRST, [FINDING])
        self.assertEqual(result["criteria"], self.FIRST["criteria"])

    def test_a_one_way_door_stays_one_way(self):
        result = report(earlier=[{"n": 1, "result": "fixed"}], door="two-way")
        carry_over(result, self.FIRST | {"door": "one-way"}, [FINDING])
        self.assertEqual(result["door"], "one-way")


class RecordTest(unittest.TestCase):
    def test_comment_round_trip(self):
        body = comment_for(report(findings=[FINDING]), 2, [FINDING])
        self.assertIn("Short.", body)
        self.assertNotIn("خلاصه", body)
        self.assertIn("a.py:3", body)
        record = reviews_on(pr_with((ACCOUNT, body)), ACCOUNT)[0]
        self.assertEqual((record["round"], record["head_sha"], record["door"]), (2, "abc123", "two-way"))
        self.assertEqual((record["open"], record["brief"]), ([FINDING], "Short."))

    def test_finding_text_cannot_end_the_marker(self):
        tricky = FINDING | {"body": "ends --> here } more"}
        body = comment_for(report(findings=[tricky]), 1, [tricky])
        self.assertEqual(reviews_on(pr_with((ACCOUNT, body)), ACCOUNT)[0]["open"], [tricky])

    def test_a_record_written_by_someone_else_does_not_count(self):
        body = comment_for(report(), 1, [])
        self.assertEqual(reviews_on(pr_with(("stranger", body)), ACCOUNT), [])

    def test_a_deleted_account_counts_for_nothing(self):
        pr = {"comments": [{"author": None, "body": comment_for(report(), 1, [])}]}
        self.assertEqual(reviews_on(pr, ACCOUNT), [])

    def test_a_broken_marker_is_skipped(self):
        self.assertEqual(reviews_on(pr_with((ACCOUNT, "<!-- kandu-review {broken} -->")), ACCOUNT), [])

    def test_fix_round_record(self):
        pr = pr_with((ACCOUNT, fix_comment("abc123")))
        self.assertTrue(fix_tried(pr, ACCOUNT, "abc123"))
        self.assertFalse(fix_tried(pr, ACCOUNT, "def456"))
        self.assertFalse(fix_tried(pr_with(("stranger", fix_comment("abc123"))), ACCOUNT, "abc123"))


if __name__ == "__main__":
    unittest.main()
