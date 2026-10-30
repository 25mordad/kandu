import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kandu.config import ConfigError, DidbanBrain, DidbanConfig, load_didban
from kandu.didban import check_brain, claude_args, remote_path

EXAMPLES = Path(__file__).resolve().parent.parent / "config.example"
DIDBAN = DidbanConfig(brain=DidbanBrain(reach="ssh server", install_dir="~/my kandu"))


class LoadDidbanTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)

    def test_example_is_valid(self):
        shutil.copy(EXAMPLES / "didban.example.yaml", self.local / "didban.yaml")
        self.assertEqual(load_didban(self.local).brain.reach, "ssh server")

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load_didban(self.local)

    def test_reach_must_be_ssh(self):
        (self.local / "didban.yaml").write_text("brain: {reach: local, install_dir: ~/kandu}")
        with self.assertRaises(ConfigError) as caught:
            load_didban(self.local)
        self.assertIn("brain.reach", caught.exception.problems[0])


class RemotePathTest(unittest.TestCase):
    def test_tilde_stays_active_and_spaces_are_quoted(self):
        self.assertEqual(remote_path("~/my kandu"), "\"$HOME\"/'my kandu'")

    def test_absolute_path(self):
        self.assertEqual(remote_path("/opt/kandu"), "/opt/kandu")


class CheckBrainTest(unittest.TestCase):
    def run_with(self, result):
        with mock.patch("kandu.didban.subprocess.run", **result) as run:
            return check_brain(DIDBAN.brain), run

    def test_reachable(self):
        done = subprocess.CompletedProcess([], 0, "config ok: 2 machine(s), 0 project(s)\n", "")
        (ok, message), run = self.run_with({"return_value": done})
        self.assertTrue(ok)
        self.assertIn("config ok", message)
        args = run.call_args.args[0]
        self.assertEqual(args[0], "ssh")
        self.assertIn("server", args)

    def test_ssh_failure(self):
        done = subprocess.CompletedProcess([], 255, "", "ssh: connect: Connection timed out\n")
        (ok, message), _ = self.run_with({"return_value": done})
        self.assertFalse(ok)
        self.assertIn("timed out", message)

    def test_timeout(self):
        (ok, _), _ = self.run_with({"side_effect": subprocess.TimeoutExpired("ssh", 20)})
        self.assertFalse(ok)


class ClaudeArgsTest(unittest.TestCase):
    def test_role_setup_and_blocked_tools(self):
        args = claude_args(DIDBAN, "brain reachable", ["-p", "hello"])
        self.assertEqual(args[:3], ["claude", "-p", "hello"])
        prompt = args[args.index("--append-system-prompt") + 1]
        self.assertIn("You are Didban", prompt)
        self.assertIn("ssh server", prompt)
        self.assertIn("brain reachable", prompt)
        self.assertIn("Edit,Write,NotebookEdit", args)


if __name__ == "__main__":
    unittest.main()
