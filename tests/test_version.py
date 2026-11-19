import re
import tempfile
import unittest
from pathlib import Path

from kandu import version

ROOT = Path(__file__).resolve().parent.parent


class VersionTest(unittest.TestCase):
    def test_the_repo_holds_kandus_version_starting_at_1_0_0(self):
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "1.0.0")

    def test_the_install_reads_the_version_from_the_repo(self):
        self.assertEqual(version.read(), (ROOT / "VERSION").read_text(encoding="utf-8").strip())
        self.assertRegex(version.read(), r"^\d+\.\d+\.\d+$")

    def test_another_file_is_read_as_it_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "VERSION"
            path.write_text("2.3.4\n", encoding="utf-8")
            self.assertEqual(version.read(path), "2.3.4")

    def test_a_missing_file_reads_as_no_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(version.read(Path(tmp) / "VERSION"))
