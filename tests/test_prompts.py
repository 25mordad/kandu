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
            "Goal", "Current behavior", "Reference", "Acceptance criteria", "How to test", "Out of scope", "Blocked by",
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
        self.assertIn("with `--project PROJECT` once you know which project it belongs to", text)

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
        self.assertEqual(headings(PROMPTS / "templates" / "spec.md"), ["Problem", "Solution", "Reference", "Decisions", "Out of scope"])
        text = (PROMPTS / "defining.md").read_text(encoding="utf-8")
        spec = text.index("A request of more than one task gets a **spec** issue first")
        self.assertIn("create it only on the user's yes", text[spec:])
        self.assertIn("--spec-issue", text[spec:])
        self.assertLess(spec, text.index("--spec #N"))
        self.assertLess(spec, text.index("7. Save the body"))

    def test_defining_names_the_source_and_covers_every_decision(self):
        text = (PROMPTS / "defining.md").read_text(encoding="utf-8")
        self.assertIn("File paths are allowed in the `Reference` section", text)
        self.assertIn("and only there", text)
        self.assertIn("each carry their own `Reference`, never only the spec", text)
        self.assertIn("checks the result against that reference", text)
        self.assertIn("read-only copy", text)
        coverage = text.index("**Coverage.**")
        self.assertIn("create nothing while a line has none", text[coverage:])
        self.assertIn("the spec included", text[coverage:])
        self.assertLess(coverage, text.index("6. Show the final text"))
        for name in ("issue.md", "spec.md"):
            self.assertIn("Reference", headings(PROMPTS / "templates" / name))

    def test_reference_is_in_the_templates_but_no_issue_or_spec_must_have_it(self):
        from kandu import tasks
        self.assertNotIn("Reference", tasks.required_sections())
        self.assertNotIn("Reference", tasks.spec_sections())
        tasks.check_body("\n".join(f"## {h}\nx" for h in tasks.required_sections()))

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


def numbered_step(text, n):
    return re.search(rf"^{n}\. .+?(?=^\d+\. |\Z)", text, re.MULTILINE | re.DOTALL).group(0)


class TwoStepDefiningTest(unittest.TestCase):
    FIELDS = ("`Blocked by`", "`Priority`", "`Kind`", "`Size`", "`Deadline`")

    def to_issue(self):
        return {name: (PROMPTS / name).read_text(encoding="utf-8") for name in ("to-spec.md", "to-ticket.md")}

    def test_the_interviews_last_summary_holds_the_seams_and_every_planned_task_with_its_fields(self):
        summary = numbered_step((PROMPTS / "grilling.md").read_text(encoding="utf-8"), 7)
        for words in ("testing seams", "planned tasks", "by hand", "`task_tokens`") + self.FIELDS:
            self.assertIn(words, summary)

    def test_to_issue_asks_nothing_and_waits_for_no_yes(self):
        for name, text in self.to_issue().items():
            for waiting in ("Then ask", "until the user", "the user has said yes", "the user says yes",
                            "as an assumption they can reject"):
                self.assertNotIn(waiting, text, name)
        self.assertIn("The user's yes to the interview's summary is the yes to the spec and every task",
                      self.to_issue()["to-spec.md"])

    def test_to_issue_creates_the_spec_then_every_task_in_order_then_closes_the_request(self):
        spec, ticket = self.to_issue()["to-spec.md"], self.to_issue()["to-ticket.md"]
        self.assertIn("with the command of step 4 of `prompts/defining.md` for a spec", spec)
        self.assertLess(ticket.index("--spec #N"), ticket.index("kandu.intake close ID"))
        self.assertIn("each before any task that names it under `Blocked by`", ticket)

    def test_a_field_the_interview_left_open_takes_a_stated_default_named_in_the_spec_as_an_assumption(self):
        spec = self.to_issue()["to-spec.md"]
        for field in self.FIELDS + ("by hand", "tokens"):
            self.assertRegex(spec, rf"(?m)^   - {re.escape(field)}: \S", field)
        self.assertIn("`## Assumptions`", spec)
        self.assertIn("each choice made by a default", spec)

    def test_to_issue_stops_only_without_a_finished_interview_and_points_to_it(self):
        texts = self.to_issue()
        self.assertEqual(sum(len(re.findall(r"\b[Ss]top", text)) for text in texts.values()), 1)
        first = numbered_step(texts["to-spec.md"], 1)
        self.assertIn("❓ or ⏳", first)
        self.assertIn("stop and point to `/kandu-grill-with-docs`", first)

    def test_a_rerun_with_the_spec_made_still_takes_the_tasks_before_creating_them(self):
        first = numbered_step(self.to_issue()["to-spec.md"], 1)
        self.assertIn("the spec exists already: do step 3, then go on with `prompts/to-ticket.md`", first)

    def test_to_issue_ends_on_one_table_of_what_it_created(self):
        text = self.to_issue()["to-ticket.md"]
        last = numbered_step(text, len(re.findall(r"(?m)^\d+\. ", text)))
        self.assertIn("one table", last)
        for column in ("link",) + self.FIELDS:
            self.assertIn(column, last)


class GlossaryInTheTaskTest(unittest.TestCase):
    def read(self, path):
        return (ROOT / path).read_text(encoding="utf-8")

    def test_to_issue_writes_the_words_and_decisions_into_the_task_that_first_uses_them(self):
        ticket = self.read("prompts/to-ticket.md")
        for words in ("one acceptance criterion", "the task whose change first uses", "`GLOSSARY.md`",
                      "`docs/adr/`", "in the form the file already uses",
                      "When the repo has no `GLOSSARY.md`, write no word criterion", "never one that creates it"):
            self.assertIn(words, ticket)

    def test_to_issue_adds_no_separate_task_to_record_them(self):
        for name in ("to-spec.md", "to-ticket.md"):
            self.assertNotIn("Record the glossary and decisions", self.read("prompts/" + name), name)

    def test_the_worker_adds_a_new_word_to_an_existing_glossary_and_never_creates_one(self):
        text = self.read("prompts/worker.md")
        self.assertIn("add each new project word your change names to the repo's `GLOSSARY.md` in the same PR", text)
        self.assertIn("Never create the file", text)

    def test_the_reviewer_reports_a_missing_word_as_a_note(self):
        notes = re.search(r"^## Notes, which do not block\n(.+?)(?=^## )",
                          self.read("prompts/reviewer.md"), re.MULTILINE | re.DOTALL).group(1)
        self.assertIn("a new project word the PR names but leaves out of the repo's existing `GLOSSARY.md`", notes)

    def test_the_design_says_where_a_tasks_words_and_decisions_are_written(self):
        design = self.read("DESIGN.md")
        self.assertNotIn("one more task of the spec records", design)
        self.assertIn("in the same PR as the change that first uses them", design)


if __name__ == "__main__":
    unittest.main()
