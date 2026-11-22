"""A read-only copy of a task's reference for the run (#129): what the section names, what may be copied, the copy
itself with real git and tar, and what a run sees and leaves behind. No network: GitHub and the agents are fakes."""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kandu import agents, reference, supervisor
from kandu.config import Config, Machine, Project, Rules, load_config, set_reference_copy
from kandu.dashboard import Api
from kandu.github import GitHubError
from kandu.runner import Runner, RunnerError, Unreachable
from tests.test_supervisor import PR_OPENED, REPO, SHA1, Base, FakeRunner, report

ROOT = Path(__file__).resolve().parent.parent
OTHER = "acme-org/internal-tool"  # the other project of the example registry
BODY = f"""Do the panel.

## Reference
Repo: `{OTHER}`
Paths: `panel/index.html`, `panel/static/`
Copy: the look

## Acceptance criteria
- [ ] it matches the reference
"""


def git(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


class ParseTest(unittest.TestCase):
    def test_a_reference_names_a_repo_and_its_paths(self):
        found = reference.references_in(BODY)
        self.assertEqual([(r.repo, r.paths) for r in found], [(OTHER, ["panel/index.html", "panel/static/"])])

    def test_paths_may_be_a_list_and_a_section_may_hold_several_repos(self):
        body = "## Reference\nRepo: a/one\n- `x.txt`\n- `y/z.txt` the steps\nRepo: `b/two`\nPath: `w`\n## Out of scope\n`q`"
        self.assertEqual([(r.repo, r.paths) for r in reference.references_in(body)],
                         [("a/one", ["x.txt", "y/z.txt"]), ("b/two", ["w"])])

    def test_no_section_or_none_is_no_reference(self):
        for body in ("", None, "## Reference\nnone\n", "## Goal\nRepo: a/b `c`\n"):
            self.assertEqual(reference.references_in(body), [])

    def test_only_a_plain_path_inside_the_repo_is_safe(self):
        for path in ("a", "a/b.txt", "dir/", ".github/x.yml"):
            self.assertTrue(reference.safe_path(path), path)
        for path in ("/etc/passwd", "../x", "a/../../x", "~/x", ".git/config", "a/.git/x", "-rf", ".", "a\\b"):
            self.assertFalse(reference.safe_path(path), path)


class PlanTest(Base):
    def plan(self, body, task_repo=REPO, public=()):
        return reference.plan(task_repo, body, self.config.projects, lambda repo: repo in public)

    def test_a_registered_repo_is_planned_with_its_project(self):
        wanted, notes = self.plan(BODY)
        self.assertEqual(notes, [])
        self.assertEqual([(p.name, r.repo, r.paths, n) for p, r, n in wanted],
                         [("internal-tool", OTHER, ["panel/index.html", "panel/static/"], 1)])

    def test_the_tasks_own_repo_needs_no_copy(self):
        self.assertEqual(self.plan(BODY.replace(OTHER, REPO.upper())), ([], []))

    def test_a_repo_that_is_not_in_the_registry_is_refused_with_a_note_that_names_nothing(self):
        wanted, notes = self.plan(BODY.replace(OTHER, "someone/else"))
        self.assertEqual(wanted, [])
        self.assertEqual(notes, [f"Reference 1: {reference.NOT_IN_REGISTRY}."])
        self.assertNotIn("someone", notes[0])

    def test_a_private_repo_never_reaches_a_run_on_a_public_repo(self):
        wanted, notes = self.plan(BODY, public={REPO})
        self.assertEqual((wanted, notes), ([], [f"Reference 1: {reference.PRIVATE_INTO_PUBLIC}."]))
        self.assertEqual(len(self.plan(BODY, public={REPO, OTHER})[0]), 1)
        self.assertEqual(len(self.plan(BODY, public=())[0]), 1)  # private into private, and public into private

    def test_a_visibility_that_cannot_be_read_refuses(self):
        def broken(repo):
            raise GitHubError("down")
        wanted, notes = reference.plan(REPO, BODY, self.config.projects, broken)
        self.assertEqual((wanted, notes), ([], [f"Reference 1: {reference.VISIBILITY_UNKNOWN}."]))

    def test_unsafe_paths_are_left_out_and_a_reference_without_paths_says_so(self):
        wanted, notes = self.plan(BODY.replace("`panel/static/`", "`../secret`"))
        self.assertEqual(wanted[0][1].paths, ["panel/index.html"])
        self.assertEqual(notes, [f"Reference 1: {reference.UNSAFE_PATH}."])
        wanted, notes = self.plan(f"## Reference\nRepo: {OTHER}\nCopy: the look\n")
        self.assertEqual((wanted, notes), ([], [f"Reference 1: {reference.NO_PATHS}."]))


class CopyTest(unittest.TestCase):
    """The copy itself, on real folders with real git and tar."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "TMPDIR": str(self.root / "tmp")})
        env.start()
        self.addCleanup(env.stop)
        (self.root / "tmp").mkdir()
        self.source_copy = self.home / "src"
        panel = self.source_copy / "panel"
        (panel / "static").mkdir(parents=True)
        (panel / "index.html").write_text("<h1>panel</h1>")
        (panel / "static" / "a.css").write_text("a{}")
        (self.source_copy / "other.txt").write_text("not named")
        os.symlink("/etc/passwd", panel / "link")
        git("init", "-b", "main", ".", cwd=self.source_copy)
        git("add", ".", cwd=self.source_copy)
        git("commit", "-m", "c", cwd=self.source_copy)
        (panel / "untracked.env").write_text("SECRET")  # never committed: never copied
        self.machines = [Machine("box", "local", True), Machine("far", "ssh far", True)]
        self.runner = Runner(Config(settings=SimpleNamespace(machines=self.machines), projects=[]), self.root / "logs")
        self.source = self.project("src", "~/src")
        self.target = self.project("work", "~/work")

    def project(self, name, path, machine="box"):
        return Project(name=name, family="f", repos=[f"o/{name}"], run="local", machine=machine, path=path, test="true",
                       priority=1, max_parallel_workers=1, paused=False, daily_token_limit=1)

    def tree(self, folder):
        return sorted(str(p.relative_to(folder)) for p in Path(folder).rglob("*") if p.is_file() or p.is_symlink())

    def test_only_the_named_tracked_paths_land_in_a_folder_of_the_runs_own(self):
        folder = self.runner.make_reference_folder(self.target)
        self.assertTrue(Path(folder).is_dir())
        self.assertEqual(Path(folder).parent, self.root / "tmp")
        done, missing = self.runner.copy_reference(self.source, "o/src", ["panel/index.html", "panel/static/"],
                                                   self.target, folder)
        self.assertEqual((done, missing), (["panel/index.html", "panel/static/"], []))
        self.assertEqual(self.tree(folder), ["o__src/panel/index.html", "o__src/panel/static/a.css"])
        self.assertEqual((Path(folder) / "o__src/panel/index.html").read_text(), "<h1>panel</h1>")

    def test_the_copy_is_read_only_and_a_symlink_or_an_untracked_file_is_not_in_it(self):
        folder = self.runner.make_reference_folder(self.target)
        self.runner.copy_reference(self.source, "o/src", ["panel"], self.target, folder)
        self.assertEqual(self.tree(folder), ["o__src/panel/index.html", "o__src/panel/static/a.css"])
        for path in Path(folder).rglob("*"):
            self.assertFalse(path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH), path)
        self.runner.remove_reference_folder(self.target, folder)

    def test_the_folder_goes_with_its_read_only_files(self):
        folder = self.runner.make_reference_folder(self.target)
        self.runner.copy_reference(self.source, "o/src", ["panel"], self.target, folder)
        self.runner.remove_reference_folder(self.target, folder)
        self.assertFalse(Path(folder).exists())
        self.assertEqual(os.listdir(self.root / "tmp"), [])
        self.runner.remove_reference_folder(self.target, folder)  # already gone: fine

    def test_each_run_has_its_own_folder_in_the_machines_run_tmp_dir_when_that_is_on(self):
        self.machines[0] = Machine("box", "local", True, run_tmp=True, run_tmp_dir="~/run-tmp")
        runner = Runner(Config(settings=SimpleNamespace(machines=self.machines), projects=[]), self.root / "logs")
        one, two = runner.make_reference_folder(self.target), runner.make_reference_folder(self.target)
        self.assertNotEqual(one, two)
        self.assertEqual({Path(one).parent, Path(two).parent}, {self.home / "run-tmp"})

    def test_a_path_that_does_not_exist_is_reported_and_the_rest_is_copied(self):
        folder = self.runner.make_reference_folder(self.target)
        done, missing = self.runner.copy_reference(self.source, "o/src", ["panel/index.html", "nope", "panel/untracked.env"],
                                                   self.target, folder)
        self.assertEqual((done, missing), (["panel/index.html"], ["nope", "panel/untracked.env"]))
        self.assertEqual(self.tree(folder), ["o__src/panel/index.html"])

    def test_nothing_found_copies_nothing(self):
        folder = self.runner.make_reference_folder(self.target)
        self.assertEqual(self.runner.copy_reference(self.source, "o/src", ["nope"], self.target, folder), ([], ["nope"]))
        self.assertEqual(self.tree(folder), [])

    def test_a_source_machine_that_does_not_answer_is_unreachable(self):
        ssh_bin = self.root / "ssh-bin"
        ssh_bin.mkdir()
        (ssh_bin / "ssh").write_text("#!/bin/sh\nexit 255\n")
        (ssh_bin / "ssh").chmod(0o755)
        folder = self.runner.make_reference_folder(self.target)
        far = self.project("src", "~/src", machine="far")
        with mock.patch.dict(os.environ, {"PATH": f"{ssh_bin}:{os.environ['PATH']}"}):
            with self.assertRaises(Unreachable):
                self.runner.copy_reference(far, "o/src", ["panel"], self.target, folder)

    def test_a_source_that_is_not_a_git_copy_cannot_be_read(self):
        folder = self.runner.make_reference_folder(self.target)
        (self.home / "plain").mkdir()
        with self.assertRaises(RunnerError):
            self.runner.copy_reference(self.project("src", "~/plain"), "o/src", ["panel"], self.target, folder)

    def test_a_reference_over_the_limit_is_not_copied(self):
        folder = self.runner.make_reference_folder(self.target)
        with mock.patch("kandu.runner.MAX_REFERENCE_BYTES", 100):
            with self.assertRaises(RunnerError):
                self.runner.copy_reference(self.source, "o/src", ["panel"], self.target, folder)
        self.assertEqual(self.tree(folder), [])


class ReferenceRunner(FakeRunner):
    """The fake runner with a reference folder it can list: what it made, copied and removed."""

    def __init__(self, github, base):
        super().__init__(github)
        self.base = base
        self.made, self.removed, self.copies = [], [], []
        self.copy_error = None
        self.missing = []
        self.alive_during_run = []

    def make_reference_folder(self, project):
        folder = str(self.base / f"kandu-ref-{len(self.made)}")
        os.makedirs(folder)
        self.made.append(folder)
        return folder

    def copy_reference(self, source, repo, paths, project, folder):
        self.copies.append((source.name, repo, list(paths), project.name))
        if self.copy_error:
            raise self.copy_error
        found = [p for p in paths if p not in self.missing]
        for path in found:
            target = Path(folder) / repo.replace("/", "__") / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("copy")
        return found, [p for p in paths if p in self.missing]

    def remove_reference_folder(self, project, folder):
        self.removed.append(folder)
        shutil.rmtree(folder, ignore_errors=True)

    def run_agent(self, *args, **kwargs):
        self.alive_during_run.append([Path(f).exists() for f in self.made])
        return super().run_agent(*args, **kwargs)


class RunTest(Base):
    def setUp(self):
        super().setUp()
        self.runner = ReferenceRunner(self.github, self.root / "refs")
        (self.root / "refs").mkdir()
        self.config.settings.supervisor.reference_copy = True
        self.config.settings.permissions.worker = Rules(allow=["Read"])
        self.config.settings.permissions.reviewer = Rules(allow=["Read"])
        self.github.repo_is_public = lambda repo: False
        self.github.add(1, body=BODY)
        self.sup = self.new_supervisor()

    def notes(self):
        return [c for c in self.github.issue_comments if "reference" in c]

    def test_it_ships_off_and_the_run_is_as_before(self):
        self.assertFalse(load_config(self.local).settings.supervisor.reference_copy)
        self.config.settings.supervisor.reference_copy = False
        self.runner.steps = ['{"status": "blocked", "reason": "r"}']
        self.cycle()
        self.assertEqual((self.runner.made, self.runner.copies, self.notes()), ([], [], []))
        self.assertNotIn("Reference copy", self.runner.runs[0].prompt)

    def test_the_worker_is_told_where_the_read_only_copy_is_and_may_read_only_that_folder(self):
        self.runner.steps = ['{"status": "blocked", "reason": "r"}']
        self.cycle()
        folder = self.runner.made[0]
        self.assertEqual(self.runner.copies, [("internal-tool", OTHER, ["panel/index.html", "panel/static/"], "website")])
        run = self.runner.runs[0]
        self.assertIn("## Reference copy", run.prompt)
        self.assertIn(f"`{folder}/{OTHER.replace('/', '__')}`", run.prompt)
        self.assertIn("read-only", run.prompt)
        self.assertIn("never commit or push", run.prompt)
        self.assertIn(f"Read(/{folder}/**)", run.allowed)
        self.assertEqual(self.runner.alive_during_run, [[True]])

    def test_the_folder_is_removed_when_the_run_ends(self):
        self.runner.steps = ['{"status": "blocked", "reason": "r"}']
        self.cycle()
        self.assertEqual(self.runner.removed, self.runner.made)
        self.assertFalse(Path(self.runner.made[0]).exists())
        self.assertEqual(self.events("reference_copied")[0]["repos"], [OTHER])

    def test_it_is_removed_when_the_run_fails_or_is_stopped_or_its_reply_is_broken(self):
        cases = {"error": [agents_error()], "stopped": [supervisor_stopped()], "broken": ["no json", "still none"]}
        for name, steps in cases.items():
            with self.subTest(name):
                self.runner.made.clear()
                self.runner.removed.clear()
                self.runner.steps = list(steps)
                self.cycle()
                self.assertTrue(self.runner.made)
                self.assertEqual(self.runner.removed, self.runner.made)
                self.assertEqual([p for p in Path(self.root / "refs").iterdir()], [])

    def test_it_is_removed_when_the_run_raises(self):
        def blows_up():
            raise RunnerError("boom")
        self.runner.steps = [blows_up]
        self.cycle()
        self.assertEqual(self.runner.removed, self.runner.made)
        self.assertTrue(self.runner.made)

    def test_the_reviewers_get_the_copy_too_and_it_is_removed_after_each_review(self):
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]
        self.cycle()
        worker, reviewer = self.runner.runs
        self.assertEqual(len(self.runner.made), 2)
        self.assertEqual(self.runner.removed, self.runner.made)
        for run, folder in ((worker, self.runner.made[0]), (reviewer, self.runner.made[1])):
            self.assertIn(f"`{folder}/", run.prompt)
            self.assertIn(f"Read(/{folder}/**)", run.allowed)
        self.assertIn("read-only", reviewer.prompt)

    def test_a_repo_outside_the_registry_is_not_copied_and_the_task_gets_a_note(self):
        self.github.edit_issue(REPO, 1, "Task 1", BODY.replace(OTHER, "someone/else"))
        self.runner.steps = ['{"status": "blocked", "reason": "r"}']
        self.cycle()
        self.assertEqual((self.runner.made, self.runner.copies), ([], []))
        self.assertNotIn("Reference copy", self.runner.runs[0].prompt)
        self.assertEqual(len(self.notes()), 1)
        self.assertIn(reference.NOT_IN_REGISTRY, self.notes()[0])
        self.assertNotIn("someone", self.notes()[0])

    def test_a_private_repo_is_not_copied_into_a_run_on_a_public_repo(self):
        self.github.repo_is_public = lambda repo: repo == REPO
        self.runner.steps = ['{"status": "blocked", "reason": "r"}']
        self.cycle()
        self.assertEqual((self.runner.made, self.runner.copies), ([], []))
        self.assertIn(reference.PRIVATE_INTO_PUBLIC, self.notes()[0])
        self.assertNotIn(OTHER, self.notes()[0])
        self.assertEqual(self.github.status(1), "Blocked")  # the run went on, and ended as its worker said

    def test_a_missing_path_leaves_a_note_and_the_rest_is_copied(self):
        self.runner.missing = ["panel/static/"]
        self.runner.steps = ['{"status": "blocked", "reason": "r"}']
        self.cycle()
        self.assertIn("`panel/index.html`", self.runner.runs[0].prompt)
        self.assertNotIn("panel/static", self.runner.runs[0].prompt.split("## Reference copy")[1])
        self.assertIn(reference.MISSING_PATH, self.notes()[0])
        self.assertNotIn("panel/static", self.notes()[0])

    def test_nothing_found_goes_on_without_it_and_its_empty_folder_is_removed(self):
        self.runner.missing = ["panel/index.html", "panel/static/"]
        self.runner.steps = ['{"status": "blocked", "reason": "r"}']
        self.cycle()
        self.assertNotIn("## Reference copy", self.runner.runs[0].prompt)
        self.assertEqual(self.runner.removed, self.runner.made)
        self.assertEqual(self.github.status(1), "Blocked")

    def test_a_source_machine_that_cannot_be_reached_leaves_a_note_and_does_not_stop_the_run(self):
        self.runner.copy_error = Unreachable("laptop did not answer over SSH")
        self.runner.steps = ['{"status": "blocked", "reason": "r"}']
        self.cycle()
        self.assertEqual(len(self.runner.runs), 1)
        self.assertNotIn("## Reference copy", self.runner.runs[0].prompt)
        self.assertIn(reference.UNREACHABLE, self.notes()[0])
        self.assertNotIn("laptop", self.notes()[0])
        self.assertEqual(self.runner.removed, self.runner.made)

    def test_a_source_that_cannot_be_read_leaves_a_note_and_does_not_stop_the_run(self):
        self.runner.copy_error = RunnerError("git failed in /home/someone/x")
        self.runner.steps = ['{"status": "blocked", "reason": "r"}']
        self.cycle()
        self.assertIn(reference.UNREADABLE, self.notes()[0])
        self.assertNotIn("someone", self.notes()[0])
        self.assertEqual(len(self.runner.runs), 1)

    def test_a_note_is_said_once_however_many_runs_the_task_has(self):
        self.github.edit_issue(REPO, 1, "Task 1", BODY.replace(OTHER, "someone/else"))
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]
        self.cycle()
        self.assertEqual(len(self.runner.runs), 2)
        self.assertEqual(len(self.notes()), 1)

    def test_nothing_of_the_copy_is_in_what_the_worker_may_push(self):
        self.runner.steps = ['{"status": "blocked", "reason": "r"}']
        self.cycle()
        folder = self.runner.made[0]
        self.assertNotIn(folder, self.runner.runs[0].cwd)
        self.assertFalse([rule for rule in self.runner.runs[0].allowed if folder in rule and not rule.startswith("Read(")])


def agents_error():
    from kandu.runner import AgentResult
    return AgentResult(text="", is_error=True)


def supervisor_stopped():
    from kandu.runner import AgentResult
    return AgentResult(stopped=True)


class SettingTest(Base):
    def test_it_is_turned_on_and_off_from_the_dashboard_and_the_config_keeps_it(self):
        api = Api.__new__(Api)
        api.config, api.store, api.local_dir = self.config, self.store, self.local
        self.assertEqual(api.change_reference_copy({"enabled": ["true"]}), {"reference_copy": True})
        self.assertTrue(load_config(self.local).settings.supervisor.reference_copy)
        self.assertEqual(self.store.last_event("reference_copy_changed")["enabled"], True)
        self.assertEqual(api.change_reference_copy({"enabled": ["false"]}), {"reference_copy": False})
        self.assertFalse(load_config(self.local).settings.supervisor.reference_copy)

    def test_a_value_that_is_not_true_or_false_is_refused(self):
        api = Api.__new__(Api)
        api.config, api.store, api.local_dir = self.config, self.store, self.local
        with self.assertRaises(Exception):
            api.change_reference_copy({"enabled": ["maybe"]})

    def test_set_reference_copy_changes_only_its_line(self):
        before = (self.local / "kandu.yaml").read_text()
        set_reference_copy(True, self.local)
        after = (self.local / "kandu.yaml").read_text()
        self.assertEqual(sum(a != b for a, b in zip(before.splitlines(), after.splitlines())), 1)
        self.assertIn("reference_copy: true", after)


if __name__ == "__main__":
    unittest.main()
