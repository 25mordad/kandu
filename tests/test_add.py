import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kandu import add
from kandu.config import load_config
from kandu.dashboard import Api, dispatch
from kandu.state import StateStore

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "secret-token"
LOOKED = "\n".join(["git", "remote git@github.com:acme-org/shop.git", "file scripts/checks.sh", "file package.json",
                    "file .env", "file AGENTS.md", "npm-dev", ""])


class FakeHost:
    def __init__(self, looked=LOOKED):
        self.looked = looked
        self.commands = []

    def absolute(self, path):
        return path.replace("~", "/home/me", 1)

    def check(self, command, cwd=None):
        self.commands.append(command)
        return self.looked if command[0] == "sh" else ""


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.local = self.root / "local"
        self.local.mkdir()
        shutil.copy(ROOT / "config.example" / "kandu.example.yaml", self.local / "kandu.yaml")
        shutil.copy(ROOT / "config.example" / "projects.example.yaml", self.local / "projects.yaml")
        self.host = FakeHost()
        runner = mock.patch("kandu.add.Runner")
        self.addCleanup(runner.stop)
        runner.start().return_value.hosts = {"server": self.host, "laptop": self.host}
        self.github = mock.Mock()
        self.github.create_board.return_value = 7
        self.github.repo_is_public.return_value = False
        self.github.create_issue.return_value = "https://github.com/acme-org/shop/issues/1"

    def config(self):
        return load_config(self.local)

    def project(self, name="shop"):
        return next(p for p in self.config().projects if p.name == name)


class MachineTest(Base):
    def test_adds_a_machine_and_keeps_the_comments(self):
        result = add.add_machine("desk", "desk", False, self.local, probe=lambda machine: True)
        self.assertEqual(result, {"machine": "desk", "reachable": True})
        machine = self.config().machine("desk")
        self.assertEqual((machine.reach, machine.always_on), ("ssh desk", False))
        self.assertIn("# SSH alias from ~/.ssh/config", (self.local / "kandu.yaml").read_text())

    def test_refuses_a_duplicate_or_a_bad_alias(self):
        before = (self.local / "kandu.yaml").read_text()
        for name, alias in (("laptop", "laptop"), ("desk", "two words"), ("desk", "-oProxyCommand=x"), ("bad name", "x")):
            with self.assertRaises(add.AddError):
                add.add_machine(name, alias, False, self.local, probe=lambda machine: True)
        self.assertEqual((self.local / "kandu.yaml").read_text(), before)


