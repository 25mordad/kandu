import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kandu.config import ConfigError, DidbanBrain, DidbanConfig, load_didban
from kandu.didban import check_brain, claude_args, main, remote_path

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

    def test_reach_is_one_alias(self):
        for reach in ("ssh 'server", "ssh -oProxyCommand=x server", "ssh server extra"):
            (self.local / "didban.yaml").write_text(f'brain: {{reach: "{reach}", install_dir: ~/kandu}}')
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
        (state, message), run = self.run_with({"return_value": done})
        self.assertEqual(state, "ok")
        self.assertIn("config ok", message)
        args = run.call_args.args[0]
        self.assertEqual(args[:1] + args[-2:], [
            "ssh", "server", "cd \"$HOME\"/'my kandu' && .venv/bin/python -m kandu.config",
        ])

    def test_login_banner_does_not_hide_the_result(self):
        done = subprocess.CompletedProcess([], 0, "config ok: 2 machine(s), 0 project(s)\n", "Welcome to server\n")
        (state, message), _ = self.run_with({"return_value": done})
        self.assertEqual(state, "ok")
        self.assertIn("config ok", message)

    def test_ssh_failure(self):
        done = subprocess.CompletedProcess([], 255, "", "ssh: connect: Connection timed out\n")
        (state, message), _ = self.run_with({"return_value": done})
        self.assertEqual(state, "unreachable")
        self.assertIn("timed out", message)

    def test_config_problem_is_not_reported_as_unreachable(self):
        done = subprocess.CompletedProcess([], 1, "", "2 problem(s) in config:\n  - a\n  - b\n")
        (state, message), _ = self.run_with({"return_value": done})
        self.assertEqual(state, "problem")
        self.assertEqual(message, "2 problem(s) in config: - a - b")

    def test_missing_venv_is_not_reported_as_unreachable(self):
        done = subprocess.CompletedProcess([], 127, "", "bash: .venv/bin/python: No such file\n")
        (state, _), _ = self.run_with({"return_value": done})
        self.assertEqual(state, "problem")

    def test_timeout(self):
        (state, _), _ = self.run_with({"side_effect": subprocess.TimeoutExpired("ssh", 20)})
        self.assertEqual(state, "unreachable")

    def test_ssh_not_installed(self):
        (state, message), _ = self.run_with({"side_effect": FileNotFoundError()})
        self.assertEqual(state, "unreachable")
        self.assertIn("not installed", message)


class MainTest(unittest.TestCase):
    def run_main(self, check, argv=(), **run):
        err = io.StringIO()
        run = run or {"return_value": subprocess.CompletedProcess([], 0)}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("kandu.didban.UNREPORTED", Path(tmp) / "unreported"), \
             mock.patch("kandu.didban.load_didban", return_value=DIDBAN), \
             mock.patch("kandu.didban.check_brain", return_value=check), \
             mock.patch("kandu.didban._waiting", return_value="none"), \
             mock.patch("kandu.didban.report_sessions") as self.reports, \
             mock.patch("kandu.didban.signal.signal"), \
             mock.patch("kandu.didban.subprocess.run", **run) as run_call, \
             mock.patch("sys.argv", ["didban", *argv]), \
             contextlib.redirect_stderr(err):
            return main(), run_call, err.getvalue()

    def prompt(self, run_call):
        args = run_call.call_args.args[0]
        return args[args.index("--append-system-prompt") + 1]

    def test_healthy_brain(self):
        _, exec_call, err = self.run_main(("ok", "config ok"))
        self.assertIn("brain reachable; config ok", self.prompt(exec_call))
        self.assertEqual(err, "")

    def test_config_problem_is_named_to_the_session_and_the_user(self):
        _, exec_call, err = self.run_main(("problem", "1 problem(s) in config"))
        self.assertIn("Kandu's check failed: 1 problem(s) in config", self.prompt(exec_call))
        self.assertIn("1 problem(s) in config", err)

    def test_unreachable_brain_still_opens_the_session(self):
        _, exec_call, err = self.run_main(("unreachable", "no answer in time"))
        self.assertIn("brain NOT reachable", self.prompt(exec_call))
        self.assertIn("no answer in time", err)

    def test_a_session_is_reported_when_it_ends(self):
        _, run_call, _ = self.run_main(("unreachable", "no answer in time"))
        self.assertIn("--session-id", run_call.call_args.args[0])
        self.reports.assert_called_once()

    def test_what_a_closed_window_left_is_reported_at_the_start(self):
        self.run_main(("ok", "config ok"))
        self.assertEqual(self.reports.call_count, 2)

    def test_a_resumed_session_keeps_its_own_id(self):
        _, run_call, _ = self.run_main(("ok", "config ok"), ["--continue"])
        self.assertNotIn("--session-id", run_call.call_args.args[0])

    def test_the_exit_code_of_claude_is_kept(self):
        code, _, _ = self.run_main(("ok", "config ok"), return_value=subprocess.CompletedProcess([], 3))
        self.assertEqual(code, 3)

    def test_claude_not_installed(self):
        code, _, err = self.run_main(("ok", "config ok"), side_effect=FileNotFoundError())
        self.assertEqual(code, 1)
        self.assertIn("claude is not installed", err)

    def test_config_problem_on_this_machine(self):
        err = io.StringIO()
        with mock.patch("kandu.didban.load_didban", side_effect=ConfigError(["brain: missing"])), \
             contextlib.redirect_stderr(err):
            self.assertEqual(main(), 1)
        self.assertIn("brain: missing", err.getvalue())


class ClaudeArgsTest(unittest.TestCase):
    def test_role_setup_and_blocked_tools(self):
        args = claude_args(DIDBAN, "brain reachable", ["-p", "hello"])
        self.assertEqual(args[:3], ["claude", "-p", "hello"])
        prompt = args[args.index("--append-system-prompt") + 1]
        self.assertIn("You are Didban", prompt)
        self.assertIn("ssh server", prompt)
        self.assertIn("brain reachable", prompt)
        self.assertIn("Edit,Write,NotebookEdit", args)

    def test_session_is_named_and_remote(self):
        args = claude_args(DIDBAN, "brain reachable", [])
        self.assertEqual(args[args.index("--name") + 1], "Kandu")
        self.assertEqual(args[args.index("--remote-control") + 1], "Kandu")


if __name__ == "__main__":
    unittest.main()
