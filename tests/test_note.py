import contextlib
import io
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from kandu import note
from kandu.config import load_config

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "config.example"


class NoteTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name) / "local"
        self.local.mkdir()
        shutil.copy(EXAMPLES / "kandu.example.yaml", self.local / "kandu.yaml")
        shutil.copy(EXAMPLES / "projects.example.yaml", self.local / "projects.yaml")

    def change(self, **values):
        path = self.local / "projects.yaml"
        data = yaml.safe_load(path.read_text())
        for key, value in values.items():
            if value is None:
                data["projects"][0].pop(key, None)
            else:
                data["projects"][0][key] = value
        path.write_text(yaml.safe_dump(data))

    def render(self):
        config = load_config(self.local)
        return note.render(config, config.projects[0])

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.argv", ["note", *args, "--local", str(self.local)]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = note.main()
        return code, out.getvalue(), err.getvalue()

    def test_a_project_with_a_board_and_forbidden_areas(self):
        self.change(family="other", board={"owner": "acme-org", "number": 7}, forbidden=["migrations/", "src/payments/"])
        text = self.render()
        self.assertIn("acme-org/website", text)
        self.assertIn("kandu/", text)
        self.assertIn("PR", text)
        self.assertIn("project 7 of `acme-org`", text)
        self.assertIn("`migrations/`", text)
        self.assertIn("`src/payments/`", text)
        breaks = text[text.index("## When something breaks"):]
        self.assertIn("project 7 of `acme-org`", breaks)
        self.assertIn("Didban", breaks)

    def test_a_project_without_a_board_or_forbidden_areas(self):
        self.change(board=None, forbidden=None)
        text = self.render()
        self.assertIn("no board yet", text)
        self.assertNotIn("project 7", text)
        self.assertIn("Didban", text[text.index("## When something breaks"):])
        forbidden = text[text.index("## Forbidden areas"):text.index("## When something breaks")]
        self.assertIn("None", forbidden)

    def test_it_says_its_file_and_that_the_repo_guides_point_to_it(self):
        text = self.render()
        self.assertIn(f"`{note.FILE}`", text)
        for guide in ("CLAUDE.md", "AGENTS.md"):
            self.assertIn(f"`{guide}`", text)
        self.assertIn(note.POINTER, text)
        self.assertIn(note.FILE, note.POINTER)

    def test_the_merge_mode_follows_the_config(self):
        self.change(auto_merge=True)
        self.assertIn("merges a PR by itself", self.render())
        self.change(auto_merge=False)
        self.assertIn("approves", self.render())
        self.assertNotIn("merges a PR by itself", self.render())

    def test_every_placeholder_is_filled(self):
        self.change(board=None, forbidden=None)
        for text in (self.render(),):
            self.assertNotRegex(text, r"[{}]")

    def test_the_template_is_generic(self):
        template = note.TEMPLATE.read_text(encoding="utf-8")
        examples = yaml.safe_load((EXAMPLES / "projects.example.yaml").read_text())["projects"]
        for project in examples:
            for word in (project["name"], project["machine"], project["path"], *project["repos"]):
                self.assertNotIn(word, template)
        self.assertNotRegex(template, r"https?://|@|~/|/home/")

    def test_the_command_prints_the_note_and_changes_nothing(self):
        before = {p: p.read_bytes() for p in self.local.rglob("*") if p.is_file()}
        code, out, err = self.run_cli("website")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("# "))
        self.assertIn("acme-org/website", out)
        after = {p: p.read_bytes() for p in self.local.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_an_unknown_project_is_refused(self):
        code, out, err = self.run_cli("nothing")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("nothing", err)

    def test_a_config_problem_is_refused(self):
        for path in self.local.iterdir():
            path.unlink()
        code, out, err = self.run_cli("website")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("config problem", err)

    def test_kandu_add_offers_the_note_as_a_task_only_on_yes(self):
        text = (ROOT / ".claude" / "commands" / "kandu-add.md").read_text(encoding="utf-8")
        step = next(line for line in text.splitlines() if "kandu.note" in line)
        self.assertIn("only if the user said yes", step)
        self.assertIn("/kandu-task", step)
        self.assertRegex(step, re.compile(r"never write", re.IGNORECASE))


if __name__ == "__main__":
    unittest.main()
