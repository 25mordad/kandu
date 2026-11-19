import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from kandu.config import NOTIFY_EVENTS, ROLES, ConfigError, load_config, main, set_parallel, set_project_model

EXAMPLES = Path(__file__).resolve().parent.parent / "config.example"


class ConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        shutil.copy(EXAMPLES / "kandu.example.yaml", self.local / "kandu.yaml")
        shutil.copy(EXAMPLES / "projects.example.yaml", self.local / "projects.yaml")

    def edit(self, filename, change):
        path = self.local / filename
        data = yaml.safe_load(path.read_text())
        change(data)
        path.write_text(yaml.safe_dump(data))

    def problems(self):
        with self.assertRaises(ConfigError) as caught:
            load_config(self.local)
        return "\n".join(caught.exception.problems)

    def test_with_no_permissions_every_role_may_do_everything(self):
        permissions = load_config(self.local).settings.permissions
        self.assertEqual([getattr(permissions, role) for role in ROLES], [None] * len(ROLES))

    def test_the_example_permissions_are_valid(self):
        path = self.local / "kandu.yaml"
        lines = path.read_text(encoding="utf-8").splitlines()
        start = lines.index(next(line for line in lines if line.startswith("# permissions:")))
        block = []
        for line in lines[start:]:
            if not line.startswith("#"):
                break
            block.append(line[2:])
        path.write_text("\n".join(lines[:start] + block + lines[start + len(block):]), encoding="utf-8")
        permissions = load_config(self.local).settings.permissions
        for role in ROLES:
            self.assertIn("Read", getattr(permissions, role).allow)
        self.assertIn("Bash(git push origin {branch})", permissions.worker.allow)

    def test_a_role_that_does_not_exist_is_refused(self):
        path = self.local / "kandu.yaml"
        path.write_text(path.read_text(encoding="utf-8") + "\npermissions:\n  painter:\n    allow: [Read]\n",
                        encoding="utf-8")
        self.assertIn("permissions.painter: unknown key", self.problems())

    def test_an_empty_setup_command_is_refused(self):
        self.edit("projects.yaml", lambda d: d["projects"][0].update(setup={"command": " ", "inputs": [""]}))
        problems = self.problems()
        self.assertIn("setup.command: must not be empty", problems)
        self.assertIn("setup.inputs: an entry must not be empty", problems)

    def test_the_example_setup_is_read(self):
        setup = load_config(self.local).projects[0].setup
        self.assertEqual((setup.command, setup.inputs), ("npm ci", ["package-lock.json"]))

    def test_examples_are_valid(self):
        config = load_config(self.local)
        self.assertEqual(config.settings.brain.machine, "server")
        self.assertEqual([p.name for p in config.projects], ["website", "internal-tool"])
        self.assertEqual(config.projects[0].board.number, 1)
        self.assertEqual(config.settings.didban.knowledge[0].name, "notes")

    def test_the_cleanup_after_a_merge_is_on_unless_turned_off(self):
        self.assertTrue(load_config(self.local).settings.supervisor.clean_merged)
        self.edit("kandu.yaml", lambda d: d["supervisor"].pop("clean_merged"))
        self.assertTrue(load_config(self.local).settings.supervisor.clean_merged)
        self.edit("kandu.yaml", lambda d: d["supervisor"].update(clean_merged=False))
        self.assertFalse(load_config(self.local).settings.supervisor.clean_merged)

    def test_the_default_look_is_classic_unless_the_config_names_kandu_clean(self):
        self.edit("kandu.yaml", lambda d: d["dashboard"].pop("look", None))
        self.assertEqual(load_config(self.local).settings.dashboard.look, "classic")
        self.edit("kandu.yaml", lambda d: d["dashboard"].update(look="clean"))
        self.assertEqual(load_config(self.local).settings.dashboard.look, "clean")
        self.edit("kandu.yaml", lambda d: d["dashboard"].update(look="classic"))
        self.assertEqual(load_config(self.local).settings.dashboard.look, "classic")

    def test_any_other_default_look_is_a_config_problem(self):
        for wrong in ("honeycomb", "Clean", ""):
            self.edit("kandu.yaml", lambda d: d["dashboard"].update(look=wrong))
            self.assertIn(f"dashboard.look: must be one of classic, classic-new, clean, got {wrong!r}", self.problems())

    def test_the_example_config_shows_the_default_look(self):
        text = (EXAMPLES / "kandu.example.yaml").read_text(encoding="utf-8")
        dashboard = text.split("\ndashboard:\n")[1].split("\n\n")[0]
        self.assertRegex(dashboard, r"\n  look: classic +# optional; ")

    def test_empty_registry_is_valid(self):
        self.edit("projects.yaml", lambda d: d.update(projects=[]))
        self.assertEqual(load_config(self.local).projects, [])

    def test_didban_section_is_optional(self):
        self.edit("kandu.yaml", lambda d: d.pop("didban"))
        self.assertEqual(load_config(self.local).settings.didban.knowledge, [])

    def test_missing_file(self):
        (self.local / "projects.yaml").unlink()
        self.assertIn("file not found", self.problems())

    def test_invalid_yaml(self):
        (self.local / "kandu.yaml").write_text("brain: [")
        self.assertIn("invalid YAML", self.problems())

    def test_run_location_is_never_defaulted(self):
        self.edit("projects.yaml", lambda d: d["projects"][0].pop("run"))
        self.assertIn("projects[0].run: missing", self.problems())

    def test_invalid_run(self):
        self.edit("projects.yaml", lambda d: d["projects"][0].update(run="anywhere"))
        self.assertIn("must be one of clone, in-place", self.problems())

    def test_clone_must_be_on_the_brain(self):
        self.edit("projects.yaml", lambda d: d["projects"][0].update(machine="laptop"))
        self.assertIn("run: clone must be on the brain's machine", self.problems())

    def test_tokens_need_a_file_a_name_and_a_purpose(self):
        self.edit("projects.yaml", lambda d: d["projects"][1].update(task_tokens={"A_KEY": "images"}))
        self.assertIn("task_tokens: needs secrets_from", self.problems())
        self.edit("projects.yaml", lambda d: d["projects"][0].update(task_tokens={"A KEY; rm": ""}))
        problems = self.problems()
        self.assertIn("not a variable name", problems)
        self.assertIn("say what it is for", problems)

    def test_unknown_machine(self):
        self.edit("projects.yaml", lambda d: d["projects"][1].update(machine="ghost"))
        self.assertIn("unknown machine 'ghost'", self.problems())

    def test_brain_must_be_a_known_machine(self):
        self.edit("kandu.yaml", lambda d: d["brain"].update(machine="ghost"))
        self.assertIn("brain.machine: unknown machine", self.problems())

    def test_brain_must_be_local(self):
        self.edit("kandu.yaml", lambda d: d["brain"].update(machine="laptop"))
        self.assertIn("must have reach: local", self.problems())

    def test_didban_source_needs_a_known_machine(self):
        self.edit("kandu.yaml", lambda d: d["didban"]["knowledge"][0].update(machine="ghost"))
        self.assertIn("didban.knowledge[0].machine", self.problems())

    def test_language_is_english_unless_the_config_says_otherwise(self):
        self.edit("kandu.yaml", lambda d: d.pop("language"))
        self.assertEqual(load_config(self.local).settings.language, "en")
        self.edit("kandu.yaml", lambda d: d.update(language="fa"))
        self.assertEqual(load_config(self.local).settings.language, "fa")

    def test_notifications_have_their_own_language(self):
        self.edit("kandu.yaml", lambda d: d["notify"].update(language="fa"))
        self.assertEqual(load_config(self.local).settings.notify.language, "fa")
        self.edit("kandu.yaml", lambda d: d["notify"].update(language="xx"))
        self.assertIn("notify.language", self.problems())

    def test_notifications_are_every_event_unless_the_config_lists_them(self):
        self.edit("kandu.yaml", lambda d: d["notify"].pop("events"))
        self.assertEqual(load_config(self.local).settings.notify.events, list(NOTIFY_EVENTS))
        self.edit("kandu.yaml", lambda d: d["notify"].update(events=["failure"]))
        self.assertEqual(load_config(self.local).settings.notify.events, ["failure"])
        self.edit("kandu.yaml", lambda d: d["notify"].update(events=["everything"]))
        self.assertIn("notify.events", self.problems())

    def test_a_model_is_chosen_per_role(self):
        self.assertEqual(load_config(self.local).settings.claude.models["splitter"], "claude-sonnet-5-5")
        self.edit("kandu.yaml", lambda d: d["claude"].pop("models"))
        self.assertEqual(load_config(self.local).settings.claude.models, {})

    def test_a_model_needs_a_known_role_and_a_real_name(self):
        self.edit("kandu.yaml", lambda d: d["claude"]["models"].update(wroker="sonnet"))
        self.assertIn("claude.models: must be one of", self.problems())
        self.edit("kandu.yaml", lambda d: d["claude"].update(models={"worker": "--dangerously-skip-permissions"}))
        self.assertIn("claude.models.worker: not a model name", self.problems())

    def test_a_projects_own_model_wins_then_the_global_one_then_claudes_default(self):
        self.edit("kandu.yaml", lambda d: d["claude"].update(models={"worker": "global-worker", "queen": "global-queen"}))
        self.edit("projects.yaml", lambda d: (d["projects"][0].update(models={"worker": "own-worker", "reviewer": "own-reviewer"}),
                                              d["projects"][1].pop("models", None)))
        config = load_config(self.local)
        website, other = config.projects
        self.assertEqual(config.model_for(website, "worker"), "own-worker")
        self.assertEqual(config.model_for(website, "reviewer"), "own-reviewer")
        self.assertEqual(config.model_for(other, "worker"), "global-worker")  # not named by the project: the global one
        self.assertIsNone(config.model_for(other, "reviewer"))                  # named nowhere: Claude's default
        self.assertIsNone(config.model_for(website, "splitter"))
        self.assertEqual(config.model_for(website, "queen"), "global-queen")    # the queen's is global only
        self.assertEqual(config.model_for(None, "queen"), "global-queen")

    def test_a_project_with_no_models_runs_as_before(self):
        self.edit("projects.yaml", lambda d: [p.pop("models", None) for p in d["projects"]])
        config = load_config(self.local)
        for project in config.projects:
            self.assertEqual(project.models, {})
            for role in ("worker", "reviewer", "splitter", "live_check", "bazras", "queen"):
                self.assertEqual(config.model_for(project, role), config.settings.claude.models.get(role), role)

    def test_a_projects_model_needs_a_role_it_may_name_and_a_real_name(self):
        for role in ("worker", "reviewer", "splitter", "live_check", "bazras"):
            self.edit("projects.yaml", lambda d: d["projects"][0].update(models={role: "claude-sonnet-5-5"}))
            self.assertEqual(load_config(self.local).projects[0].models, {role: "claude-sonnet-5-5"})
        self.edit("projects.yaml", lambda d: d["projects"][0].update(models={"queen": "claude-sonnet-5-5"}))
        self.assertIn("projects[0] (website).models: must be one of worker, reviewer, splitter, live_check, bazras, "
                      "got 'queen'", self.problems())
        self.edit("projects.yaml", lambda d: d["projects"][0].update(models={"wroker": "claude-sonnet-5-5"}))
        self.assertIn("projects[0] (website).models: must be one of", self.problems())
        self.assertIn("got 'wroker'", self.problems())
        self.edit("projects.yaml", lambda d: d["projects"][0].update(models={"worker": "--dangerously-skip-permissions"}))
        self.assertIn("projects[0] (website).models.worker: not a model name: '--dangerously-skip-permissions'",
                      self.problems())

    def test_the_example_shows_a_projects_own_model(self):
        self.assertEqual(load_config(self.local).projects[1].models, {"worker": "claude-opus-5-5"})

    def test_unknown_key_catches_typos(self):
        self.edit("kandu.yaml", lambda d: d["supervisor"].update(auto_merg=True))
        self.assertIn("supervisor.auto_merg: unknown key", self.problems())

    def test_split_approval_is_the_user_or_the_queen_the_user_by_default(self):
        self.assertIn("split_approval: user", (EXAMPLES / "projects.example.yaml").read_text(encoding="utf-8"))
        self.edit("projects.yaml", lambda d: d["projects"][0].pop("split_approval", None))
        self.assertEqual([p.split_approval for p in load_config(self.local).projects], ["user", "user"])
        self.edit("projects.yaml", lambda d: d["projects"][0].update(split_approval="queen"))
        self.assertEqual(load_config(self.local).projects[0].split_approval, "queen")
        self.edit("projects.yaml", lambda d: d["projects"][0].update(split_approval="anyone"))
        self.assertIn("projects[0] (website).split_approval: must be one of user, queen", self.problems())

    def test_the_queen_answers_one_way_merges_is_a_boolean_off_by_default(self):
        self.assertIn("queen_answers_one_way: false", (EXAMPLES / "projects.example.yaml").read_text(encoding="utf-8"))
        self.edit("projects.yaml", lambda d: d["projects"][0].pop("queen_answers_one_way", None))
        self.assertEqual([p.queen_answers_one_way for p in load_config(self.local).projects], [False, False])
        self.edit("projects.yaml", lambda d: d["projects"][0].update(queen_answers_one_way=True))
        self.assertTrue(load_config(self.local).projects[0].queen_answers_one_way)
        self.edit("projects.yaml", lambda d: d["projects"][0].update(queen_answers_one_way="yes"))
        self.assertIn("projects[0].queen_answers_one_way: must be bool", self.problems())

    def test_wrong_type(self):
        self.edit("kandu.yaml", lambda d: d["dashboard"].update(port="8790"))
        self.assertIn("dashboard.port: must be int", self.problems())

    def test_bool_is_not_an_int(self):
        self.edit("kandu.yaml", lambda d: d["supervisor"].update(max_parallel_workers=True))
        self.assertIn("supervisor.max_parallel_workers: must be int", self.problems())

    def test_percent_range(self):
        self.edit("kandu.yaml", lambda d: d["claude"]["limits"].update(weekly_percent=120))
        self.assertIn("weekly_percent: must be between 1 and 100", self.problems())

    def test_duplicate_project_name(self):
        self.edit("projects.yaml", lambda d: d["projects"][1].update(name="website"))
        self.assertIn("duplicate name", self.problems())

    def test_telegram_section_required_for_telegram(self):
        self.edit("kandu.yaml", lambda d: d["notify"].pop("telegram"))
        self.assertIn("notify.telegram: required", self.problems())

    def test_all_problems_are_reported_together(self):
        self.edit("kandu.yaml", lambda d: d["claude"].update(auth="magic"))
        self.edit("projects.yaml", lambda d: d["projects"][1].update(machine="ghost"))
        problems = self.problems()
        self.assertIn("claude.auth", problems)
        self.assertIn("unknown machine 'ghost'", problems)

    def test_project_auto_merge_overrides_supervisor(self):
        self.edit("kandu.yaml", lambda d: d["supervisor"].update(auto_merge=True))
        config = load_config(self.local)
        self.assertFalse(config.auto_merge_for(config.projects[0]))
        self.assertTrue(config.auto_merge_for(config.projects[1]))

    def test_machine_lookup(self):
        config = load_config(self.local)
        self.assertFalse(config.machine("laptop").always_on)

    def test_reach_is_local_or_one_alias(self):
        self.edit("kandu.yaml", lambda d: d["machines"][1].update(reach="ssh -oProxyCommand=x laptop"))
        self.assertIn("machines[1].reach", self.problems())

    def test_auto_revert_is_refused(self):
        self.edit("kandu.yaml", lambda d: d["supervisor"].update(auto_revert=True))
        self.assertIn("supervisor.auto_revert: must be false", self.problems())

    def test_clone_must_be_inside_the_work_folder(self):
        for path in ("~/code/website", "~/kandu-work-2/website", "~/kandu-work/../website", "~/kandu-work"):
            self.edit("projects.yaml", lambda d: d["projects"][0].update(path=path))
            self.assertIn("run: clone must be inside brain.work_dir", self.problems())

    def test_in_place_may_be_anywhere(self):
        self.edit("projects.yaml", lambda d: d["projects"][1].update(path="/srv/tool"))
        self.assertEqual(load_config(self.local).projects[1].path, "/srv/tool")

    def test_repo_owner_must_be_allowed(self):
        self.edit("projects.yaml", lambda d: d["projects"][0].update(repos=["stranger/website"]))
        self.assertIn("owner 'stranger' is not in github.owners", self.problems())

    def test_repo_must_be_owner_and_name(self):
        self.edit("projects.yaml", lambda d: d["projects"][0].update(repos=["website"]))
        self.assertIn("must be owner/name", self.problems())

    def test_board_owner_must_be_allowed(self):
        self.edit("projects.yaml", lambda d: d["projects"][0]["board"].update(owner="stranger"))
        self.assertIn("owner 'stranger' is not in github.owners", self.problems())

    def test_one_board_per_family(self):
        self.edit("projects.yaml", lambda d: d["projects"][1]["board"].update(number=2))
        self.assertIn("one board per family", self.problems())

    def test_two_families_may_have_their_own_boards(self):
        def change(d):
            d["projects"][1].update(family="other")
            d["projects"][1]["board"].update(number=2)
        self.edit("projects.yaml", change)
        self.assertEqual(load_config(self.local).projects[1].board.number, 2)

    def test_login_warning_defaults_to_a_week(self):
        self.edit("kandu.yaml", lambda d: d["claude"].pop("login_warn_days"))
        self.assertEqual(load_config(self.local).settings.claude.login_warn_days, 7)

    def test_supervisor_section_is_optional(self):
        self.edit("kandu.yaml", lambda d: d.pop("supervisor"))
        supervisor = load_config(self.local).settings.supervisor
        self.assertEqual((supervisor.wake_every_minutes, supervisor.max_cycle_minutes), (60, 50))
        self.assertFalse(supervisor.auto_merge)

    def test_one_supervisor_key_can_be_set_alone(self):
        self.edit("kandu.yaml", lambda d: d.update(supervisor={"wake_every_minutes": 30}))
        supervisor = load_config(self.local).settings.supervisor
        self.assertEqual((supervisor.wake_every_minutes, supervisor.max_cycle_minutes), (30, 50))


class SetParallelTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        shutil.copy(EXAMPLES / "kandu.example.yaml", self.local / "kandu.yaml")
        shutil.copy(EXAMPLES / "projects.example.yaml", self.local / "projects.yaml")

    def text(self, name):
        return (self.local / name).read_text(encoding="utf-8")

    def test_total_and_one_project_change_and_the_rest_stays(self):
        before = self.text("projects.yaml")
        set_parallel(7, {"internal-tool": 3}, self.local)
        config = load_config(self.local)
        self.assertEqual(config.settings.supervisor.max_parallel_workers, 7)
        self.assertEqual([p.max_parallel_workers for p in config.projects], [2, 3])
        self.assertIn("# one board per family", self.text("projects.yaml"))
        self.assertEqual(len(before.splitlines()), len(self.text("projects.yaml").splitlines()))

    def test_only_a_project_leaves_kandu_yaml_alone(self):
        before = self.text("kandu.yaml")
        set_parallel(None, {"website": 1}, self.local)
        self.assertEqual(self.text("kandu.yaml"), before)

    def test_a_missing_total_is_added_under_supervisor(self):
        path = self.local / "kandu.yaml"
        path.write_text(self.text("kandu.yaml").replace("  max_parallel_workers: 2\n", ""), encoding="utf-8")
        set_parallel(5, None, self.local)
        self.assertEqual(load_config(self.local).settings.supervisor.max_parallel_workers, 5)

    def test_a_wrong_value_or_project_writes_nothing(self):
        before = (self.text("kandu.yaml"), self.text("projects.yaml"))
        for total, projects in ((0, None), (3, {"website": 0}), (3, {"nobody": 2})):
            with self.assertRaises(ConfigError):
                set_parallel(total, projects, self.local)
        self.assertEqual((self.text("kandu.yaml"), self.text("projects.yaml")), before)


class SetProjectModelTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        shutil.copy(EXAMPLES / "kandu.example.yaml", self.local / "kandu.yaml")
        shutil.copy(EXAMPLES / "projects.example.yaml", self.local / "projects.yaml")

    def text(self):
        return (self.local / "projects.yaml").read_text(encoding="utf-8")

    def models(self):
        return {p.name: p.models for p in load_config(self.local).projects}

    def test_a_project_without_a_block_gets_one_and_the_other_project_keeps_its_own(self):
        set_project_model("website", "worker", "claude-sonnet-5-5", self.local)
        self.assertEqual(self.models(), {"website": {"worker": "claude-sonnet-5-5"},
                                         "internal-tool": {"worker": "claude-opus-5-5"}})
        set_project_model("website", "reviewer", "opus[1m]", self.local)
        self.assertEqual(self.models()["website"], {"worker": "claude-sonnet-5-5", "reviewer": "opus[1m]"})
        self.assertIn("# one board per family", self.text())

    def test_a_value_is_changed_in_place_and_the_blocks_comment_stays(self):
        set_project_model("internal-tool", "worker", "claude-sonnet-5-5", self.local)
        set_project_model("internal-tool", "splitter", "claude-haiku-5-5", self.local)
        self.assertEqual(self.models()["internal-tool"], {"worker": "claude-sonnet-5-5", "splitter": "claude-haiku-5-5"})
        self.assertIn("# optional; this project's own model", self.text())

    def test_clearing_takes_the_line_out_and_the_block_with_its_last_line(self):
        set_project_model("internal-tool", "reviewer", "claude-sonnet-5-5", self.local)
        set_project_model("internal-tool", "worker", None, self.local)
        self.assertEqual(self.models()["internal-tool"], {"reviewer": "claude-sonnet-5-5"})
        set_project_model("internal-tool", "reviewer", None, self.local)
        self.assertEqual(self.models()["internal-tool"], {})
        self.assertNotIn("    models:", self.text())
        before = self.text()
        set_project_model("website", "worker", None, self.local)  # nothing to clear: nothing changes
        self.assertEqual(self.text(), before)

    def test_a_wrong_role_model_or_project_writes_nothing_and_says_which(self):
        before = self.text()
        for name, role, model, says in (
                ("website", "queen", "claude-sonnet-5-5", "website.models: must be one of"),
                ("website", "wroker", "claude-sonnet-5-5", "website.models: must be one of"),
                ("website", "worker", "--dangerously-skip-permissions", "website.models.worker: not a model name"),
                ("nobody", "worker", "claude-sonnet-5-5", "no entry named 'nobody'")):
            with self.assertRaises(ConfigError) as caught:
                set_project_model(name, role, model, self.local)
            self.assertIn(says, caught.exception.problems[0])
        self.assertEqual(self.text(), before)


class MainTest(unittest.TestCase):
    def run_main(self, local):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.argv", ["kandu.config", "--local", str(local)]), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            return main(), out.getvalue(), err.getvalue()

    def test_valid_config_prints_a_result_on_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(EXAMPLES / "kandu.example.yaml", Path(tmp) / "kandu.yaml")
            shutil.copy(EXAMPLES / "projects.example.yaml", Path(tmp) / "projects.yaml")
            code, out, _ = self.run_main(tmp)
        self.assertEqual(code, 0)
        self.assertEqual(out, "config ok: 2 machine(s), 2 project(s)\n")

    def test_problems_go_to_stderr_with_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self.run_main(tmp)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("file not found", err)


if __name__ == "__main__":
    unittest.main()
