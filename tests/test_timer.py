import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kandu.config import load_config
from kandu import compat, launch, timer, wintimer
from kandu.timer import render_dashboard, render_service, render_timer

EXAMPLES = Path(__file__).resolve().parent.parent / "config.example"


class TimerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        local = Path(tmp.name)
        shutil.copy(EXAMPLES / "kandu.example.yaml", local / "kandu.yaml")
        shutil.copy(EXAMPLES / "projects.example.yaml", local / "projects.yaml")
        self.config = load_config(local)
        linux = mock.patch("kandu.compat.WINDOWS", False)  # these check the systemd units, on any system
        linux.start()
        self.addCleanup(linux.stop)

    def test_service_runs_from_the_venv(self):
        self.assertIn('ExecStart="%h/kandu/.venv/bin/python" -m kandu.supervisor cycle', render_service(self.config))

    def test_a_oneshot_is_limited_by_its_start_timeout(self):
        # RuntimeMaxSec has no effect on Type=oneshot.
        text = render_service(self.config)
        self.assertIn("Type=oneshot", text)
        self.assertIn("TimeoutStartSec=55min", text)
        self.assertNotIn("RuntimeMaxSec", text)

    def test_dashboard_runs_from_the_venv_with_its_token_file(self):
        text = render_dashboard(self.config)
        self.assertIn('ExecStart="%h/kandu/.venv/bin/python" -m kandu.dashboard', text)
        self.assertIn("EnvironmentFile=-%h/kandu/local/.env", text)

    def test_timer_uses_the_wake_interval(self):
        self.config.settings.supervisor.wake_every_minutes = 30
        self.assertIn("OnUnitActiveSec=30min", render_timer(self.config))

    def test_a_cycle_starts_unless_one_is_running(self):
        for state, expected, calls in (("inactive\n", True, 2), ("activating\n", None, 1)):
            with mock.patch("kandu.timer.subprocess.run") as run:
                run.return_value = mock.Mock(stdout=state, returncode=0)
                self.assertIs(timer.start_cycle(), expected)
                self.assertEqual(run.call_count, calls)

    def test_timer_on_reads_the_timer_unit(self):
        for state, expected in (("active\n", True), ("inactive\n", False)):
            with mock.patch("kandu.timer.subprocess.run") as run:
                run.return_value = mock.Mock(stdout=state)
                self.assertIs(timer.timer_on(), expected)
                self.assertIn("kandu.timer", run.call_args.args[0])

    def test_restart_re_arms_the_timer_and_restarts_the_dashboard_from_outside(self):
        with mock.patch.object(timer, "install") as install, mock.patch.object(timer.subprocess, "run") as run:
            timer.restart(self.config)
        install.assert_called_once_with(self.config)
        first, second = (c.args[0] for c in run.call_args_list)
        self.assertEqual(first, ["systemctl", "--user", "try-restart", "kandu.timer", "kandu-sync.timer"])
        self.assertEqual(second[:2], ["systemd-run", "--user"])
        self.assertEqual(second[-1], "kandu-dashboard.service")

    def test_dashboard_flag_never_starts_the_timer(self):
        with mock.patch.object(timer, "load_config", return_value=self.config), \
                mock.patch.object(timer, "install") as install, \
                mock.patch.object(timer.subprocess, "run") as run, \
                mock.patch("sys.argv", ["timer", "--dashboard"]):
            self.assertEqual(timer.main(), 0)
        install.assert_called_once()
        run.assert_called_once_with(["systemctl", "--user", "enable", "--now", "kandu-dashboard.service"], check=True)


class WindowsTimerTest(unittest.TestCase):
    """Task Scheduler stands in for `systemd` on Windows. Nothing here talks to Task Scheduler itself."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        shutil.copy(EXAMPLES / "kandu.example.yaml", self.root / "kandu.yaml")
        shutil.copy(EXAMPLES / "projects.example.yaml", self.root / "projects.yaml")
        self.config = load_config(self.root)
        self.config.settings.brain.install_dir = str(self.root / "kandu")

    def test_the_supervisor_wakes_on_the_interval_from_the_venv(self):
        self.config.settings.supervisor.wake_every_minutes = 30
        xml = wintimer.render_task(self.config, wintimer.SUPERVISOR)
        self.assertIn("<Interval>PT30M</Interval>", xml)
        self.assertIn(f"<Command>{self.root / 'kandu' / '.venv' / 'Scripts' / 'pythonw.exe'}</Command>", xml)
        self.assertIn("<Arguments>-X utf8 -m kandu.launch supervisor cycle</Arguments>", xml)
        self.assertIn(f"<ExecutionTimeLimit>PT{self.config.settings.supervisor.max_cycle_minutes + 5}M", xml)

    def test_installing_starts_nothing(self):
        for name in wintimer.TASKS:
            self.assertIn("\n    <Enabled>false</Enabled>", wintimer.render_task(self.config, name))

    def test_the_dashboard_starts_at_logon_and_comes_back(self):
        xml = wintimer.render_task(self.config, wintimer.DASHBOARD)
        self.assertIn("<LogonTrigger>", xml)
        self.assertIn("<RestartOnFailure>", xml)
        self.assertIn("<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", xml)

    def test_a_task_that_is_on_stays_on_when_written_again(self):
        with mock.patch.object(wintimer, "state", return_value="Ready"), \
                mock.patch.object(wintimer, "_schtasks", return_value=mock.Mock(returncode=0)) as schtasks, \
                mock.patch.object(wintimer, "render_task", return_value="<Task/>") as render:
            wintimer.install(self.config)
        self.assertEqual(schtasks.call_count, 3)
        self.assertTrue(all(c.args[2] is True for c in render.call_args_list))

    def test_a_cycle_does_not_start_while_one_holds_the_lock(self):
        lock = self.root / "supervisor.lock"
        with open(lock, "w") as held, mock.patch.object(wintimer.subprocess, "Popen") as popen:
            compat.lock(held, blocking=False)
            self.assertIsNone(wintimer.start_cycle(lock))
        popen.assert_not_called()
        with mock.patch.object(wintimer.subprocess, "Popen") as popen:
            self.assertIs(wintimer.start_cycle(lock), True)
        self.assertEqual(popen.call_args.args[0][-3:], ["kandu.launch", "supervisor", "cycle"])


class LaunchTest(unittest.TestCase):
    def test_env_file_is_read_without_a_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("# note\nA=1\nexport B='two words'\nC=\"x=y\"\n\nbad line\n", encoding="utf-8")
            self.assertEqual(launch.read_env(path), {"A": "1", "B": "two words", "C": "x=y"})

    def test_a_missing_env_file_is_empty(self):
        self.assertEqual(launch.read_env(Path("no-such-folder") / ".env"), {})

    def test_only_kandus_own_modules_start(self):
        self.assertEqual(launch.main(["os"]), 2)


if __name__ == "__main__":
    unittest.main()
