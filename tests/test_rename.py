"""Renaming a project (#42): after it Kandu knows the project only by its new name, and everything it kept about the
project carries over; a refused or failed rename changes nothing."""
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from kandu import rename, supervisor
from kandu.config import load_config
from kandu.dashboard import Api, dispatch
from kandu.state import StateStore

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "secret-token"
WORKER = {"claude-sonnet-5-5": {"input": 100, "output": 400, "cache_read": 9000, "cache_creation": 500}}


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
        self.api = Api(load_config(self.local), self.store, None, self.root / "logs", TOKEN, timer_on=lambda: True,
                       local_dir=self.local)
        self.earlier = datetime.now().astimezone().replace(microsecond=0) - timedelta(minutes=5)
        # What Kandu kept about `website` before the rename.
        self.keep("memory/website.md", "It is a static site.\n")
        self.keep("brief/website.md", "# website\n\n## What it is\nThe public site.\n")
        self.keep("reports/bazras/website/2026-11-15.md", "All fine.\n")
        self.store.pause_project("website")
        self.store.set_project_setting("website", "split_approval", "queen")
        self.store.set_project_setting("internal-tool", "split_approval", "user")
        self.store.write_request({"id": "r1", "project": "website", "text": "a dark theme"})
        self.store.write_request({"id": "r2", "project": "internal-tool", "text": "faster"})
        self.run_finished("website", "acme-org/website#12", self.earlier)

    def keep(self, where, text):
        path = self.local / where
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def run_finished(self, project, task, when=None):
        self.store.record("run_finished", now=when, task=task, project=project, run_id=f"r-{project}-{task[-2:]}",
                          role="worker", models=WORKER, duration_ms=1000, is_error=False)

    def snapshot(self):
        """Everything Kandu keeps, as text: a refused or failed rename leaves it the same."""
        files = {}
        for top in (self.local, self.store.root):
            for path in sorted(top.rglob("*")):
                if path.is_file() and path.name != "renames.json" and path.suffix != ".lock":
                    files[str(path.relative_to(self.root))] = path.read_bytes()
        renames = self.store.renames_file.read_bytes() if self.store.renames_file.exists() else None
        return files, renames

    def rename(self, new, old="website"):
        return rename.rename_project(old, new, self.store, self.local)

    def post(self, target):
        return dispatch(self.api, "POST", target, TOKEN)

    def names(self):
        return [p.name for p in load_config(self.local).projects]


