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

    def test_pr_template_states_the_merge_danger(self):
        text = (PROMPTS / "templates" / "pr.md").read_text(encoding="utf-8")
        self.assertEqual(headings(PROMPTS / "templates" / "pr.md"), ["Summary", "Evidence", "Merge Danger"])
        self.assertIn("**Door:**", text)
        self.assertIn("**Blast radius:**", text)


class PromptsTest(unittest.TestCase):
    def test_reviewer_has_both_axes(self):
        found = headings(PROMPTS / "reviewer.md")
        self.assertIn("Axis: Spec", found)
        self.assertIn("Axis: Standards", found)

    def test_files_named_in_prompts_exist(self):
        text = (PROMPTS / "worker.md").read_text(encoding="utf-8")
        for name in re.findall(r"`(prompts/[\w/.-]+)`", text):
            self.assertTrue((ROOT / name).exists(), name)

    def test_links_in_the_design_exist(self):
        text = (ROOT / "DESIGN.md").read_text(encoding="utf-8")
        for name in re.findall(r"\]\(((?:prompts|docs)/[\w/.-]+|GLOSSARY\.md)\)", text):
            self.assertTrue((ROOT / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
