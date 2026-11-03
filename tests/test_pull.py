import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kandu.pull import pull


def git(path, *args):
    subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                   capture_output=True)


class PullTest(unittest.TestCase):
    """Real repositories: an origin, the live copy, and a second clone that pushes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin, self.live, self.other = self.tmp / "origin.git", self.tmp / "live", self.tmp / "other"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.other)], check=True, capture_output=True)
        self.commit(self.other, "page.txt", "one\n")
        git(self.other, "push", "-q", "origin", "HEAD:main")
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.live)], check=True, capture_output=True)

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def commit(self, repo, name, text):
        (repo / name).write_text(text)
        git(repo, "add", name)
        git(repo, "commit", "-q", "-m", name)

    def head(self, repo):
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout

    def conflict(self):
        self.commit(self.other, "page.txt", "from the PR\n")
        git(self.other, "push", "-q", "origin", "HEAD:main")
        self.commit(self.live, "page.txt", "edited on the server\n")

    def test_a_clean_pull_brings_the_copy_up_to_date(self):
        self.commit(self.other, "page.txt", "two\n")
        git(self.other, "push", "-q", "origin", "HEAD:main")
        self.assertEqual(pull(self.live)[0], 0)
        self.assertEqual((self.live / "page.txt").read_text(), "two\n")

    def test_a_conflicting_merge_is_aborted_and_the_copy_stays(self):
        self.conflict()
        before = self.head(self.live)
        code, line = pull(self.live)
        self.assertEqual(code, 1)
        self.assertIn("merge was aborted", line)
        self.assertEqual(self.head(self.live), before)
        self.assertEqual((self.live / "page.txt").read_text(), "edited on the server\n")
        self.assertFalse((self.live / ".git" / "MERGE_HEAD").exists())

    def test_a_conflicting_rebase_is_aborted_too(self):
        self.conflict()
        before = self.head(self.live)
        code, line = pull(self.live, ["--rebase", "--autostash", "-q"])
        self.assertEqual(code, 1)
        self.assertIn("rebase was aborted", line)
        self.assertEqual(self.head(self.live), before)
        self.assertEqual((self.live / "page.txt").read_text(), "edited on the server\n")

    def test_it_runs_as_a_file_from_the_deploys_folder(self):
        self.conflict()
        script = Path(__file__).resolve().parent.parent / "kandu" / "pull.py"
        ran = subprocess.run([sys.executable, "-I", str(script), str(self.live)], cwd=self.tmp, capture_output=True,
                             text=True)
        self.assertEqual(ran.returncode, 1)
        self.assertIn("merge was aborted", ran.stderr)

    def test_not_a_checkout(self):
        self.assertEqual(pull(self.tmp / "nowhere")[0], 2)
