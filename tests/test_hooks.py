import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / ".githooks"
TERMS_SCRIPT = HOOKS.parent / "kandu" / "privateterms.py"
TERM = "zzprivatezz"


class HooksTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.env = os.environ | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "core.hooksPath", str(HOOKS))
        self.commit("first.txt", "first\n")
        self.words(f"{TERM}\n")

    def git(self, *args, cwd=None, check=True):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env,
                              capture_output=True, text=True, check=check)

    def words(self, text):
        local = self.repo / "local"
        local.mkdir(exist_ok=True)
        (local / "private-words.txt").write_text(text, encoding="utf-8", newline="")

    def commit(self, name, content, message="change", cwd=None, verify=True):
        path = Path(cwd or self.repo) / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        self.git("add", name, cwd=cwd)
        flags = [] if verify else ["--no-verify"]
        return self.git("commit", "-q", *flags, "-m", message, cwd=cwd, check=False)

    def assertBlocked(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("private terms found", result.stderr)

    def test_clean_commit_passes(self):
        self.assertEqual(self.commit("clean.txt", "nothing here\n").returncode, 0)

    def test_no_words_file_passes(self):
        (self.repo / "local" / "private-words.txt").unlink()
        self.assertEqual(self.commit("a.txt", f"{TERM}\n").returncode, 0)

    def test_added_line(self):
        self.assertBlocked(self.commit("a.txt", f"has {TERM.upper()} in it\n"))

    def test_line_that_starts_with_two_plus_signs(self):
        self.assertBlocked(self.commit("a.txt", f"++ {TERM}\n"))

    def test_file_name(self):
        self.assertBlocked(self.commit(f"{TERM}.txt", "nothing here\n"))

    def test_renamed_file(self):
        self.git("mv", "first.txt", f"{TERM}.txt")
        self.assertBlocked(self.git("commit", "-q", "-m", "rename", check=False))

    def test_binary_file(self):
        self.assertBlocked(self.commit("a.bin", b"\x00\x01" + TERM.encode() + b"\x00"))

    def test_external_diff_does_not_hide_lines(self):
        self.git("config", "diff.external", "true")
        self.assertBlocked(self.commit("a.txt", f"{TERM}\n"))

    def test_words_file_with_crlf_comments_and_spaces(self):
        self.words(f"# a comment\r\n\r\n  {TERM}  \r\n")
        self.assertBlocked(self.commit("a.txt", f"{TERM}\n"))

    def test_commit_message(self):
        self.assertBlocked(self.commit("a.txt", "nothing here\n", message=f"about {TERM}"))

    def test_linked_worktree_uses_the_main_tree_words(self):
        tree = self.tmp / "linked"
        self.git("worktree", "add", "-q", "-b", "feature", str(tree))
        self.assertBlocked(self.commit("a.txt", f"{TERM}\n", cwd=tree))

    def test_push_blocks_what_the_commit_hooks_never_saw(self):
        remote = self.tmp / "remote.git"
        self.git("init", "-q", "--bare", "-b", "main", str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-q", "origin", "main")
        self.commit("a.txt", f"{TERM}\n", verify=False)
        self.assertBlocked(self.git("push", "origin", "main", check=False))

    def test_new_branch_push_scans_only_new_commits(self):
        remote = self.tmp / "remote.git"
        self.git("init", "-q", "--bare", "-b", "main", str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-q", "origin", "main")
        self.git("checkout", "-q", "-b", "feature")
        self.commit("a.txt", "nothing here\n")
        self.assertEqual(self.git("push", "origin", "feature", check=False).returncode, 0)

    def install_terms(self, script=None):
        """Gives the test repo an install's .venv and kandu/privateterms.py, as the hooks look for them."""
        venv = self.repo / ".venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        (venv / "python").chmod(0o755)
        (self.repo / "kandu").mkdir()
        if script is None:
            shutil.copy(TERMS_SCRIPT, self.repo / "kandu" / "privateterms.py")
        else:
            (self.repo / "kandu" / "privateterms.py").write_text(script, encoding="utf-8")
        (self.repo / ".git" / "info" / "exclude").write_text(".venv/\nkandu/\nlocal/\n", encoding="utf-8")

    def test_a_project_in_the_registry_is_private_without_a_word_for_it(self):
        self.install_terms()
        (self.repo / "local" / "projects.yaml").write_text(
            "projects:\n  - name: zzsecretproject\n    repos: [zzowner/zzrepo]\n", encoding="utf-8")
        self.assertEqual(self.commit("c.txt", "nothing here\n").returncode, 0)
        self.assertBlocked(self.commit("a.txt", "the zzsecretproject repo\n"))
        self.git("reset", "-q")
        self.assertBlocked(self.commit("b.txt", "see zzowner/zzrepo\n"))

    def test_the_user_words_still_count_with_the_terms_script(self):
        self.install_terms()
        self.assertBlocked(self.commit("a.txt", f"{TERM}\n"))

    def test_a_terms_script_that_fails_blocks_everything(self):
        self.install_terms(script="import sys\nsys.exit(3)\n")
        result = self.commit("a.txt", "nothing here\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not read the private terms", result.stderr)

    def test_push_blocks_a_private_author(self):
        remote = self.tmp / "remote.git"
        self.git("init", "-q", "--bare", "-b", "main", str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-q", "origin", "main")
        self.git("config", "user.email", f"{TERM}@example.com")
        self.commit("a.txt", "nothing here\n", verify=False)
        self.assertBlocked(self.git("push", "origin", "main", check=False))

    def test_push_of_clean_commits_passes(self):
        remote = self.tmp / "remote.git"
        self.git("init", "-q", "--bare", "-b", "main", str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.commit("a.txt", "nothing here\n")
        self.assertEqual(self.git("push", "origin", "main", check=False).returncode, 0)


if __name__ == "__main__":
    unittest.main()
