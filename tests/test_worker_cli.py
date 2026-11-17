"""Each project's settings choose whether its workers run on Claude Code or Codex (#81). The choice is kept in Kandu's
state, where it wins over the registry; the doctor's reason shows when the project's machine has no working Codex."""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kandu import doctor
from kandu.config import load_config
from kandu.dashboard import Api, dispatch
from kandu.runner import Unreachable
from kandu.state import StateStore

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "secret-token"


class FakeRunner:
    """Answers `codex login status` on each machine as the doctor's runner does: None, or why Codex cannot run."""
    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def codex_login(self, machine):
        self.asked.append(machine)
        answer = self.answers[machine]
        if isinstance(answer, Exception):
            raise answer
        return answer


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.local = self.root / "local"
        self.local.mkdir()
        shutil.copy(ROOT / "config.example" / "kandu.example.yaml", self.local / "kandu.yaml")
        shutil.copy(ROOT / "config.example" / "projects.example.yaml", self.local / "projects.yaml")
        self.store = StateStore(self.root / "state")


class DashboardChoiceTest(Base):
    def setUp(self):
        super().setUp()
        self.runner = FakeRunner({"server": None, "laptop": None})
        self.api = Api(load_config(self.local), self.store, None, self.root / "logs", TOKEN, timer_on=lambda: True,
                       local_dir=self.local, runner=self.runner)

    def state(self):
        return dispatch(self.api, "GET", "/api/v1/state")[1]

    def row(self, name="website"):
        return next(r for r in self.state()["project_settings"] if r["name"] == name)

    def choose(self, cli, name="website", token=TOKEN):
        return dispatch(self.api, "POST", f"/api/v1/projects/{name}/settings?worker_cli={cli}", token)

    def test_the_settings_show_the_registrys_choice_until_the_dashboard_makes_one(self):
        self.assertEqual(self.row()["worker_cli"], "claude")  # its entry names claude
        self.assertEqual(self.row("internal-tool")["worker_cli"], "claude")  # its entry names none: Claude Code
        self.api.config.projects[1].agent_cli = {"worker": "codex"}
        self.assertEqual(self.row("internal-tool")["worker_cli"], "codex")

    def test_a_change_is_kept_in_kandus_state_logged_and_shown(self):
        registry = (self.local / "projects.yaml").read_text(encoding="utf-8")
        self.assertEqual(self.choose("codex"), (200, {"worker_cli": "codex"}))
        self.assertEqual(self.row()["worker_cli"], "codex")
        self.assertEqual(self.store.project_settings(), {"website": {"worker_cli": "codex"}})
        self.assertEqual((self.local / "projects.yaml").read_text(encoding="utf-8"), registry)
        event = self.store.last_event("project_setting_changed")
        self.assertEqual((event["project"], event["setting"], event["old"], event["new"]),
                         ("website", "worker_cli", "claude", "codex"))

    def test_the_dashboards_choice_wins_over_the_registry(self):
        self.api.config.projects[0].agent_cli = {"worker": "codex"}
        self.assertEqual(self.choose("claude"), (200, {"worker_cli": "claude"}))
        self.assertEqual(self.row()["worker_cli"], "claude")
        self.assertNotIn("codex", self.state())  # no project runs on Codex now

    def test_a_project_chosen_for_codex_is_one_that_uses_codex(self):
        self.assertNotIn("codex", self.state())  # an install that never chose Codex sees nothing of it
        self.choose("codex")
        self.assertIn("codex", self.state())
        self.assertTrue(self.api.config.uses_codex())

    def test_only_claude_or_codex_and_only_with_the_token(self):
        for cli in ("gemini", "Codex", ""):
            self.assertEqual(self.choose(cli)[0], 400, cli)
        self.assertEqual(self.choose("codex", token="")[0], 403)
        self.assertEqual(self.store.project_settings(), {})
        self.assertIsNone(self.store.last_event("project_setting_changed"))

    def test_the_doctors_answer_for_the_projects_machine(self):
        self.assertEqual(dispatch(self.api, "GET", "/api/v1/projects/website/codex"), (200, {"machine": "server", "why": None}))
        for why in ("not installed", "not logged in; run: codex login", "codex login status did not answer"):
            self.runner.answers["laptop"] = why
            self.assertEqual(dispatch(self.api, "GET", "/api/v1/projects/internal-tool/codex"),
                             (200, {"machine": "laptop", "why": why}))
        self.runner.answers["laptop"] = Unreachable("laptop: ssh timed out")
        self.assertEqual(dispatch(self.api, "GET", "/api/v1/projects/internal-tool/codex"),
                         (200, {"machine": "laptop", "why": "the machine does not answer"}))
        self.assertEqual(dispatch(self.api, "GET", "/api/v1/projects/nothing/codex")[0], 404)
        self.assertEqual(self.store.project_settings(), {})  # asking changes nothing

    def test_a_renamed_project_keeps_its_choice(self):
        self.choose("codex")
        self.assertEqual(dispatch(self.api, "POST", "/api/v1/projects/website/settings?name=site", TOKEN)[0], 200)
        self.assertEqual(self.row("site")["worker_cli"], "codex")


class DoctorTest(Base):
    def test_a_project_the_dashboard_put_on_codex_has_its_machine_checked(self):
        checked = {}

        def run_checks(config, local_dir, runner, github):
            checked["codex"] = [p.name for p in config.projects if config.uses_codex() and p.worker_cli == "codex"]
            return []

        self.store.set_project_setting("internal-tool", "worker_cli", "codex")
        with mock.patch.object(doctor, "StateStore", lambda: self.store), \
                mock.patch.object(doctor, "run_checks", run_checks), \
                mock.patch.object(doctor, "Runner"), mock.patch.object(doctor, "GitHub"), \
                mock.patch("sys.argv", ["doctor", "--local", str(self.local)]):
            self.assertEqual(doctor.main(), 0)
        self.assertEqual(checked["codex"], ["internal-tool"])


if __name__ == "__main__":
    unittest.main()
