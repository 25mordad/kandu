import copy
import unittest

from kandu.review import (
    ReportError, blockers, comment_for, fix_comment, fix_tried, open_after, parse_report, reviews_on, validate_report,
)

ACCOUNT = "kandu-owner"
FINDING = {"n": 1, "path": "a.py", "line": 3, "blocker": True, "body": "This breaks when X."}
NOTE = {"n": 2, "path": "b.py", "line": None, "blocker": False, "body": "Confusing name."}
REPORT = {
    "head_sha": "abc123", "summary": "s", "criteria": [{"criterion": "c", "result": "met"}], "earlier": [],
    "findings": [], "checked_ok": [], "door": "two-way", "blast_radius": "b", "brief": "خلاصه",
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

    def test_unmet_criterion_needs_a_finding(self):
        unmet = [{"criterion": "c", "result": "not_shown"}]
        with self.assertRaises(ReportError):
            validate_report(report(criteria=unmet), [])
        validate_report(report(criteria=unmet, findings=[FINDING]), [])

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
        new = NOTE | {"n": 3}
        result = report(earlier=[{"n": 1, "result": "partly_fixed"}, {"n": 2, "result": "fixed"}], findings=[new])
        validate_report(result, [FINDING, NOTE])
        self.assertEqual(open_after(result, [FINDING, NOTE]), [FINDING, new])
        self.assertEqual(blockers(open_after(result, [FINDING, NOTE])), [FINDING])


class RecordTest(unittest.TestCase):
    def test_comment_round_trip(self):
        body = comment_for(report(findings=[FINDING]), 2, [FINDING])
        self.assertIn("خلاصه", body)
        self.assertIn("a.py:3", body)
        record = reviews_on(pr_with((ACCOUNT, body)), ACCOUNT)[0]
        self.assertEqual((record["round"], record["head_sha"], record["door"]), (2, "abc123", "two-way"))
        self.assertEqual((record["open"], record["brief"]), ([FINDING], "خلاصه"))

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
