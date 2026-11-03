import tempfile
import unittest
from pathlib import Path

from kandu import agents
from kandu.config import Project, Rules


def project(**changes):
    values = dict(name="site", family="f", repos=["o/r"], run="clone", machine="m", path="~/w/site", test="npm test",
                  priority=1, max_parallel_workers=1, paused=False, daily_token_limit=1000)
    return Project(**(values | changes))


RULES = Rules(allow=["Read", "Bash(git push origin {branch})"], deny=["Read(**/.env)"])


class ToolsTest(unittest.TestCase):
    def test_with_no_rules_a_role_may_do_everything(self):
        for allowed, denied in (agents.worker_tools(project(secrets_from="~/s/keys.txt"), "kandu/1-a"),
                                agents.live_check_tools(project(allowed_tools=["Bash(npm install)"])),
                                agents.role_tools()):
            self.assertIsNone(allowed)
            self.assertEqual(denied, [])

    def test_forbidden_areas_hold_even_with_no_rules(self):
        for rules in (None, RULES):
            _, denied = agents.worker_tools(project(forbidden=["migrations/", "src/pay.py"]), "kandu/1-a", rules)
            self.assertIn("Edit(./migrations/**)", denied)
            self.assertIn("Edit(./src/pay.py)", denied)

    def test_rules_come_from_the_config_with_the_branch_filled_in(self):
        allowed, denied = agents.worker_tools(project(), "kandu/1-a", RULES)
        self.assertIn("Bash(git push origin kandu/1-a)", allowed)
        self.assertIn("Read(**/.env)", denied)
        self.assertEqual(agents.role_tools(RULES), (["Read", "Bash(git push origin )"], ["Read(**/.env)"]))

    def test_a_limited_worker_runs_the_projects_tests_and_tools(self):
        allowed, _ = agents.worker_tools(project(allowed_tools=["Bash(npm install)"]), "kandu/1-a", RULES)
        for tool in ("Bash(npm test)", "Bash(npm test *)", "Bash(npm install)"):
            self.assertIn(tool, allowed)
        allowed, _ = agents.live_check_tools(project(allowed_tools=["Bash(npm install)"]), RULES)
        self.assertIn("Bash(npm install)", allowed)

    def test_a_limited_worker_cannot_read_the_project_secrets(self):
        _, denied = agents.worker_tools(project(secrets_from="~/s/keys.txt"), "kandu/1-a", RULES)
        self.assertIn("Read(~/s/keys.txt)", denied)
        _, denied = agents.worker_tools(project(secrets_from="/s/keys.txt"), "kandu/1-a", RULES)
        self.assertIn("Read(//s/keys.txt)", denied)

    def test_rules_are_not_changed_by_a_run(self):
        rules = Rules(allow=["Read"], deny=[])
        agents.worker_tools(project(allowed_tools=["Bash(x)"]), "kandu/1-a", rules)
        self.assertEqual(rules.allow, ["Read"])


class PromptTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)

    def worker(self, **kwargs):
        return agents.worker_prompt(project(forbidden=["migrations/"]), "o/r", 12, "kandu/12-a", "r-1",
                                    local_dir=self.local, **kwargs)

    def test_first_run_has_the_facts_and_the_pr_template(self):
        text = self.worker()
        for part in ("`o/r`", "#12", "`kandu/12-a`", "`r-1`", "`npm test`", "migrations/", "## Blast radius",
                     "`CLAUDE.md`"):
            self.assertIn(part, text)

    def test_a_fix_round_has_the_findings_and_no_template(self):
        text = self.worker(findings=[{"n": 4, "body": "This breaks."}], pr=7)
        self.assertIn("This breaks.", text)
        self.assertIn("PR #7", text)
        self.assertNotIn("## Blast radius", text)

    def test_project_memory_is_included_only_if_it_exists(self):
        self.assertNotIn("## Project memory", self.worker())
        (self.local / "memory").mkdir()
        (self.local / "memory" / "site.md").write_text("a lesson", encoding="utf-8")
        self.assertIn("a lesson", self.worker())

    def test_reviewer_gets_the_head_criteria_and_companions(self):
        text = agents.reviewer_prompt(project(), "o/r", 12, 7, "abc123", "fa", [("o/b", 9)],
                                      ["It stops"], 2, local_dir=self.local)
        for part in ("`abc123`", "gh pr diff 7 --repo o/r", "fa", "o/b#9", "1. It stops", "from 2"):
            self.assertIn(part, text)

    def test_a_second_review_gets_the_items_and_nothing_to_widen_them(self):
        (self.local / "memory").mkdir()
        (self.local / "memory" / "site.md").write_text("a lesson", encoding="utf-8")
        text = agents.second_review_prompt("o/r", 12, 7, "abc123", "fa", [{"n": 1, "body": "item one"}])
        self.assertIn("item one", text)
        self.assertNotIn("a lesson", text)
        self.assertNotIn("## Blockers", text)