class DetectTest(Base):
    def test_proposes_an_entry_from_the_copy_and_never_where_it_runs(self):
        found = add.detect(self.config(), "laptop", "~/code/shop")
        p = found["proposal"]
        self.assertEqual((p["repo"], p["name"], p["family"], p["test"]),
                         ("acme-org/shop", "shop", "shop", "bash scripts/checks.sh"))
        self.assertEqual((p["run_local"], p["secrets_from"], p["repo_rules"]),
                         ("npm run dev", "~/code/shop/.env", "Follow AGENTS.md."))
        self.assertEqual((p["run"], p["auto_merge"], found["copy"], found["board"]), ("", False, "present", "a new board"))
        self.assertTrue(p["deploy_task"])
        self.assertEqual(self.host.commands[0][-1], "/home/me/code/shop")

    def test_reads_the_description_the_forbidden_folders_and_the_env_names(self):
        self.host.looked = LOOKED + "desc A shop\ndir data\nenvnames SHOP_KEY OTHER\n"
        found = add.detect(self.config(), "laptop", "~/code/shop")
        self.assertEqual((found["proposal"]["description"], found["proposal"]["forbidden"]), ("A shop", "data/"))
        self.assertTrue(any("SHOP_KEY" in w for w in found["warnings"]))

    def test_a_node_project_is_set_up_from_its_lock_file(self):
        self.assertEqual([add.detect(self.config(), "laptop", "~/code/shop")["proposal"][k] for k in ("setup", "setup_inputs")],
                         ["npm install", "package.json"])
        self.host.looked = LOOKED + "file package-lock.json\n"
        p = add.detect(self.config(), "laptop", "~/code/shop")["proposal"]
        self.assertEqual((p["setup"], p["setup_inputs"]), ("npm ci", "package-lock.json"))

    def test_a_python_project_without_working_tests_gets_a_checks_task(self):
        self.host.looked = "git\nremote https://github.com/acme-org/shop\nfile pyproject.toml\npy python3\n"
        found = add.detect(self.config(), "laptop", "~/code/shop")
        p = found["proposal"]
        self.assertTrue(p["test_task"])
        self.assertIn("python3 -m compileall", p["test"])
        self.assertIn(f"bash {add.CHECKS_SCRIPT}", p["test"])
        self.assertTrue(any("no tests yet" in w for w in found["warnings"]))

    def test_with_no_python_on_the_machine_the_test_is_left_to_the_user(self):
        self.host.looked = "git\nremote https://github.com/acme-org/shop\nfile pyproject.toml\n"
        found = add.detect(self.config(), "laptop", "~/code/shop")
        self.assertEqual(found["proposal"]["test"], "")
        self.assertTrue(any("no Python" in w for w in found["warnings"]))

    def test_a_path_under_home_is_written_from_home(self):
        self.assertEqual(add.detect(self.config(), "laptop", "/home/me/code/shop/")["proposal"]["path"], "~/code/shop")

    def test_refuses_a_repo_the_copy_does_not_belong_to(self):
        with self.assertRaisesRegex(add.AddError, "belongs to acme-org/shop"):
            add.detect(self.config(), "laptop", "~/code/shop", "acme-org/other")

    def test_a_pasted_link_is_taken_as_the_repo(self):
        for pasted in ("https://github.com/acme-org/shop", "https://github.com/acme-org/shop.git", " acme-org/shop/ ",
                       "git@github.com:acme-org/shop.git"):
            self.assertEqual(add.detect(self.config(), "laptop", "~/code/shop", pasted)["proposal"]["repo"],
                             "acme-org/shop")

    def test_refuses_a_path_already_registered(self):
        with self.assertRaisesRegex(add.AddError, "internal-tool"):
            add.detect(self.config(), "laptop", "~/code/internal-tool")

    def test_a_missing_folder_needs_the_repo(self):
        self.host.looked = "missing\n"
        with self.assertRaisesRegex(add.AddError, "owner/name"):
            add.detect(self.config(), "laptop", "~/code/new")
        self.assertEqual(add.detect(self.config(), "laptop", "~/code/new", "acme-org/new")["copy"], "will be cloned")

    def test_a_folder_that_is_not_git_is_refused(self):
        self.host.looked = "remote \n"
        with self.assertRaisesRegex(add.AddError, "not a git copy"):
            add.detect(self.config(), "laptop", "~/code/shop")

    def test_detect_changes_nothing(self):
        before = [(self.local / n).read_text() for n in ("kandu.yaml", "projects.yaml")]
        add.detect(self.config(), "laptop", "~/code/shop")
        self.assertEqual([(self.local / n).read_text() for n in ("kandu.yaml", "projects.yaml")], before)
        self.assertEqual([c[0] for c in self.host.commands], ["sh"])
        self.github.assert_not_called()


