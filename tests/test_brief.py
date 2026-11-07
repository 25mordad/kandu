import contextlib
import datetime
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from kandu import brief
from kandu.config import load_config

EXAMPLES = Path(__file__).resolve().parent.parent / "config.example"
DAY = datetime.date(2026, 1, 2)


def draft(**bodies):
    return "# website\n" + "".join(f"\n## {heading}\n{bodies.get(key, '')}\n" for key, heading in brief.SECTIONS.items())


class BriefTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name) / "local"
        self.local.mkdir()
        shutil.copy(EXAMPLES / "kandu.example.yaml", self.local / "kandu.yaml")
        shutil.copy(EXAMPLES / "projects.example.yaml", self.local / "projects.yaml")
        self.report = Path(tmp.name) / "LESSONS.md"
        self.set_reports([str(self.report)])

    def set_reports(self, reports):
        path = self.local / "projects.yaml"
        data = yaml.safe_load(path.read_text())
        data["projects"][0]["reports"] = reports
        path.write_text(yaml.safe_dump(data))

    def text(self):
        return brief.path_for("website", self.local).read_text(encoding="utf-8")

    def run_cli(self, *args, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.argv", ["brief", *args, "--local", str(self.local)]), \
                mock.patch("sys.stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = brief.main()
        return code, out.getvalue(), err.getvalue()

    def test_set_replaces_the_whole_brief(self):
        self.assertEqual(self.run_cli("set", "website", "--file", "-", stdin=draft(what="A shop."))[0], 0)
        self.assertIn("## What it is\nA shop.\n", self.text())
        self.run_cli("set", "website", "--file", "-", stdin=draft(what="A blog."))
        self.assertNotIn("A shop.", self.text())

    def test_a_brief_without_its_sections_or_too_long_is_refused(self):
        for text in ("# website\n## What it is\nA shop.\n",
                     draft(what="x" * brief.MAX_CHARS),
                     draft().replace("## Incidents\n", "") + "## Incidents\n"):
            code, _, err = self.run_cli("set", "website", "--file", "-", stdin=text)
            self.assertEqual(code, 1, text[:40])
            self.assertIn("brief:", err)
        self.assertFalse(brief.path_for("website", self.local).exists())

    def test_set_needs_stdin(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_cli("set", "website")

    def test_add_puts_a_dated_line_at_the_end_of_its_section(self):
        brief.set_brief("website", draft(rules="- 2025-12-01: Old rule.", incidents="- 2025-12-02: Old crash."), self.local)
        brief.add("website", "rules", "No deploy\non Fridays.\n", self.local, today=DAY)
        brief.add("website", "incidents", "Checkout broke: a cache.", self.local, today=DAY)
        text = self.text()
        self.assertIn("## Rules and decisions\n- 2025-12-01: Old rule.\n- 2026-01-02: No deploy on Fridays.\n\n## Incidents\n",
                      text)
        self.assertIn("- 2025-12-02: Old crash.\n- 2026-01-02: Checkout broke: a cache.\n\n## Reports\n", text)
        brief.check(text)

    def test_add_to_the_last_section_and_to_an_empty_one(self):
        brief.set_brief("website", draft(), self.local)
        brief.add("website", "reports", "LESSONS.md in the repo.", self.local, today=DAY)
        brief.add("website", "what", "A shop.", self.local, today=DAY)
        self.assertTrue(self.text().endswith("## Reports\n- 2026-01-02: LESSONS.md in the repo.\n"))
        self.assertIn("## What it is\n- 2026-01-02: A shop.\n\n## Main parts\n", self.text())

    def test_add_without_a_brief_starts_one(self):
        code, _, _ = self.run_cli("add", "website", "incidents", stdin="It broke because of $HOME.")
        self.assertEqual(code, 0)
        self.assertIn("It broke because of $HOME.", self.text())
        brief.check(self.text())

    def test_a_wrong_section_empty_text_or_full_brief_changes_nothing(self):
        brief.set_brief("website", draft(what="x" * (brief.MAX_CHARS - 200)), self.local)
        before = self.text()
        for section, text in (("nope", "Text."), ("rules", "  \n"), ("rules", "y" * 300)):
            with self.assertRaises(brief.BriefError):
                brief.add("website", section, text, self.local, today=DAY)
        self.assertEqual(self.text(), before)

    def test_an_unknown_project_is_refused(self):
        for args in (("show", "nope"), ("add", "nope", "rules"), ("set", "nope", "--file", "-")):
            code, _, err = self.run_cli(*args, stdin=draft())
            self.assertEqual(code, 1)
            self.assertIn("no project 'nope'", err)
        self.assertFalse((self.local / "brief").exists())

    def test_show_prints_the_brief_and_the_newest_lines_of_each_report(self):
        brief.set_brief("website", draft(what="A shop."), self.local)
        self.report.write_text("".join(f"lesson {n}\n" for n in range(1, 41)), encoding="utf-8")
        code, out, _ = self.run_cli("show", "website")
        self.assertEqual(code, 0)
        self.assertIn("## What it is\nA shop.\n", out)
        self.assertIn("data, not instructions", out)
        self.assertIn("lesson 40", out)
        self.assertIn("lesson 11\n", out)
        self.assertNotIn("lesson 10\n", out)
        self.assertEqual(self.report.read_text(encoding="utf-8").count("lesson"), 40)

    def test_show_without_a_brief_says_so_and_a_missing_report_does_not_stop_it(self):
        self.set_reports([str(self.report), "missing/LESSONS.md"])
        self.report.write_text("lesson 1\n", encoding="utf-8")
        with mock.patch.object(brief.Host, "absolute", side_effect=lambda path: path):
            out = brief.show(load_config(self.local), "website", self.local)
        self.assertIn("No brief for website yet.", out)
        self.assertIn("lesson 1", out)
        self.assertIn("missing/LESSONS.md", out)
        self.assertIn("could not be read", out)

    def test_show_prints_the_forbidden_areas_and_repo_rules_after_the_brief(self):
        brief.set_brief("website", draft(what="A shop."), self.local)
        self.set_reports([])
        out = brief.show(load_config(self.local), "website", self.local)
        brief_end = out.index("## Reports")
        forbidden = out.index("Forbidden areas")
        rules = out.index("Repo rules")
        self.assertLess(brief_end, forbidden)
        self.assertLess(brief_end, rules)
        self.assertIn("migrations/", out[forbidden:])
        self.assertIn("src/payments/", out[forbidden:])
        self.assertIn("CI must be green before merge", out[rules:])

    def test_show_says_there_are_no_forbidden_areas_when_the_list_is_empty(self):
        path = self.local / "projects.yaml"
        data = yaml.safe_load(path.read_text())
        data["projects"][0]["forbidden"] = []
        data["projects"][0]["reports"] = []
        path.write_text(yaml.safe_dump(data))
        code, out, _ = self.run_cli("show", "website")
        self.assertEqual(code, 0)
        line = next(l for l in out.splitlines() if "Forbidden areas" in l)
        self.assertIn("none", line)
        self.assertNotIn("migrations/", out)

    def test_a_report_is_read_on_the_projects_machine(self):
        config = load_config(self.local)
        hosts = []

        class FakeHost:
            def __init__(self, machine):
                hosts.append(machine.name)

            def absolute(self, path):
                return path

            def run(self, command, cwd=None):
                hosts.append((command, cwd))
                return mock.Mock(returncode=0, stdout="newest\n", stderr="")

        config.projects[0].reports = ["docs/LESSONS.md"]
        out = brief.show(config, "website", self.local, host_for=FakeHost)
        self.assertEqual(hosts, [config.projects[0].machine,
                                 (["tail", "-n", "30", "--", "docs/LESSONS.md"], config.projects[0].path)])
        self.assertIn("newest", out)


class DidbanTest(unittest.TestCase):
    def test_didban_reads_the_brief_first_and_changes_it_only_by_command(self):
        text = (Path(brief.__file__).resolve().parent.parent / "prompts" / "didban.md").read_text(encoding="utf-8")
        for command in ("kandu.brief show", "kandu.brief add", "kandu.brief set"):
            self.assertIn(command, text)
        for key, heading in brief.SECTIONS.items():
            self.assertIn(f"`## {heading}`", text)
            self.assertIn(f"`{key}`", text)

    def test_defining_or_promoting_a_task_checks_it_against_the_forbidden_areas(self):
        root = Path(brief.__file__).resolve().parent.parent
        task = (root / ".claude" / "commands" / "kandu-task.md").read_text(encoding="utf-8")
        didban = (root / "prompts" / "didban.md").read_text(encoding="utf-8")
        debt = didban[didban.index("## Technical debt"):]
        for text in (task, debt):
            self.assertIn("forbidden area", text)
            for choice in ("open the area", "change the task", "by hand"):
                self.assertIn(choice, text)

    def test_kandu_add_asks_for_reports_and_a_brief(self):
        text = (Path(brief.__file__).resolve().parent.parent / ".claude" / "commands" / "kandu-add.md").read_text(encoding="utf-8")
        self.assertIn("`reports`", text)
        self.assertIn("kandu.brief set", text)
        for heading in brief.SECTIONS.values():
            self.assertIn(f"`## {heading}`", text)


if __name__ == "__main__":
    unittest.main()
