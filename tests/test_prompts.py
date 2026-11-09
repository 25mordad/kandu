import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"


def headings(path):
    return re.findall(r"^## (.+)$", path.read_text(encoding="utf-8"), re.MULTILINE)


class TemplatesTest(unittest.TestCase):
    def test_issue_template_has_the_sections_of_the_design(self):
        self.assertEqual(headings(PROMPTS / "templates" / "issue.md"), [
            "Goal", "Current behavior", "Acceptance criteria", "How to test", "Out of scope", "Blocked by",
        ])

    def test_every_text_that_defines_a_task_keeps_related_tasks_out_of_blocked_by(self):
        for path in (PROMPTS / "templates" / "issue.md", PROMPTS / "didban.md",
                     PROMPTS / "defining.md"):
            text = path.read_text(encoding="utf-8")
            self.assertIn("only a task this one cannot work or merge correctly without", text, path.name)
            self.assertIn("never a related one", text, path.name)

    def test_the_queen_asks_to_drop_only_a_blocker_the_task_does_not_need(self):
        text = (PROMPTS / "queen.md").read_text(encoding="utf-8")
        self.assertIn("`drop_blocker` asks the user to drop one blocker", text)
        self.assertIn("Use it only for a blocker the task does not need", text)
        self.assertIn('"action": "drop_blocker", "on": "owner/repo#5"', text)

    def test_pr_template_states_the_blast_radius(self):
        self.assertEqual(headings(PROMPTS / "templates" / "pr.md"), ["Summary", "Evidence", "Blast radius"])


class PromptsTest(unittest.TestCase):
    def test_reviewer_has_the_review_rules(self):
        found = headings(PROMPTS / "reviewer.md")
        for heading in ("Blockers", "Do not report", "Tests", "Verify each blocker before you report it",
                        "Companion PRs", "Scope", "Merge danger", "Report"):
            self.assertIn(heading, found)

    def test_reviewer_report_is_valid_json_with_the_needed_fields(self):
        text = (PROMPTS / "reviewer.md").read_text(encoding="utf-8")
        report = json.loads(re.search(r"```json\n(.+?)```", text, re.DOTALL).group(1))
        self.assertEqual(set(report), {"head_sha", "summary", "criteria", "earlier", "findings",
                                       "checked_ok", "door", "blast_radius", "brief", "user_brief", "wait_for_user"})
        self.assertEqual(set(report["findings"][0]), {"n", "path", "line", "blocker", "criterion", "body"})

    def test_agents_in_other_projects_get_no_path_into_this_repo(self):
        for name in ("worker.md", "reviewer.md"):
            text = (PROMPTS / name).read_text(encoding="utf-8")
            self.assertNotIn("prompts/", text, name)
            self.assertNotIn("DESIGN.md", text, name)

    def test_didban_saves_a_requests_map_each_round_and_resumes_from_it(self):
        text = (PROMPTS / "didban.md").read_text(encoding="utf-8")
        self.assertIn("after each round of refining, save the request's decision map with `python -m kandu.intake map ID`", text)
        self.assertIn("continue refining from it", text)

    def test_kandu_task_and_didban_define_a_task_from_one_file(self):
        command = (ROOT / ".claude" / "commands" / "kandu-task.md").read_text(encoding="utf-8")
        self.assertIn("`prompts/defining.md`", command)
        self.assertNotIn("kandu.tasks", command)
        self.assertIn("`prompts/defining.md` says", (PROMPTS / "didban.md").read_text(encoding="utf-8"))

    def test_refining_ends_on_an_empty_decision_map_and_a_shared_understanding(self):
        text = (PROMPTS / "defining.md").read_text(encoding="utf-8")
        for mark in ("✅", "❓", "⏳", "✂️"):
            self.assertIn(mark, text)
        self.assertIn("Finish when the map holds no ❓ or ⏳", text)
        self.assertIn("shared understanding", text)
        self.assertIn("why it matters", text)
        self.assertIn("recommended answer", text)
        self.assertNotIn("skips the interview", text)

    def test_a_request_of_several_tasks_writes_its_spec_first_on_the_users_yes(self):
        self.assertEqual(headings(PROMPTS / "templates" / "spec.md"), ["Problem", "Solution", "Decisions", "Out of scope"])
        text = (PROMPTS / "defining.md").read_text(encoding="utf-8")
        spec = text.index("A request of more than one task gets a **spec** issue first")
        self.assertIn("create it only on the user's yes", text[spec:])
        self.assertIn("--spec-issue", text[spec:])
        self.assertLess(spec, text.index("--spec #N"))
        self.assertLess(spec, text.index("7. Save the body"))

    def test_didban_keeps_to_the_project_the_user_named(self):
        text = (PROMPTS / "didban.md").read_text(encoding="utf-8")
        self.assertIn("One project at a time", headings(PROMPTS / "didban.md"))
        self.assertLess(text.index("## One project at a time"), text.index("in your first reply"))

    def test_a_fix_round_stays_on_its_findings(self):
        self.assertIn("Fix exactly the findings you are given", (PROMPTS / "worker.md").read_text(encoding="utf-8"))

    def test_the_scope_rule_keeps_guides_from_widening_a_task(self):
        for name in ("worker.md", "reviewer.md"):
            self.assertIn("even if the repo's guides describe it", (PROMPTS / name).read_text(encoding="utf-8"), name)

    def test_the_second_review_report_has_the_same_fields_and_adds_nothing(self):
        text = (PROMPTS / "second-review.md").read_text(encoding="utf-8")
        report = json.loads(re.search(r"```json\n(.+?)```", text, re.DOTALL).group(1))
        self.assertEqual(set(report), {"head_sha", "summary", "criteria", "earlier", "findings", "checked_ok", "door",
                                       "blast_radius", "brief", "user_brief", "wait_for_user"})
        self.assertEqual((report["findings"], report["criteria"]), ([], []))

    def test_the_notes_report_shows_every_field_code_needs(self):
        text = (PROMPTS / "splitter-notes.md").read_text(encoding="utf-8")
        note = json.loads(re.search(r"```json\n(.+?)```", text, re.DOTALL).group(1))["notes"][0]
        self.assertEqual(set(note), {"n", "size", "files", "door", "reason", "title", "body"})

    def test_didban_is_told_where_its_sources_are(self):
        self.assertIn("`didban.knowledge`", (PROMPTS / "didban.md").read_text(encoding="utf-8"))

    def test_links_in_the_design_exist(self):
        text = (ROOT / "DESIGN.md").read_text(encoding="utf-8")
        for name in re.findall(r"\]\(((?:prompts|docs)/[\w/.-]+|GLOSSARY\.md)\)", text):
            self.assertTrue((ROOT / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