class AddProjectTest(Base):
    def fields(self, **changes):
        return add.detect(self.config(), "laptop", "~/code/shop")["proposal"] | {"run": "in-place"} | changes

    def test_writes_the_project_and_creates_its_board(self):
        checks = mock.Mock(return_value=[("ok", "fine")])
        result = add.add_project(self.fields(description="The shop", deploy_task=False), self.github, self.local,
                                 checks)
        self.github.create_board.assert_called_once()
        self.assertEqual(result["board"], {"owner": "acme-org", "number": 7, "created": True})
        self.assertEqual(result["checks"], [{"level": "ok", "text": "fine"}])
        project = self.project()
        self.assertEqual((project.machine, project.path, project.repos, project.run, project.auto_merge),
                         ("laptop", "~/code/shop", ["acme-org/shop"], "in-place", False))
        self.assertEqual((project.board.number, project.max_parallel_workers), (7, 1))
        self.assertIn("# Kandu project registry", (self.local / "projects.yaml").read_text())

    def test_writes_the_setup_and_leaves_it_out_when_there_is_none(self):
        add.add_project(self.fields(setup="npm ci", setup_inputs="package-lock.json, .nvmrc", deploy_task=False),
                        self.github, self.local)
        setup = self.project().setup
        self.assertEqual((setup.command, setup.inputs), ("npm ci", ["package-lock.json", ".nvmrc"]))

    def test_no_setup_is_written_when_it_is_empty(self):
        add.add_project(self.fields(setup="", deploy_task=False), self.github, self.local)
        self.assertIsNone(self.project().setup)

    def test_where_it_runs_is_always_the_users_choice(self):
        before = (self.local / "projects.yaml").read_text()
        for run in ("", "anywhere"):
            with self.assertRaisesRegex(add.AddError, "run"):
                add.add_project(self.fields(run=run), self.github, self.local)
        self.assertEqual((self.local / "projects.yaml").read_text(), before)
        with self.assertRaisesRegex(add.AddError, "brain"):  # Kandu clones only on its own machine
            add.add_project(self.fields(run="clone"), self.github, self.local)
        self.assertEqual((self.local / "projects.yaml").read_text(), before)
        fields = add.detect(self.config(), "server", "~/kandu-work/shop")["proposal"] | {"run": "clone", "deploy_task": False}
        add.add_project(fields, self.github, self.local)
        self.assertEqual(self.project().run, "clone")

    def test_reuses_the_family_board(self):
        result = add.add_project(self.fields(family="acme", deploy_task=False), self.github, self.local)
        self.github.create_board.assert_not_called()
        self.assertEqual(result["board"], {"owner": "acme-org", "number": 1, "created": False})

    def test_clones_a_missing_copy(self):
        self.host.looked = "missing\n"
        fields = add.detect(self.config(), "laptop", "~/code/new", "acme-org/new")["proposal"] | {
            "test": "make test", "run": "in-place", "deploy_task": False}
        self.assertTrue(add.add_project(fields, self.github, self.local)["cloned"])
        self.assertIn(["git", "clone", "https://github.com/acme-org/new.git", "/home/me/code/new"], self.host.commands)

    def test_a_bad_entry_creates_and_writes_nothing(self):
        before = (self.local / "projects.yaml").read_text()
        for fields in (self.fields(test=""), self.fields(name="Bad Name"), self.fields(live_url="https://x"),
                       self.fields(name="website"), self.fields(priority="first")):
            with self.assertRaises(add.AddError):
                add.add_project(fields, self.github, self.local)
        self.host.looked = LOOKED.replace("acme-org", "stranger")
        with self.assertRaisesRegex(add.AddError, "github.owners"):
            add.add_project(self.fields(), self.github, self.local)
        self.github.create_board.assert_not_called()
        self.github.create_issue.assert_not_called()
        self.assertEqual((self.local / "projects.yaml").read_text(), before)

    def test_no_deploy_can_put_a_task_on_the_board_that_writes_one(self):
        result = add.add_project(self.fields(), self.github, self.local)
        self.assertEqual(result["deploy_task"], "https://github.com/acme-org/shop/issues/1")
        repo, title, body = self.github.create_issue.call_args.args[:3]
        self.assertEqual((repo, title), ("acme-org/shop", add.DEPLOY_TASK_TITLE))
        self.assertIn(add.DEPLOY_SCRIPT, body)
        self.assertEqual(self.project().deploy.command, add.DEPLOY_PENDING)

    def test_without_the_task_there_is_no_deploy(self):
        result = add.add_project(self.fields(deploy_task="false"), self.github, self.local)
        self.github.create_issue.assert_not_called()
        self.assertIsNone(result["deploy_task"])
        self.assertIsNone(self.project().deploy)

    def test_a_task_that_fails_leaves_the_project_added(self):
        self.github.create_issue.side_effect = add.GitHubError("down")
        result = add.add_project(self.fields(), self.github, self.local)
        self.assertTrue(result["deploy_task"].startswith("not created"))
        self.assertIn("shop", [p.name for p in self.config().projects])

    def test_forbidden_and_the_checks_task_go_in(self):
        self.github.create_issue.return_value = "https://github.com/acme-org/shop/issues/2"
        result = add.add_project(self.fields(forbidden="data/, secrets/", test_task="true", deploy_task="false"),
                                 self.github, self.local)
        self.assertEqual(result["test_task"], "https://github.com/acme-org/shop/issues/2")
        self.assertEqual(self.github.create_issue.call_args.args[1], add.TEST_TASK_TITLE)
        self.assertEqual(self.project().forbidden, ["data/", "secrets/"])

    def test_a_deploy_goes_in_as_direct(self):
        add.add_project(self.fields(deploy="make deploy", live_url="https://shop.example.com"), self.github, self.local)
        deploy = self.project().deploy
        self.assertEqual((deploy.mode, deploy.command, deploy.live_url),
                         ("direct", "make deploy", "https://shop.example.com"))
        self.github.create_issue.assert_not_called()


