import tempfile
import unittest
from pathlib import Path

from kandu import agents
from kandu.config import Project


def project(**changes):
    values = dict(name="site", family="f", repos=["o/r"], run="clone", machine="m", path="~/w/site", test="npm test",
                  priority=1, max_parallel_workers=1, paused=False, daily_token_limit=1000)
    return Project(**(values | changes))


class ToolsTest(unittest.TestCase):
    def test_no_agent_reads_a_secrets_file(self):
        for _, denied in (agents.worker_tools(project(), "kandu/1-a"), agents.reviewer_tools(), agents.splitter_tools()):
            self.assertIn("Read(**/.env)", denied)
            self.assertIn("Read(**/.dev.vars)", denied)
            self.assertIn("Read(//proc/**)", denied)

    def test_a_worker_cannot_read_the_project_secrets(self):
        _, denied = agents.worker_tools(project(secrets_from="~/s/keys.txt"), "kandu/1-a")
        self.assertIn("Read(~/s/keys.txt)", denied)
        _, denied = agents.worker_tools(project(secrets_from="/s/keys.txt"), "kandu/1-a")
        self.assertIn("Read(//s/keys.txt)", denied)

    def test_only_a_token_task_is_told_of_its_tokens(self):
        item = project(task_tokens={"A_KEY": "making images"})
        prompt = agents.worker_prompt(item, "o/r", 1, "kandu/1-a", "r-1", tokens=True)
        self.assertIn("`A_KEY`: making images", prompt)
        self.assertNotIn("A_KEY", agents.worker_prompt(item, "o/r", 1, "kandu/1-a", "r-1"))

    def test_worker_may_push_only_its_own_branch(self):
        allowed, _ = agents.worker_tools(project(), "kandu/1-a")
        pushes = [t for t in allowed if "git push" in t]
        self.assertEqual(pushes, ["Bash(git push origin kandu/1-a)", "Bash(git push -u origin kandu/1-a)"])

    def test_worker_runs_the_projects_tests(self):
        allowed, _ = agents.worker_tools(project(), "kandu/1-a")
        self.assertIn("Bash(git rm *)", allowed)
        self.assertIn("Bash(rm *)", allowed)
        self.assertIn("Bash(npm test)", allowed)
        self.assertIn("Bash(npm test *)", allowed)

    def test_project_tools_are_added(self):
        allowed, _ = agents.worker_tools(project(allowed_tools=["Bash(npm install)"]), "kandu/1-a")
        self.assertIn("Bash(npm install)", allowed)

    def test_forbidden_areas_are_denied(self):
        _, denied = agents.worker_tools(project(forbidden=["migrations/", "src/pay.py"]), "kandu/1-a")
        for rule in ("Edit(./migrations/**)", "Write(./migrations/**)", "Edit(./src/pay.py)"):
            self.assertIn(rule, denied)

    def test_secrets_are_denied_to_both_roles(self):
        for _, denied in (agents.worker_tools(project(), "kandu/1-a"), agents.reviewer_tools()):
            self.assertIn("Read(~/.ssh/**)", denied)
            self.assertIn("Read(**/.env)", denied)
            self.assertIn("Bash(git push --force*)", denied)

    def test_reviewer_cannot_change_anything(self):
        allowed, denied = agents.reviewer_tools()
        self.assertFalse({"Edit", "Write"} & set(allowed))
        self.assertTrue({"Edit", "Write", "NotebookEdit"} <= set(denied))
        for tool in allowed:
            for word in ("commit", "push", "comment", "merge"):
                self.assertNotIn(word, tool)


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


if __name__ == "__main__":
    unittest.main()
