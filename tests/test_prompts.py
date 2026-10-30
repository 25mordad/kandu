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

    def test_pr_template_states_the_blast_radius(self):
        self.assertEqual(headings(PROMPTS / "templates" / "pr.md"), ["Summary", "Evidence", "Blast radius"])


class PromptsTest(unittest.TestCase):
    def test_reviewer_has_the_review_rules(self):
        found = headings(PROMPTS / "reviewer.md")
        for heading in ("Blockers", "Do not report", "Tests", "Verify each blocker before you report it",
                        "Companion PRs", "Later rounds", "Merge danger", "Report"):
            self.assertIn(heading, found)

    def test_reviewer_report_is_valid_json_with_the_needed_fields(self):
        text = (PROMPTS / "reviewer.md").read_text(encoding="utf-8")
        report = json.loads(re.search(r"```json\n(.+?)```", text, re.DOTALL).group(1))
        self.assertEqual(set(report), {"head_sha", "summary", "criteria", "earlier", "findings",
                                       "checked_ok", "door", "blast_radius", "brief"})
        self.assertEqual(set(report["findings"][0]), {"n", "path", "line", "blocker", "body"})

    def test_agents_in_other_projects_get_no_path_into_this_repo(self):
        for name in ("worker.md", "reviewer.md"):
            text = (PROMPTS / name).read_text(encoding="utf-8")
            self.assertNotIn("prompts/", text, name)
            self.assertNotIn("DESIGN.md", text, name)

    def test_the_worker_fixes_every_finding(self):
        self.assertIn("Fix every finding", (PROMPTS / "worker.md").read_text(encoding="utf-8"))

    def test_didban_is_told_where_its_sources_are(self):
        self.assertIn("`didban.knowledge`", (PROMPTS / "didban.md").read_text(encoding="utf-8"))

    def test_links_in_the_design_exist(self):
        text = (ROOT / "DESIGN.md").read_text(encoding="utf-8")
        for name in re.findall(r"\]\(((?:prompts|docs)/[\w/.-]+|GLOSSARY\.md)\)", text):
            self.assertTrue((ROOT / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
