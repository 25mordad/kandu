import shutil
import tempfile
import unittest
from pathlib import Path

from kandu import notify
from kandu.config import NOTIFY_EVENTS, load_config
from kandu.dashboard import Api, dispatch
from kandu.state import StateStore

ROOT = Path(__file__).resolve().parent.parent
SINCE = "2026-11-10T08:30:00+01:00"


class NotifyTest(unittest.TestCase):
    def test_a_long_outage_is_told_once_and_its_end_only_if_it_was_told(self):
        down = notify.message_for({"type": "machine_unreachable", "machine": "laptop", "since": SINCE, "waiting": 2})
        self.assertEqual((down.kind, down.key), ("machine_unreachable", f"machine_unreachable:laptop:{SINCE}"))
        self.assertIn("Machine laptop has not answered since 2026-11-10 08:30. Tasks waiting for it: 2.",
                      notify._render(down, "en"))
        back = notify.message_for({"type": "machine_up", "machine": "laptop", "since": SINCE, "told": True})
        self.assertIn("reachable again", notify._render(back, "en"))
        self.assertTrue(notify._render(back, "en").startswith(notify.MARKS[notify.FOR_YOUR_INFO]))
        self.assertIsNone(notify.message_for({"type": "machine_up", "machine": "laptop", "since": SINCE, "told": False}))
        self.assertIn("machine_unreachable", NOTIFY_EVENTS)
        self.assertIn("ماشین laptop", notify._render(down, "fa"))


class StateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        local = root / "local"
        local.mkdir()
        shutil.copy(ROOT / "config.example" / "kandu.example.yaml", local / "kandu.yaml")
        shutil.copy(ROOT / "config.example" / "projects.example.yaml", local / "projects.yaml")
        self.store = StateStore(root / "state")
        self.api = Api(load_config(local), self.store, None, root / "logs", "t", timer_on=lambda: True, local_dir=local)

    def test_each_machine_says_since_when_it_is_down(self):
        self.store.write_current({"unreachable_machines": [{"machine": "laptop", "since": SINCE, "tasks": []}]})
        state = dispatch(self.api, "GET", "/api/v1/state")[1]
        self.assertEqual({m["name"]: m["down_since"] for m in state["machines"]}, {"server": None, "laptop": SINCE})
        self.assertEqual(state["unreachable_machines"][0]["machine"], "laptop")

    def test_the_example_config_has_the_option(self):
        self.assertEqual(self.api.config.settings.supervisor.unreachable_alert_hours, 12)
        self.assertIn("unreachable_alert_hours", (ROOT / "config.example" / "kandu.example.yaml").read_text())
        self.assertIn("unreachable_alert_hours", (ROOT / ".claude" / "commands" / "kandu-setup.md").read_text())


class PageTest(unittest.TestCase):
    def test_the_page_lists_each_machine_that_does_not_answer_in_both_languages(self):
        page = (ROOT / "kandu" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('<div id="machines" class="list" hidden></div>', page)
        self.assertIn('$("machines").innerHTML = unreachableMachines(s);', page)
        for key in ("machine_off", "machine_waits", "machine_waits_none"):
            self.assertEqual(page.count(f'"{key}": '), 2, key)


if __name__ == "__main__":
    unittest.main()