class ApiTest(Base):
    def setUp(self):
        super().setUp()
        self.api = Api(self.config(), StateStore(self.root / "state"), self.github, self.root / "logs", TOKEN,
                       timer_on=lambda: True, local_dir=self.local)

    def post(self, target, token=TOKEN):
        return dispatch(self.api, "POST", target, token)

    def test_needs_the_token(self):
        self.assertEqual(self.post("/api/v1/projects/detect?machine=laptop&path=~/code/shop", None)[0], 403)
        self.assertEqual(self.post("/api/v1/projects?machine=laptop&path=~/code/shop", "wrong")[0], 403)
        self.assertEqual(self.post("/api/v1/machines?name=desk&ssh=desk", None)[0], 403)

    def test_detect_then_add(self):
        status, found = self.post("/api/v1/projects/detect?machine=laptop&path=~/code/shop")
        self.assertEqual((status, found["proposal"]["name"]), (200, "shop"))
        with mock.patch("kandu.add.doctor_checks", return_value=[("ok", "fine")]):
            status, body = self.post("/api/v1/projects?machine=laptop&path=~/code/shop&repo=acme-org/shop"
                                     "&name=shop&test=npm+test&run=in-place&auto_merge=false&deploy_task=false")
        self.assertEqual((status, body["project"], body["checks"]), (200, "shop", [{"level": "ok", "text": "fine"}]))
        self.assertFalse(next(p for p in self.api.config.projects if p.name == "shop").auto_merge)
        self.assertEqual(self.api.store.last_event("project_added")["project"], "shop")
        self.assertIn("shop", dispatch(self.api, "GET", "/api/v1/state")[1]["project_order"])

    def test_a_problem_is_a_400(self):
        status, body = self.post("/api/v1/projects/detect?machine=nowhere&path=~/x")
        self.assertEqual((status, body["error"]), (400, "unknown machine 'nowhere'"))
        status, body = self.post("/api/v1/projects?machine=laptop&path=~/code/shop&repo=acme-org/shop&name=shop"
                                 "&test=npm+test")
        self.assertEqual(status, 400)
        self.assertIn("run is required", body["error"])

    def test_add_machine_shows_in_state(self):
        with mock.patch("kandu.add._answers", return_value=False):
            status, body = self.post("/api/v1/machines?name=desk&ssh=desk&always_on=true")
        self.assertEqual((status, body["reachable"]), (200, False))
        machines = dispatch(self.api, "GET", "/api/v1/state")[1]["machines"]
        self.assertIn({"name": "desk", "always_on": True, "down_since": None, "run_tmp": False, "run_tmp_dir": None},
                      machines)
        self.assertEqual(self.api.store.last_event("machine_added")["machine"], "desk")


class PageTest(unittest.TestCase):
    def test_the_form_asks_where_kandu_works_with_no_choice_made(self):
        page = (ROOT / "kandu" / "dashboard.html").read_text(encoding="utf-8")
        select = page.split('<select data-f="run">', 1)[1].split("</select>", 1)[0]
        self.assertTrue(select.startswith('<option value="" data-t="run_choose">'))
        self.assertIn('id="addopen"', page.split('data-panel="settings"', 1)[1].split("</section>", 1)[0])


if __name__ == "__main__":
    unittest.main()