class RefusedTest(Base):
    def assert_refused(self, new, message):
        before = self.snapshot()
        with self.assertRaises(rename.RenameError) as caught:
            self.rename(new)
        self.assertEqual(str(caught.exception), message)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.names(), ["website", "internal-tool"])

    def test_an_empty_name_is_refused_and_nothing_changes(self):
        self.assert_refused("", "give the project a name")
        self.assert_refused("   ", "give the project a name")

    def test_a_name_with_anything_but_lowercase_letters_digits_and_dashes_is_refused(self):
        for name in ("my site", "Website", "site_2", "site.com", "site/x", "-site", "سایت"):
            self.assert_refused(name, rename.NOT_A_NAME)

    def test_another_projects_name_is_refused(self):
        self.assert_refused("internal-tool", "another project already has this name")

    def test_a_rename_while_a_worker_runs_is_refused(self):
        self.store.write_current({"workers": [{"task": "acme-org/website#12", "project": "website", "run_id": "r-1"}]})
        self.assert_refused("site", "stop or wait for the running work first")

    def test_a_rename_while_a_free_flight_runs_is_refused(self):
        self.store.write_flight({"key": "acme-org-website-12", "kind": "task", "task": "acme-org/website#12",
                                 "project": "website", "started": "2026-11-16T09:00:00+01:00"})
        self.assert_refused("site", "stop or wait for the running work first")

    def test_a_rename_while_a_supervisor_cycle_runs_is_refused(self):
        # A cycle reads projects.yaml once when it wakes, and may start work on the project at any step after.
        refused = []

        class Cycle:
            store = self.store

            def cycle(cycle):
                try:
                    self.assert_refused("site", "stop or wait for the running work first")
                    refused.append(True)
                except AssertionError as e:
                    refused.append(e)
                return "done"

        with mock.patch("builtins.print"):
            supervisor._locked_cycle(Cycle())
        self.assertEqual(refused, [True])
        self.assertEqual(self.rename("site")["name"], "site")  # once the cycle is over

    def test_work_on_another_project_does_not_hold_the_rename(self):
        self.store.write_current({"workers": [{"task": "acme-org/tool#3", "project": "internal-tool", "run_id": "r-2"}]})
        self.assertEqual(self.rename("site")["name"], "site")

    def test_a_name_kandu_still_keeps_files_under_is_refused(self):
        self.keep("memory/site.md", "left from before\n")
        self.assert_refused("site", rename.KEPT)

    def test_an_entry_whose_name_is_not_its_first_line_is_refused(self):
        path = self.local / "projects.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "  - name: website\n    description: Public website of Acme\n",
            "  - repos: [acme-org/website]\n    name: website\n    description: Public website of Acme\n", 1).replace(
            "    family: acme                       # one board per family\n    repos: [acme-org/website]\n",
            "    family: acme                       # one board per family\n", 1), encoding="utf-8")
        self.assertEqual(load_config(self.local).projects[0].repos, ["acme-org/website"])
        self.assert_refused("site", "the entry of website in projects.yaml could not be found; rename it there by hand")

    def test_an_upstream_project_kandu_cannot_change_is_refused(self):
        path = self.local / "kandu.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("  project: kandu ", "  project: &own website ", 1),
                        encoding="utf-8")
        self.assertEqual(load_config(self.local).settings.upstream.project, "website")
        self.assert_refused("site", "upstream.project in kandu.yaml names website, but could not be changed; "
                                    "change it by hand")

    def test_a_config_that_would_not_pass_its_check_after_the_rename_is_refused(self):
        # The only `- name: website` line is in internal-tool's description, so the rename would change that line,
        # and upstream.project would then name `site`, a project that is not there.
        registry = self.local / "projects.yaml"
        registry.write_text(registry.read_text(encoding="utf-8").replace(
            "  - name: website\n    description: Public website of Acme\n",
            "  - description: Public website of Acme\n    name: website\n", 1).replace(
            "    description: Internal tool that needs a local database on the laptop\n",
            "    description: |\n      Internal tool. Its old entry was:\n      - name: website\n", 1),
            encoding="utf-8")
        settings = self.local / "kandu.yaml"
        upstream = "  enabled: {}               # off until you turn it on;"
        settings.write_text(settings.read_text(encoding="utf-8").replace(upstream.format("false"),
                                                                         upstream.format("true "), 1)
                            .replace("  project: kandu ", "  project: website ", 1), encoding="utf-8")
        self.assertEqual(load_config(self.local).settings.upstream.project, "website")
        before = self.snapshot()
        with self.assertRaises(rename.RenameError) as caught:
            self.rename("site")
        self.assertTrue(str(caught.exception).startswith("the config would not pass its check after the rename: "
                                                         "upstream.project: "), caught.exception)
        self.assertEqual(self.snapshot(), before)

    def test_an_unknown_project_is_refused(self):
        with self.assertRaises(rename.RenameError):
            self.rename("site", old="shop")


