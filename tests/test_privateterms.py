import tempfile
import unittest
from pathlib import Path

from kandu.privateterms import all_terms, config_terms

KANDU_YAML = """
brain:
  machine: brainbox
  work_dir: ~/kandu-work
  install_dir: ~/code/kandu
machines:
  - name: brainbox
    reach: local
  - name: laptop
    reach: ssh alice@studio-mac
github:
  account: someone
  owners: [someone, acme-org]
"""

PROJECTS_YAML = """
projects:
  - name: website
    family: acme
    repos: [acme-org/website]
    board: {owner: acme-org, number: 1}
    machine: brainbox
    path: ~/sites/acme-website
  - name: kandu
    family: kandu
    repos: [someone/kandu-private]
    path: /home/alice/code/kandu
"""


class PrivateTermsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.local = self.root / "local"
        self.local.mkdir()
        (self.local / "kandu.yaml").write_text(KANDU_YAML, encoding="utf-8")
        (self.local / "projects.yaml").write_text(PROJECTS_YAML, encoding="utf-8")
        (self.local / ".env").write_text("TOKEN=abcdefgh12345\nSHORT=abc\n# NOTE=commented-out-value\n", encoding="utf-8")
        (self.local / "private-words.txt").write_text("# words\nAlice\n\n", encoding="utf-8")
        examples = self.root / "config.example"
        examples.mkdir()
        (examples / "kandu.example.yaml").write_text("work_dir: ~/kandu-work\nmachines: [laptop]\n", encoding="utf-8")

    def test_the_setup_gives_its_terms(self):
        terms = config_terms(self.local)
        for term in ["brainbox", "studio-mac", "alice", "acme-org", "acme-org/website", "website", "acme",
                     "~/sites/acme-website", "/sites/acme-website", "~/code/kandu", "someone/kandu-private",
                     "kandu-private", "abcdefgh12345"]:
            self.assertIn(term, terms)

    def test_the_account_the_tool_and_short_values_are_left_out(self):
        terms = [term.lower() for term in config_terms(self.local)]
        for term in ["someone", "kandu", "abc", "local", "commented-out-value"]:
            self.assertNotIn(term, terms)

    def test_a_term_the_public_examples_use_is_left_out(self):
        terms = config_terms(self.local)
        self.assertNotIn("laptop", terms)
        self.assertNotIn("~/kandu-work", terms)

    def test_a_home_path_is_given_in_both_spellings(self):
        terms = config_terms(self.local)
        self.assertIn("/home/alice/code/kandu", terms)
        self.assertIn("~/code/kandu", terms)

    def test_user_words_come_first_and_each_term_once(self):
        terms = all_terms(self.local)
        self.assertEqual(terms[0], "Alice")
        self.assertEqual(len(terms), len({term.lower() for term in terms}))

    def test_empty_local_gives_nothing(self):
        empty = self.root / "empty" / "local"
        empty.mkdir(parents=True)
        self.assertEqual(all_terms(empty), [])


if __name__ == "__main__":
    unittest.main()
