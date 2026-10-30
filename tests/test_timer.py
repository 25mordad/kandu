import shutil
import tempfile
import unittest
from pathlib import Path

from kandu.config import load_config
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


if __name__ == "__main__":
    unittest.main()
