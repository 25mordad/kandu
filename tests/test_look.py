import tempfile
import unittest
from pathlib import Path

from kandu import look


class LookTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "demo").mkdir()
        (self.root / "demo" / "a.html").write_text("<p>a</p>")

    def test_a_page_inside_the_folder_is_opened(self):
        self.assertEqual(look.page_path("demo/a.html", self.root), (self.root / "demo" / "a.html").resolve())

    def test_nothing_outside_the_folder_is_opened(self):
        self.assertIsNone(look.page_path("../../etc/passwd", self.root))
        self.assertIsNone(look.page_path("/etc/hostname", self.root))
        (self.root / "out").symlink_to("/etc")
        self.assertIsNone(look.page_path("out/hostname", self.root))

    def test_a_folder_or_a_missing_file_is_not_a_page(self):
        self.assertIsNone(look.page_path("demo", self.root))
        self.assertIsNone(look.page_path("demo/b.html", self.root))

    def test_times(self):
        self.assertEqual(look.times("2, 4.5,7"), [2.0, 4.5, 7.0])
        self.assertEqual(look.times(""), [])

    def test_a_refused_page_fails_without_a_browser(self):
        self.assertEqual(look.main(["/etc/hostname"]), 1)


if __name__ == "__main__":
    unittest.main()