class QueenReportTest(unittest.TestCase):
    def test_a_good_report_is_taken(self):
        text = '{"brief": "b", "decisions": [{"task": "o/r#1", "action": "retry", "note": "n"}]}'
        self.assertEqual(agents.parse_queen_report(text)["brief"], "b")

    def test_a_park_decision_names_its_note(self):
        text = '{"brief": "b", "decisions": [{"task": "o/r#1", "action": "park", "finding": 2, "note": "n"}]}'
        self.assertEqual(agents.parse_queen_report(text)["decisions"][0]["finding"], 2)
        with self.assertRaises(agents.ReplyError):
            agents.parse_queen_report(text.replace('"finding": 2, ', ""))

    def test_an_approve_decision_names_its_head(self):
        text = '{"brief": "b", "decisions": [{"task": "o/r#1", "action": "approve", "head_sha": "abc", "note": "n"}]}'
        self.assertEqual(agents.parse_queen_report(text)["decisions"][0]["head_sha"], "abc")
        with self.assertRaises(agents.ReplyError):
            agents.parse_queen_report(text.replace('"head_sha": "abc", ', ""))

    def test_a_bad_report_is_refused(self):
        for text in ('no', '{}', '{"brief": "", "decisions": []}', '{"brief": "b"}',
                     '{"brief": "b", "decisions": [{"task": "o/r#1", "action": "merge", "note": "n"}]}',
                     '{"brief": "b", "decisions": [{"task": "o/r#1", "action": "retry", "note": " "}]}'):
            with self.assertRaises(agents.ReplyError):
                agents.parse_queen_report(text)


class WorkerReplyTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(agents.parse_worker_reply('{"status": "pr_opened", "pr": 3}\n')["pr"], 3)
        self.assertEqual(agents.parse_worker_reply('{"status": "needs_info", "question": "q"}')["question"], "q")
        self.assertEqual(agents.parse_worker_reply('{"status": "blocked", "reason": "r"}')["reason"], "r")

    def test_invalid(self):
        for text in ("Done!", "[]", '{"status": "done"}', '{"status": "needs_info"}', '{"status": "blocked"}'):
            with self.assertRaises(agents.ReplyError):
                agents.parse_worker_reply(text)



class WorkerReplyAfterSummaryTest(unittest.TestCase):
    def test_a_summary_above_the_json_line_is_read(self):
        reply = agents.parse_worker_reply('Opened the PR.\n\n- one change\n\n{"status": "pr_opened", "pr": 6}\n')
        self.assertEqual(reply, {"status": "pr_opened", "pr": 6})

    def test_text_with_no_json_line_is_still_refused(self):
        with self.assertRaises(agents.ReplyError):
            agents.parse_worker_reply('Opened the PR.\nAll done.')


if __name__ == "__main__":
    unittest.main()


class OpenBlockersTest(unittest.TestCase):
    def test_a_worker_hears_of_blockers_not_merged_yet(self):
        from kandu.config import load_config
        import shutil, tempfile
        from pathlib import Path
        root = Path(tempfile.mkdtemp())
        for name in ("kandu", "projects"):
            shutil.copy(Path(__file__).resolve().parent.parent / "config.example" / f"{name}.example.yaml",
                        root / f"{name}.yaml")
        project = load_config(root).projects[0]
        text = agents.worker_prompt(project, "o/r", 3, "b", "r-1", local_dir=root, open_blockers=["o/r#2"])
        self.assertIn("blocked by o/r#2", text)
        self.assertNotIn("Blockers not merged", agents.worker_prompt(project, "o/r", 3, "b", "r-1", local_dir=root))