class RenamedTest(Base):
    def test_the_registry_entry_carries_the_new_name_and_the_rest_of_the_file_stays(self):
        before = (self.local / "projects.yaml").read_text(encoding="utf-8")
        self.assertEqual(self.rename("site"), {"name": "site", "old": "website"})
        after = (self.local / "projects.yaml").read_text(encoding="utf-8")
        self.assertEqual(after, before.replace("  - name: website\n", "  - name: site\n"))
        self.assertEqual(self.names(), ["site", "internal-tool"])

    def test_memory_brief_and_bazras_reports_are_found_under_the_new_name(self):
        self.rename("site")
        self.assertEqual((self.local / "memory/site.md").read_text(), "It is a static site.\n")
        self.assertEqual((self.local / "brief/site.md").read_text(), "# site\n\n## What it is\nThe public site.\n")
        self.assertEqual((self.local / "reports/bazras/site/2026-11-15.md").read_text(), "All fine.\n")
        for gone in ("memory/website.md", "brief/website.md", "reports/bazras/website"):
            self.assertFalse((self.local / gone).exists(), gone)

    def test_pause_settings_and_requests_are_found_under_the_new_name(self):
        self.rename("site")
        self.assertEqual(self.store.paused_projects(), {"site"})
        self.assertEqual(self.store.project_settings(), {"site": {"split_approval": "queen"},
                                                         "internal-tool": {"split_approval": "user"}})
        self.assertEqual({r["id"]: r["project"] for r in self.store.requests()}, {"r1": "site", "r2": "internal-tool"})

    def test_a_project_that_was_not_paused_is_not_paused_after(self):
        self.store.resume_project("website")
        self.rename("site")
        self.assertEqual(self.store.paused_projects(), set())

    def test_the_token_history_and_events_count_under_the_new_name_as_one_project(self):
        self.rename("site")
        self.run_finished("site", "acme-org/website#13")
        today = self.store.today()
        self.assertEqual(self.store.tokens_counted(today, project="site"), 2 * (100 + 400 + 500))
        self.assertEqual(self.store.tokens_counted(today, project="website"), 0)
        usage = self.store.usage_between(today, today)["tasks"]
        self.assertEqual({task: entry["project"] for task, entry in usage.items()},
                         {"acme-org/website#12": "site", "acme-org/website#13": "site"})
        self.assertNotIn("website", {e.get("project") for e in self.store.events(today)})

    def test_the_log_is_not_edited(self):
        log = self.store.events_dir / f"{self.store.today()}.jsonl"
        before = log.read_text(encoding="utf-8")
        self.rename("site")
        self.assertTrue(log.read_text(encoding="utf-8").startswith(before))

    def test_every_rename_is_logged_with_the_old_and_the_new_name(self):
        self.rename("site")
        self.rename("website", old="site")
        renamed = [(e["old"], e["new"]) for e in self.store.events(self.store.today()) if e["type"] == "project_renamed"]
        self.assertEqual(renamed, [("website", "site"), ("site", "website")])

    def test_renaming_back_restores_it_the_same_way(self):
        before = self.snapshot()[0]
        self.rename("site")
        self.run_finished("site", "acme-org/website#13")
        self.rename("website", old="site")
        self.assertEqual(self.names(), ["website", "internal-tool"])
        after = self.snapshot()[0]
        logs = [k for k in after if "/events/" in k]
        self.assertEqual({k: v for k, v in after.items() if k not in logs},
                         {k: v for k, v in before.items() if k not in logs})
        today = self.store.today()
        self.assertEqual(self.store.tokens_counted(today, project="website"), 2 * (100 + 400 + 500))
        self.assertEqual({e.get("project") for e in self.store.events(today) if e["type"] == "run_finished"}, {"website"})

    def test_a_new_project_given_the_old_name_later_does_not_take_its_history(self):
        self.rename("site")
        self.run_finished("website", "acme-org/other#1", datetime.now().astimezone() + timedelta(seconds=5))
        today = self.store.today()
        self.assertEqual({e["task"]: e["project"] for e in self.store.events(today) if e["type"] == "run_finished"},
                         {"acme-org/website#12": "site", "acme-org/other#1": "website"})

    def test_kandus_own_project_in_the_upstream_check_follows_the_rename(self):
        path = self.local / "kandu.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("  project: kandu ", "  project: website "),
                        encoding="utf-8")
        self.rename("site")
        self.assertEqual(load_config(self.local).settings.upstream.project, "site")

    def test_the_folder_repo_and_board_stay_as_they_are(self):
        was = next(p for p in load_config(self.local).projects if p.name == "website")
        self.rename("site")
        now = next(p for p in load_config(self.local).projects if p.name == "site")
        self.assertEqual((now.path, now.repos, now.board, now.family), (was.path, was.repos, was.board, was.family))


class FailedTest(Base):
    def assert_unchanged(self, before):
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.names(), ["website", "internal-tool"])
        self.assertEqual(self.store.tokens_counted(self.store.today(), project="website"), 100 + 400 + 500)

    def test_a_failure_partway_leaves_everything_as_it_was(self):
        # The memory moves first; the brief cannot move, as its folder may not be written.
        brief = self.local / "brief"
        brief.chmod(0o500)
        self.addCleanup(brief.chmod, 0o700)
        before = self.snapshot()
        with self.assertRaises(rename.RenameError) as caught:
            self.rename("site")
        self.assertIn("it keeps its name and everything stays as it was", str(caught.exception))
        self.assert_unchanged(before)

    def test_a_failure_at_the_last_step_leaves_everything_as_it_was(self):
        before = self.snapshot()
        with mock.patch.object(self.store, "record", side_effect=OSError("the disk is full")):
            with self.assertRaises(rename.RenameError):
                self.rename("site")
        self.assert_unchanged(before)


class ThroughTheDashboardTest(Base):
    def test_the_settings_call_renames_and_the_old_name_is_no_longer_known(self):
        status, answer = self.post("/api/v1/projects/website/settings?name=site")
        self.assertEqual((status, answer), (200, {"name": "site"}))
        rows = dispatch(self.api, "GET", "/api/v1/state")[1]
        self.assertEqual([r["name"] for r in rows["project_settings"]], ["site", "internal-tool"])
        self.assertIn("site", rows["paused_projects"])
        self.assertEqual(self.post("/api/v1/projects/website/pause")[0], 404)
        self.assertEqual(self.post("/api/v1/projects/site/settings?priority=3")[0], 200)

    def test_the_days_tab_shows_one_project_with_its_full_history(self):
        self.post("/api/v1/projects/website/settings?name=site")
        self.run_finished("site", "acme-org/website#13")
        usage = dispatch(self.api, "GET", "/api/v1/usage?range=today")[1]
        self.assertEqual([(p["project"], p["total"], p["counted"]) for p in usage["projects"]],
                         [("site", 2 * (100 + 400 + 500), 2 * (100 + 400 + 500))])

    def test_a_refusal_answers_its_plain_message_and_changes_nothing(self):
        self.store.write_current({"workers": [{"task": "acme-org/website#12", "project": "website", "run_id": "r-1"}]})
        before = self.snapshot()
        self.assertEqual(self.post("/api/v1/projects/website/settings?name=site"),
                         (409, {"error": "stop or wait for the running work first"}))
        self.assertEqual(self.post("/api/v1/projects/website/settings?name=my%20site"), (400, {"error": rename.NOT_A_NAME}))
        self.assertEqual(self.post("/api/v1/projects/website/settings?name=internal-tool"),
                         (400, {"error": "another project already has this name"}))
        self.assertEqual(self.post("/api/v1/projects/website/settings?name=site&priority=2"),
                         (400, {"error": "change the name in a call of its own"}))
        self.assertEqual(self.snapshot(), before)

    def test_a_config_that_no_longer_passes_its_check_answers_its_problem_and_changes_nothing(self):
        path = self.local / "projects.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("    machine: laptop ", "    machine: nowhere ", 1),
                        encoding="utf-8")
        before = self.snapshot()
        status, answer = self.post("/api/v1/projects/website/settings?name=site")
        self.assertEqual(status, 400)
        self.assertIn("unknown machine 'nowhere'", answer["error"])
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
