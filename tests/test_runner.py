import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from kandu import ghmeter
from kandu.config import Config, Machine, Project, Setup
from kandu.runner import (KEPT_CHECKED_OUT, KEPT_NOT_WORKTREE, KEPT_UNCOMMITTED, KEPT_UNPUSHED, REMOVED, Host, LocalWork, Runner,
                          RunnerError, SCOPE_PROBE, Unreachable, child_env, holder_mark, repo_path, scope_name)

import tempfile as _tempfile
from kandu import ghmeter as _ghmeter
_ghmeter.LOG_DIR = Path(_tempfile.mkdtemp())  # the meter's lines stay out of state/

RESULT = {"type": "result", "result": "the reply", "duration_ms": 5, "is_error": False,
          "modelUsage": {"model-a": {"inputTokens": 1, "outputTokens": 2}}}
FAKE_CLAUDE = f"""#!/bin/sh
pwd > "$HOME/claude.cwd"
printf '%s\\n' "$@" > "$HOME/claude.args"
cat > "$HOME/claude.stdin"
echo 'not json'
echo '{{"type": "assistant"}}'
echo '{json.dumps(RESULT)}'
"""
FAKE_GH = """#!/bin/sh
git clone "$ORIGIN" "$4"
"""


def git(*args, cwd=None):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


def install(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class HostTest(unittest.TestCase):
    def test_local(self):
        argv = Host(Machine("a", "local", True)).argv(["git", "log"], cwd="~/my code")
        self.assertEqual(argv[:2], ["sh", "-c"])
        self.assertIn("""cd "$HOME"/'my code' && exec git log""", argv[2])

    def test_ssh_never_prompts(self):
        argv = Host(Machine("a", "ssh box", False)).argv(["true"])
        self.assertEqual(argv[:6], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "box"])

    def test_a_dropped_ssh_connection_is_not_a_missing_copy(self):
        host = Host(Machine("a", "ssh box", False))
        dropped = subprocess.CompletedProcess([], 255, "", "Connection reset by 10.0.0.2 port 22")
        with mock.patch("subprocess.run", return_value=dropped):
            with self.assertRaises(Unreachable):
                host.succeeds(["test", "-e", ".git"])
            with self.assertRaises(Unreachable):
                host.check(["git", "fetch", "origin"])

    def test_a_connection_that_hangs_is_a_drop_but_a_local_timeout_is_not(self):
        hung = subprocess.TimeoutExpired(["ssh"], 1)
        with mock.patch("subprocess.run", side_effect=hung):
            with self.assertRaises(Unreachable):
                Host(Machine("a", "ssh box", False)).run(["git", "fetch"])
            with self.assertRaises(RunnerError) as caught:
                Host(Machine("a", "local", True)).run(["git", "fetch"])
        self.assertNotIsInstance(caught.exception, Unreachable)

    def test_255_from_a_project_command_is_a_drop_only_when_the_machine_is_gone(self):
        machine = Machine("a", "ssh box", False)
        runner = Runner(Config(settings=mock.Mock(machines=[machine]), projects=[]), tempfile.mkdtemp())
        project = Project(name="site", family="f", repos=["o/site"], run="clone", machine="a", path="~/s",
                          test="t", priority=1, max_parallel_workers=1, paused=False, daily_token_limit=1000)
        exited = subprocess.CompletedProcess([], 255)
        with mock.patch("subprocess.run", return_value=exited):
            with mock.patch.object(runner, "reachable", return_value=True):
                self.assertEqual(runner.run_command(project, "~/s", "t", "log-a", scoped=False), 255)
            with mock.patch.object(runner, "reachable", return_value=False):
                with self.assertRaises(Unreachable):
                    runner.run_command(project, "~/s", "t", "log-b", scoped=False)

    def test_a_missing_file_is_still_false(self):
        host = Host(Machine("a", "ssh box", False))
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "")):
            self.assertFalse(host.succeeds(["test", "-e", ".git"]))

    def test_255_on_this_machine_is_the_commands_own(self):
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 255, "", "")):
            self.assertFalse(Host(Machine("a", "local", True)).succeeds(["false"]))

    def test_tools_in_the_users_home_are_found(self):
        self.assertIn(':$HOME/.local/bin:$PATH"', Host(Machine("a", "local", True)).argv(["true"])[2])

    def test_gh_is_the_meter_first(self):
        script = Host(Machine("a", "local", True)).argv(["true"])[2]
        self.assertIn(f'export PATH="{ghmeter.METER_DIR}:', script)

    def test_a_child_names_its_run_for_the_meter(self):
        env = child_env("r-7")
        self.assertTrue(env["PATH"].startswith(f"{ghmeter.METER_DIR}:"))
        self.assertEqual(env[ghmeter.RUN_ENV], "r-7")
        self.assertIn(ghmeter.CALLER_ENV, env)


class RepoPathTest(unittest.TestCase):
    def project(self, repos):
        return mock.Mock(path="~/w/site", repos=repos)

    def test_one_repo_is_the_path_itself(self):
        self.assertEqual(repo_path(self.project(["o/a"]), "o/a"), "~/w/site")

    def test_several_repos_have_a_folder_each(self):
        self.assertEqual(repo_path(self.project(["o/a", "o/b"]), "o/b"), "~/w/site/b")


class GitCase(unittest.TestCase):
    """Real git and a real shell; `claude` and `gh` are stubs, found first through `$HOME/.local/bin`."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        (self.home / ".local" / "bin").mkdir(parents=True)
        install(self.home / ".local" / "bin" / "claude", FAKE_CLAUDE)
        install(self.home / ".local" / "bin" / "gh", FAKE_GH)
        self.origin = self.root / "origin.git"
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "ORIGIN": str(self.origin)})
        env.start()
        self.addCleanup(env.stop)
        git("init", "--bare", "-b", "main", str(self.origin))
        self.push_commit("main", "first.txt")
        self.machine = Machine("box", "local", True)
        self.copy = self.home / "work" / "site"
        config = Config(settings=mock.Mock(machines=[self.machine]), projects=[])
        self.runner = Runner(config, self.root / "logs")

    def push_commit(self, branch, name, text="x"):
        scratch = tempfile.mkdtemp(dir=self.root)
        git("clone", str(self.origin), scratch)
        pushed = branch in git("branch", "-r", cwd=scratch)
        git("checkout", "-B", branch, *([f"origin/{branch}"] if pushed else []), cwd=scratch)
        (Path(scratch) / name).write_text(text)
        git("add", ".", cwd=scratch)
        git("commit", "-m", "c", cwd=scratch)
        git("push", "origin", branch, cwd=scratch)
        return git("rev-parse", "HEAD", cwd=scratch)

    def project(self, run="clone"):
        return Project(name="site", family="f", repos=["o/site"], run=run, machine="box", path="~/work/site",
                       test="true", priority=1, max_parallel_workers=1, paused=False, daily_token_limit=1000)



class RunnerTest(GitCase):
    def test_reachable(self):
        self.assertTrue(self.runner.reachable("box"))

    def test_clone_when_the_copy_is_missing(self):
        worktree = self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        self.assertTrue((self.copy / ".git").exists())
        self.assertEqual(git("branch", "--show-current", cwd=worktree), "kandu/1-a")

    def test_in_place_never_clones(self):
        with self.assertRaises(RunnerError):
            self.runner.prepare_worktree(self.project("in-place"), "o/site", "kandu/1-a", "main")
        self.assertFalse(self.copy.exists())

    def test_the_users_copy_is_left_alone(self):
        self.copy.parent.mkdir(parents=True)
        git("clone", str(self.origin), str(self.copy))
        (self.copy / "my-work.txt").write_text("mine")
        worktree = self.runner.prepare_worktree(self.project("in-place"), "o/site", "kandu/1-a", "main")
        self.assertTrue(worktree.startswith(f"{self.copy}.kandu-worktrees/"))
        self.assertEqual(git("branch", "--show-current", cwd=self.copy), "main")
        self.assertEqual(git("status", "--short", cwd=self.copy), "?? my-work.txt")

    def test_a_second_run_keeps_the_worktree_and_unfinished_work(self):
        first = self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        (Path(first) / "partial.txt").write_text("half")
        second = self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        self.assertEqual(first, second)
        self.assertTrue((Path(second) / "partial.txt").exists())

    def test_a_reused_worktree_catches_up_with_the_branch(self):
        worktree = self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        self.push_commit("kandu/1-a", "pushed.txt")
        self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        self.assertTrue((Path(worktree) / "pushed.txt").exists())

    def commit_here(self, worktree, name):
        (Path(worktree) / name).write_text("local")
        git("add", ".", cwd=worktree)
        git("commit", "-m", f"local {name}", cwd=worktree)
        return git("rev-parse", "HEAD", cwd=worktree)

    def test_a_worktree_ahead_of_its_pushed_branch_keeps_its_commits_and_can_push_them(self):
        pushed = self.push_commit("kandu/1-a", "pushed.txt")
        worktree = self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        local = self.commit_here(worktree, "ahead.txt")
        again = self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        self.assertEqual(again, worktree)
        self.assertEqual(git("rev-parse", "HEAD", cwd=worktree), local)
        self.assertEqual(git("rev-parse", "HEAD~1", cwd=worktree), pushed)
        git("push", "origin", "kandu/1-a", cwd=worktree)  # a plain push, as a worker's, still goes through
        self.assertEqual(git("rev-parse", "kandu/1-a", cwd=str(self.origin)), local)

    def test_a_worktree_equal_to_its_pushed_branch_stays_there(self):
        pushed = self.push_commit("kandu/1-a", "pushed.txt")
        worktree = self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        self.assertEqual(git("rev-parse", "HEAD", cwd=worktree), pushed)

    def test_a_worktree_behind_its_pushed_branch_is_fast_forwarded(self):
        first = self.push_commit("kandu/1-a", "pushed.txt")
        worktree = self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        later = self.push_commit("kandu/1-a", "later.txt")
        self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        self.assertEqual(git("rev-parse", "HEAD", cwd=worktree), later)
        self.assertEqual(git("rev-parse", "HEAD~1", cwd=worktree), first)

    def identity(self):
        names = {k: "t" for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")}
        names |= {k: "t@example.com" for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL")}
        env = mock.patch.dict(os.environ, names)
        env.start()
        self.addCleanup(env.stop)

    def test_a_local_branch_rebased_onto_a_new_base_with_an_extra_commit_is_pushed_with_a_lease(self):
        # The drift in #105: the worker rebased its pushed work onto a newer main, added a commit, and stopped
        # before it pushed. GitHub still has the old head.
        self.identity()
        self.push_commit("kandu/1-a", "pushed.txt")
        worktree = self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        pushed = git("rev-parse", "HEAD", cwd=worktree)
        self.push_commit("main", "newer-main.txt")
        git("fetch", "origin", cwd=worktree)
        git("rebase", "origin/main", cwd=worktree)
        local = self.commit_here(worktree, "extra.txt")
        settled = []
        self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main", settled=settled.append)
        self.assertEqual(git("rev-parse", "HEAD", cwd=worktree), local)
        self.assertEqual(git("rev-parse", "kandu/1-a", cwd=str(self.origin)), local)
        self.assertEqual(git("diff", "--name-only", f"{pushed}", local, cwd=worktree).split(),
                         ["extra.txt", "newer-main.txt"])  # the pushed work is still there, rebased
        self.assertEqual(len(settled), 1)
        self.assertIn("force-with-lease", settled[0])
        self.assertIn(pushed[:12], settled[0])  # the replaced head can still be found from the log

    def test_a_drifted_branch_that_is_not_a_rebase_gets_the_pushed_head_merged_in_and_keeps_both_sides(self):
        self.identity()
        self.push_commit("kandu/1-a", "pushed.txt")
        worktree = self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        local = self.commit_here(worktree, "mine.txt")
        remote = self.push_commit("kandu/1-a", "theirs.txt")
        settled = []
        self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main", settled=settled.append)
        self.assertEqual(git("rev-parse", "HEAD^1", cwd=worktree), local)
        self.assertEqual(git("rev-parse", "HEAD^2", cwd=worktree), remote)
        self.assertEqual(git("status", "--porcelain", cwd=worktree), "")
        self.assertEqual(git("rev-parse", "kandu/1-a", cwd=str(self.origin)), remote)  # the worker pushes it
        self.assertEqual(len(settled), 1)
        self.assertIn("merged", settled[0])
        git("push", "origin", "kandu/1-a", cwd=worktree)  # a plain push now goes through

    def test_a_rebase_that_lost_part_of_the_pushed_work_is_merged_not_forced(self):
        self.identity()
        self.push_commit("kandu/1-a", "pushed.txt")
        worktree = self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        self.push_commit("main", "newer-main.txt")
        git("fetch", "origin", cwd=worktree)
        git("rebase", "origin/main", cwd=worktree)
        local = self.commit_here(worktree, "extra.txt")
        remote = self.push_commit("kandu/1-a", "theirs.txt")  # pushed after the rebase: local lacks it
        settled = []
        self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main", settled=settled.append)
        self.assertEqual(git("rev-parse", "kandu/1-a", cwd=str(self.origin)), remote)
        self.assertEqual(git("rev-parse", "HEAD^1", cwd=worktree), local)
        self.assertEqual(git("rev-parse", "HEAD^2", cwd=worktree), remote)
        self.assertIn("merged", settled[0])

    def test_a_pushed_merge_commit_is_never_forced_over(self):
        self.identity()
        self.push_commit("kandu/1-a", "pushed.txt")
        worktree = self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        self.push_commit("main", "newer-main.txt")
        git("fetch", "origin", cwd=worktree)
        git("merge", "--no-ff", "-m", "Merge main", "origin/main", cwd=worktree)
        merged = git("rev-parse", "HEAD", cwd=worktree)
        git("push", "origin", "kandu/1-a", cwd=worktree)
        git("reset", "--hard", "HEAD^1", cwd=worktree)
        git("rebase", "origin/main", cwd=worktree)  # every change is in the folder, but not the merge itself
        local = git("rev-parse", "HEAD", cwd=worktree)
        settled = []
        self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main", settled=settled.append)
        self.assertEqual(git("rev-parse", "kandu/1-a", cwd=str(self.origin)), merged)
        self.assertEqual(git("rev-parse", "HEAD^1", cwd=worktree), local)
        self.assertEqual(git("rev-parse", "HEAD^2", cwd=worktree), merged)
        self.assertIn("merged", settled[0])

    def test_a_drift_whose_merge_conflicts_stops_the_task_and_leaves_the_folder_as_it_was(self):
        self.identity()
        self.push_commit("kandu/1-a", "same.txt", "first\n")
        worktree = self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        local = self.commit_here(worktree, "same.txt")
        remote = self.push_commit("kandu/1-a", "same.txt", "theirs\n")
        settled = []
        with self.assertRaises(LocalWork) as stop:
            self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main", settled=settled.append)
        self.assertIn("cannot settle", str(stop.exception))
        self.assertIn("same.txt", str(stop.exception))
        self.assertEqual(settled, [])
        self.assertEqual(git("rev-parse", "HEAD", cwd=worktree), local)
        self.assertEqual(git("status", "--porcelain", cwd=worktree), "")
        self.assertEqual((Path(worktree) / "same.txt").read_text(), "local")
        self.assertEqual(git("rev-parse", "kandu/1-a", cwd=str(self.origin)), remote)

    def test_a_drift_with_uncommitted_changes_stops_the_task_and_leaves_them(self):
        self.identity()
        self.push_commit("kandu/1-a", "pushed.txt")
        worktree = self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        local = self.commit_here(worktree, "mine.txt")
        (Path(worktree) / "mine.txt").write_text("not committed")
        remote = self.push_commit("kandu/1-a", "theirs.txt")
        with self.assertRaises(LocalWork) as stop:
            self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main", settled=[].append)
        self.assertIn("uncommitted", str(stop.exception))
        self.assertEqual(git("rev-parse", "HEAD", cwd=worktree), local)
        self.assertEqual((Path(worktree) / "mine.txt").read_text(), "not committed")
        self.assertEqual(git("rev-parse", "kandu/1-a", cwd=str(self.origin)), remote)

    def test_a_drift_whose_merge_git_refuses_to_start_stops_the_task_and_keeps_the_untracked_file(self):
        self.identity()
        self.push_commit("kandu/1-a", "pushed.txt")
        worktree = self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        local = self.commit_here(worktree, "mine.txt")
        (Path(worktree) / "theirs.txt").write_text("untracked here")
        remote = self.push_commit("kandu/1-a", "theirs.txt", "tracked on GitHub")
        settled = []
        with self.assertRaises(LocalWork) as stop:
            self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main", settled=settled.append)
        self.assertIn("failed", str(stop.exception))
        self.assertEqual(settled, [])
        self.assertEqual(git("rev-parse", "HEAD", cwd=worktree), local)
        self.assertEqual((Path(worktree) / "theirs.txt").read_text(), "untracked here")
        self.assertEqual(git("rev-parse", "kandu/1-a", cwd=str(self.origin)), remote)

    def test_a_lease_that_github_refuses_stops_the_task_and_leaves_both_branches(self):
        self.identity()
        self.push_commit("kandu/1-a", "pushed.txt")
        worktree = self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        self.push_commit("main", "newer-main.txt")
        git("fetch", "origin", cwd=worktree)
        git("rebase", "origin/main", cwd=worktree)
        local = self.commit_here(worktree, "extra.txt")
        host = self.runner.hosts["box"]
        real = host.run
        moved = {}

        def run(argv, *args, **kwargs):  # someone pushes to the branch between Kandu's fetch and its push
            if argv[:2] == ["git", "push"] and not moved:
                moved["sha"] = self.push_commit("kandu/1-a", "late.txt")
            return real(argv, *args, **kwargs)
        with mock.patch.object(host, "run", side_effect=run):
            with self.assertRaises(LocalWork) as stop:
                self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main", settled=[].append)
        self.assertIn("cannot settle", str(stop.exception))
        self.assertEqual(git("rev-parse", "HEAD", cwd=worktree), local)
        self.assertEqual(git("rev-parse", "kandu/1-a", cwd=str(self.origin)), moved["sha"])

    def conflict_in(self, worktree):
        """Leaves `worktree` with a commit of its own: main and the branch changed one file apart."""
        self.push_commit("main", "same.txt")
        (Path(worktree) / "same.txt").write_text("the branch's")
        git("add", ".", cwd=worktree)
        git("commit", "-m", "branch side", cwd=worktree)

    def start_merge(self, worktree):
        git("fetch", "origin", cwd=worktree)
        merge = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "merge", "origin/main"],
                               cwd=worktree, capture_output=True, text=True)
        self.assertNotEqual(merge.returncode, 0)  # a conflict, left unresolved

    def test_a_worktree_left_mid_merge_starts_cleanly_from_the_branch_on_github(self):
        worktree = self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        self.conflict_in(worktree)
        git("push", "origin", "kandu/1-a", cwd=worktree)
        self.start_merge(worktree)
        pushed = self.push_commit("kandu/1-a", "later.txt")
        cleaned = []
        again = self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main", cleaned=cleaned.append)
        self.assertEqual(again, worktree)
        self.assertEqual(git("rev-parse", "HEAD", cwd=worktree), pushed)
        self.assertEqual(git("status", "--porcelain", "--untracked-files=no", cwd=worktree), "")
        self.assertEqual(len(cleaned), 1)
        self.assertIn("merge", cleaned[0])

    def test_a_clean_worktree_reports_no_cleanup(self):
        self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        cleaned = []
        self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main", cleaned=cleaned.append)
        self.assertEqual(cleaned, [])

    def test_unpushed_commits_in_a_worktree_mid_merge_stop_the_task_and_nothing_is_lost(self):
        worktree = self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        self.conflict_in(worktree)  # its commit is only in this folder
        local = git("rev-parse", "HEAD", cwd=worktree)
        self.start_merge(worktree)
        cleaned = []
        with self.assertRaises(LocalWork) as stop:
            self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main", cleaned=cleaned.append)
        self.assertIn("not on GitHub", str(stop.exception))
        self.assertEqual(cleaned, [])
        self.assertEqual(git("rev-parse", "HEAD", cwd=worktree), local)
        self.assertEqual((Path(worktree) / "same.txt").read_text().count("<<<<<<<"), 1)  # left as it was

    def merging(self):
        """A task branch and a base that both changed `same.txt`."""
        identity = {k: "t" for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")}
        identity |= {k: "t@example.com" for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL")}
        env = mock.patch.dict(os.environ, identity)
        env.start()
        self.addCleanup(env.stop)
        self.push_commit("kandu/1-a", "same.txt", "ours\n")
        return self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")

    def merge(self, worktree):
        return self.runner.merge_base(self.project(), "o/site", worktree, "kandu/1-a", "main",
                                      "Merge main into kandu/1-a\n\nKandu-Run: r-1")

    def test_a_clean_merge_of_the_base_is_committed_and_pushed(self):
        worktree = self.merging()
        base = self.push_commit("main", "new.txt")
        self.assertEqual(self.merge(worktree), (base, []))
        self.runner.push_branch(self.project(), worktree, "kandu/1-a")
        files = git("ls-tree", "--name-only", "kandu/1-a", cwd=self.origin).split()
        self.assertEqual(sorted(files), ["first.txt", "new.txt", "same.txt"])
        self.assertIn("Kandu-Run: r-1", git("log", "-1", "--format=%B", "kandu/1-a", cwd=self.origin))
        self.assertTrue(self.runner.has_commit(self.project(), "o/site", worktree, "kandu/1-a", base))

    def test_commits_a_run_did_not_push_are_kept_before_the_merge(self):
        worktree = self.merging()
        (Path(worktree) / "late.txt").write_text("cut off\n")
        git("add", "late.txt", cwd=worktree)
        git("commit", "-m", "not pushed", cwd=worktree)
        late = git("rev-parse", "HEAD", cwd=worktree).strip()
        notes = []
        self.runner.merge_base(self.project(), "o/site", worktree, "kandu/1-a", "main", "Merge", kept=notes.append)
        self.assertEqual(git("rev-parse", f"refs/kandu/kept/1-a/{late[:12]}", cwd=worktree).strip(), late)
        self.assertEqual(len(notes), 1)
        self.assertIn("1 commit(s)", notes[0])

    def test_a_merge_with_nothing_unpushed_keeps_nothing(self):
        worktree = self.merging()
        notes = []
        self.runner.merge_base(self.project(), "o/site", worktree, "kandu/1-a", "main", "Merge", kept=notes.append)
        self.assertEqual(notes, [])

    def test_a_conflicting_merge_is_left_open_and_its_markers_are_found(self):
        worktree = self.merging()
        base = self.push_commit("main", "same.txt", "theirs\n")
        self.assertEqual(self.merge(worktree), (base, ["same.txt"]))
        self.assertFalse(self.runner.has_commit(self.project(), "o/site", worktree, "kandu/1-a", base))
        git("add", "same.txt", cwd=worktree)  # a worker that commits the markers as they are
        git("commit", "--no-edit", cwd=worktree)
        self.runner.push_branch(self.project(), worktree, "kandu/1-a")
        markers = self.runner.conflict_markers(self.project(), "o/site", worktree, "kandu/1-a",
                                               ["same.txt", "first.txt"])
        self.assertEqual(markers, ["same.txt"])
        (Path(worktree) / "same.txt").write_text("ours\ntheirs\n")
        git("commit", "-am", "resolved", cwd=worktree)
        self.runner.push_branch(self.project(), worktree, "kandu/1-a")
        self.assertEqual(self.runner.conflict_markers(self.project(), "o/site", worktree, "kandu/1-a", ["same.txt"]),
                         [])
        self.assertTrue(self.runner.has_commit(self.project(), "o/site", worktree, "kandu/1-a", base))

    def test_a_merge_left_open_is_cancelled_before_the_next_run(self):
        worktree = self.merging()
        self.push_commit("main", "same.txt", "theirs\n")
        self.merge(worktree)
        cleaned = []
        self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main", cleaned=cleaned.append)
        self.assertEqual(git("status", "--short", cwd=worktree), "")
        self.assertEqual((Path(worktree) / "same.txt").read_text(), "ours\n")
        self.assertEqual(len(cleaned), 1)

    def test_a_merge_left_open_whose_staged_file_changed_since_is_still_cleared_before_the_next_run(self):
        # A worker resolves the conflict and stages it, then a build rewrites a staged file and it stops without a
        # commit. `git merge --abort` refuses that folder ("not uptodate"), and once stopped every run on the task.
        worktree = self.merging()
        self.push_commit("main", "same.txt", "theirs\n")
        self.push_commit("main", "built.html", "from main\n")
        self.merge(worktree)
        (Path(worktree) / "same.txt").write_text("ours\ntheirs\n")
        (Path(worktree) / "built.html").write_text("rebuilt once\n")
        git("add", "same.txt", "built.html", cwd=worktree)
        (Path(worktree) / "built.html").write_text("rebuilt twice\n")
        refused = subprocess.run(["git", "merge", "--abort"], cwd=worktree, capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)  # the folder git refuses, as on the machine
        cleaned = []
        self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main", cleaned=cleaned.append)
        self.assertEqual(git("status", "--short", cwd=worktree), "")
        self.assertEqual(git("branch", "--show-current", cwd=worktree), "kandu/1-a")
        self.assertEqual(git("rev-parse", "HEAD", cwd=worktree), git("rev-parse", "origin/kandu/1-a", cwd=worktree))
        self.assertEqual((Path(worktree) / "same.txt").read_text(), "ours\n")
        self.assertFalse((Path(worktree) / "built.html").exists())
        self.assertEqual(len(cleaned), 1)
        self.assertIn("refused", cleaned[0])

    def test_work_left_uncommitted_does_not_stop_the_merge(self):
        worktree = self.merging()
        (Path(worktree) / "new.txt").write_text("half done\n")
        (Path(worktree) / "stray.txt").write_text("x\n")
        base = self.push_commit("main", "new.txt", "from main\n")
        self.assertEqual(self.merge(worktree), (base, []))
        self.assertEqual((Path(worktree) / "new.txt").read_text(), "from main\n")
        self.assertFalse((Path(worktree) / "stray.txt").exists())

    def test_a_branch_is_pushed_or_not(self):
        self.assertFalse(self.runner.branch_pushed(self.project(), "o/site", "kandu/1-a"))
        self.push_commit("kandu/1-a", "one.txt")
        self.assertTrue(self.runner.branch_pushed(self.project(), "o/site", "kandu/1-a"))

    def test_a_pushed_branch_is_picked_up_after_the_folder_is_gone(self):
        self.push_commit("kandu/1-a", "pushed.txt")
        worktree = self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        self.assertTrue((Path(worktree) / "pushed.txt").exists())

    def test_two_tasks_at_once_on_one_repo(self):
        def prepare(n):
            self.runner.prepare_worktree(self.project(), "o/site", f"kandu/{n}-a", "main")
        threads = [threading.Thread(target=prepare, args=(n,)) for n in (1, 2, 3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(list(Path(f"{self.copy}.kandu-worktrees").iterdir())), 3)

    def test_review_checkout_is_the_exact_commit(self):
        first = self.push_commit("kandu/1-a", "one.txt")
        second = self.push_commit("kandu/1-a", "two.txt")
        directory = self.runner.prepare_review(self.project(), "o/site", "kandu/1-a", first)
        self.assertEqual(git("rev-parse", "HEAD", cwd=directory), first)
        self.runner.prepare_review(self.project(), "o/site", "kandu/1-a", second)
        self.assertEqual(git("rev-parse", "HEAD", cwd=directory), second)

    def agent(self, should_stop=lambda: False, prompt="the prompt", model=None, schema=None):
        return self.runner.run_agent("box", str(self.home), prompt, ["Read", "Bash(npm test *)"], ["Edit"], "r-1",
                                     should_stop, model, schema=schema)

    def test_a_schema_is_passed_and_its_object_is_the_text(self):
        self.agent()
        self.assertNotIn("--json-schema", (self.home / "claude.args").read_text().splitlines())
        held = dict(RESULT, result="```json {} ```", structured_output={"a": "x", "b": []})
        install(self.home / ".local" / "bin" / "claude", FAKE_CLAUDE.replace(json.dumps(RESULT), json.dumps(held)))
        schema = {"type": "object", "required": ["a"]}
        result = self.agent(schema=schema)
        args = (self.home / "claude.args").read_text().splitlines()
        self.assertEqual(json.loads(args[args.index("--json-schema") + 1]), schema)
        self.assertEqual(json.loads(result.text), {"a": "x", "b": []})

    def test_a_session_is_kept_so_a_broken_reply_can_be_asked_for_again(self):
        held = dict(RESULT, session_id="s-1")
        install(self.home / ".local" / "bin" / "claude", FAKE_CLAUDE.replace(json.dumps(RESULT), json.dumps(held)))
        result = self.agent()
        args = (self.home / "claude.args").read_text().splitlines()
        self.assertEqual(result.session_id, "s-1")
        self.assertNotIn("--no-session-persistence", args)
        self.assertNotIn("--resume", args)
        self.runner.run_agent("box", str(self.home), "again", ["Read"], [], "r-2", lambda: False, resume="s-1")
        args = (self.home / "claude.args").read_text().splitlines()
        self.assertEqual(args[args.index("--resume") + 1], "s-1")

    def test_a_model_is_passed_only_when_one_is_set(self):
        self.agent()
        self.assertNotIn("--model", (self.home / "claude.args").read_text().splitlines())
        self.agent(model="claude-sonnet-5-5")
        args = (self.home / "claude.args").read_text().splitlines()
        self.assertEqual(args[args.index("--model") + 1], "claude-sonnet-5-5")

    def test_agent_result_and_arguments(self):
        result = self.agent()
        self.assertEqual((result.text, result.is_error, result.stopped), ("the reply", False, False))
        self.assertEqual(result.models["model-a"]["output"], 2)
        args = (self.home / "claude.args").read_text().splitlines()
        self.assertIn("Bash(npm test *)", args)
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(args[args.index("--disallowedTools") + 1], "Edit")
        self.assertEqual(args[args.index("--setting-sources") + 1], "user")
        self.assertEqual(args[args.index("--output-format") + 1], "stream-json")
        self.assertNotIn("--dangerously-skip-permissions", args)
        self.assertEqual((self.home / "claude.stdin").read_text(), "the prompt")
        self.assertEqual((self.home / "claude.cwd").read_text().strip(), str(self.home))

    def test_with_no_allow_list_the_run_may_do_everything(self):
        self.runner.run_agent("box", str(self.home), "p", None, [], "r-1", lambda: False)
        args = (self.home / "claude.args").read_text().splitlines()
        self.assertEqual(args[args.index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(args[args.index("--setting-sources") + 1], "user")
        self.assertNotIn("--allowedTools", args)
        self.assertNotIn("--disallowedTools", args)

    def test_a_large_prompt_does_not_hang(self):
        prompt = "x" * 1_000_000
        self.agent(prompt=prompt)
        self.assertEqual((self.home / "claude.stdin").stat().st_size, len(prompt))

    def test_log_is_kept_privately(self):
        self.agent()
        log = self.root / "logs" / "r-1.jsonl"
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "logs").stat().st_mode & 0o777, 0o700)
        self.assertIn('"type": "assistant"', log.read_text())

    def test_no_result_is_an_error(self):
        install(self.home / ".local" / "bin" / "claude", "#!/bin/sh\ncat > /dev/null\necho crashed\nexit 1\n")
        self.assertTrue(self.agent().is_error)

    def test_a_failing_exit_is_an_error_even_with_a_result(self):
        install(self.home / ".local" / "bin" / "claude", FAKE_CLAUDE + "exit 1\n")
        self.assertTrue(self.agent().is_error)

    def test_kandu_secrets_stay_out_of_project_commands(self):
        project = self.project()
        self.runner.prepare_worktree(project, "o/site", "kandu/1-x", "main")
        seen = self.root / "seen"
        project.test = f"env > {seen}"
        with mock.patch.dict(os.environ, {"KANDU_TELEGRAM_BOT_TOKEN": "hidden", "OTHER": "shown"}):
            cwd = self.runner.prepare_review(project, "o/site", "kandu/1-x", "origin/main")
            self.assertEqual(self.runner.run_command(project, cwd, project.test, "run-env"), 0)
        self.assertIn("OTHER=shown", seen.read_text())
        self.assertNotIn("hidden", seen.read_text())

    def left_behind(self, marker):
        """The pids of processes still running whose command line holds `marker`."""
        found = subprocess.run(["pgrep", "-f", f"^{marker}"], capture_output=True, text=True).stdout.split()
        return [pid for pid in found if int(pid) != os.getpid()]

    @unittest.skipUnless(subprocess.run(["sh", "-c", SCOPE_PROBE]).returncode == 0, "needs a user systemd")
    def test_what_an_agent_leaves_running_ends_with_it(self):
        marker = f"sleep 300.{os.getpid()}1"
        install(self.home / ".local" / "bin" / "claude",
                f"#!/bin/sh\nsetsid {marker} </dev/null >/dev/null 2>&1 &\n" + FAKE_CLAUDE.split("\n", 1)[1])
        self.assertFalse(self.agent().is_error)
        self.assertEqual(self.left_behind(marker), [])

    @unittest.skipUnless(subprocess.run(["sh", "-c", SCOPE_PROBE]).returncode == 0, "needs a user systemd")
    def test_what_a_test_leaves_running_ends_with_it_but_a_deploy_keeps_it(self):
        project = self.project()
        marker = f"sleep 300.{os.getpid()}2"
        command = f"setsid {marker} </dev/null >/dev/null 2>&1 &"
        self.assertEqual(self.runner.run_command(project, str(self.home), command, "run-left"), 0)
        self.assertEqual(self.left_behind(marker), [])
        self.assertEqual(self.runner.run_command(project, str(self.home), command, "run-kept", scoped=False), 0)
        kept = self.left_behind(marker)
        self.assertTrue(kept)
        subprocess.run(["kill", *kept])

    def test_with_no_user_systemd_a_command_runs_as_it_is(self):
        bare = self.home / "bare-bin"  # sh and echo, and no systemd-run
        bare.mkdir()
        for tool in ("sh", "echo"):
            (bare / tool).symlink_to(shutil.which(tool))
        result = subprocess.run(Host.scoped(["echo", "ran"], "kandu-t"), capture_output=True, text=True,
                                env={"PATH": str(bare), "HOME": str(self.home)})
        self.assertEqual(result.stdout.strip(), "ran")

    def without_systemd(self):
        """This machine as one with no user systemd, such as a Windows one through Git Bash."""
        install(self.home / ".local" / "bin" / "systemctl", "#!/bin/sh\nexit 1\n")

    def start_left_run(self, holder, marker):
        """A run of `holder` still going, as a cycle that died leaves one: it and the child it started."""
        host = self.runner.hosts["box"]
        command = ["sh", "-c", f'{marker} & {marker}', "sh"]
        process = subprocess.Popen(host.argv(Host.scoped(command, scope_name("r-old", holder))),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env=child_env())
        self.addCleanup(process.wait)
        self.addCleanup(lambda: subprocess.run(["pkill", "-KILL", "-f", f"^{marker}"]))
        for _ in range(50):
            if len(self.left_behind(marker)) == 2:
                return
            time.sleep(0.1)
        self.fail("the left run did not start")

    def test_with_no_user_systemd_a_stopped_agent_ends_with_all_it_started(self):
        self.without_systemd()
        marker = f"sleep 300.{os.getpid()}3"
        install(self.home / ".local" / "bin" / "claude", f"#!/bin/sh\n{marker} &\n{marker}\n")
        with mock.patch("kandu.runner.STOP_GRACE_SECONDS", 0.5):
            result = self.agent(should_stop=lambda: bool(self.left_behind(marker)))
        self.assertTrue(result.stopped)
        self.assertEqual(self.left_behind(marker), [])

    def test_with_no_user_systemd_a_run_ends_what_an_earlier_run_of_its_holder_left_running(self):
        self.without_systemd()
        self.check_earlier_runs_end("5")

    @unittest.skipUnless(subprocess.run(["sh", "-c", SCOPE_PROBE]).returncode == 0, "needs a user systemd")
    def test_a_run_ends_the_scope_an_earlier_run_of_its_holder_left_running(self):
        self.check_earlier_runs_end("6")

    def check_earlier_runs_end(self, tag):
        mine, other = f"sleep 300.{os.getpid()}{tag}1", f"sleep 300.{os.getpid()}{tag}2"
        self.start_left_run("o/site#1", mine)
        self.start_left_run("o/site#2", other)
        result = self.runner.run_agent("box", str(self.home), "p", ["Read"], [], "r-new", lambda: False,
                                       holder="o/site#1")
        self.assertFalse(result.is_error)
        self.assertEqual(len(result.ended_earlier), 1)
        self.assertEqual(self.left_behind(mine), [])
        self.assertEqual(len(self.left_behind(other)), 2)  # another task's run is not this one's to end
        self.assertEqual(self.runner.run_agent("box", str(self.home), "p", ["Read"], [], "r-next", lambda: False,
                                               holder="o/site#1").ended_earlier, [])

    def test_a_holder_s_runs_share_a_mark_no_other_holder_s_run_has(self):
        unit = scope_name("r-1", "o/site#1")
        self.assertTrue(unit.startswith(holder_mark("o/site#1")))
        self.assertFalse(unit.startswith(holder_mark("o/site#10")))
        self.assertFalse(scope_name("r-1").startswith(holder_mark("o/site#1")))
        self.assertRegex(unit, r"^[A-Za-z0-9_.-]+$")

    def test_a_scoped_command_stays_short_enough_for_a_windows_ssh_command(self):
        # cmd.exe, which Windows OpenSSH runs the command under, cuts it at 8191 characters
        scoped = Host.scoped(["claude", "-p"], scope_name("r-1", "o/site#1"))
        self.assertLess(len(Host(Machine("a", "ssh box", False)).argv(scoped, "/w")[-1]), 2500)

    def test_kandu_secrets_stay_out_of_every_child(self):
        with mock.patch.dict(os.environ, {"KANDU_DASHBOARD_TOKEN": "hidden", "OTHER": "shown"}):
            env = child_env()
        self.assertEqual(env["OTHER"], "shown")
        self.assertFalse([name for name in env if name.startswith("KANDU_")])

    def test_kandu_secrets_stay_out_of_agents(self):
        install(self.home / ".local" / "bin" / "claude", "#!/bin/sh\nenv > \"$HOME/claude.env\"\n" + FAKE_CLAUDE.split("\n", 1)[1])
        with mock.patch.dict(os.environ, {"KANDU_TELEGRAM_BOT_TOKEN": "hidden", "OTHER": "shown"}):
            self.agent()
        seen = (self.home / "claude.env").read_text()
        self.assertIn("OTHER=shown", seen)
        self.assertNotIn("hidden", seen)

    def token_agent(self, text, names):
        install(self.home / ".local" / "bin" / "claude", "#!/bin/sh\nenv > \"$HOME/claude.env\"\n" + FAKE_CLAUDE.split("\n", 1)[1])
        (self.home / "vars").write_text(text)
        return self.runner.run_agent("box", str(self.home), "p", ["Read"], ["Edit"], "r-1", lambda: False, None,
                                     (str(self.home / "vars"), names))

    def test_a_token_task_gets_only_the_named_tokens(self):
        self.token_agent('A_KEY=one\nexport B_KEY="two"\nC_KEY=\'three\'\r\nOTHER_KEY=hidden\nexport MORE=hidden\n',
                         ["A_KEY", "B_KEY", "C_KEY"])
        seen = (self.home / "claude.env").read_text().splitlines()
        self.assertIn("A_KEY=one", seen)
        self.assertIn("B_KEY=two", seen)
        self.assertIn("C_KEY=three", seen)
        self.assertNotIn("hidden", "\n".join(seen))

    def test_a_missing_token_ends_the_run_without_starting_claude(self):
        self.assertTrue(self.token_agent("OTHER=1\n", ["A_KEY"]).is_error)
        self.assertFalse((self.home / "claude.env").exists())

    def test_token_values_are_not_on_the_command_line(self):
        self.token_agent("A_KEY=topsecret\n", ["A_KEY"])
        self.assertNotIn("topsecret", (self.home / "claude.args").read_text())

    def test_other_agents_get_no_tokens(self):
        install(self.home / ".local" / "bin" / "claude", "#!/bin/sh\nenv > \"$HOME/claude.env\"\n" + FAKE_CLAUDE.split("\n", 1)[1])
        self.agent()
        self.assertNotIn("A_KEY", (self.home / "claude.env").read_text())

    def test_stop_ends_a_running_agent(self):
        install(self.home / ".local" / "bin" / "claude", "#!/bin/sh\nexec sleep 60\n")
        started = time.monotonic()
        with mock.patch("kandu.runner.POLL_SECONDS", 0.01):
            result = self.agent(should_stop=lambda: True)
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(result.stopped)


class CleanTaskTest(GitCase):
    """Only inside a temporary folder: the copy, its worktrees and its origin are all made here."""

    def setUp(self):
        super().setUp()
        self.worktree = Path(self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main"))
        (self.worktree / "work.txt").write_text("done")
        git("add", ".", cwd=self.worktree)
        git("commit", "-m", "work", cwd=self.worktree)
        git("push", "origin", "kandu/1-a", cwd=self.worktree)
        self.head = git("rev-parse", "HEAD", cwd=self.worktree)
        self.review = Path(self.runner.prepare_review(self.project(), "o/site", "kandu/1-a", self.head))

    def clean(self, folders=("1-a", "1-a-review", "check-1-review")):
        return self.runner.clean_task(self.project(), "o/site", "kandu/1-a", list(folders), self.head)

    def branches(self):
        return git("branch", "--format=%(refname:short)", cwd=self.copy).split()

    def test_a_merged_task_loses_its_folders_and_its_branch(self):
        self.assertEqual(self.clean(), [("folder", "1-a", REMOVED), ("folder", "1-a-review", REMOVED),
                                        ("branch", "kandu/1-a", REMOVED)])
        self.assertFalse(self.worktree.exists())
        self.assertFalse(self.review.exists())
        self.assertNotIn("kandu/1-a", self.branches())
        self.assertEqual(self.clean(), [])

    def test_ignored_files_do_not_count(self):
        (self.worktree / ".gitignore").write_text("build/\n")
        git("add", ".", cwd=self.worktree)
        git("commit", "-m", "ignore", cwd=self.worktree)
        git("push", "origin", "kandu/1-a", cwd=self.worktree)
        self.head = git("rev-parse", "HEAD", cwd=self.worktree)
        (self.worktree / "build").mkdir()
        (self.worktree / "build" / "out.bin").write_text("x")
        self.assertIn(("folder", "1-a", REMOVED), self.clean())

    def test_uncommitted_changes_keep_the_folder_and_its_branch(self):
        (self.worktree / "new.txt").write_text("not committed")
        found = self.clean()
        self.assertEqual(found, [("folder", "1-a", KEPT_UNCOMMITTED), ("folder", "1-a-review", REMOVED),
                                 ("branch", "kandu/1-a", KEPT_CHECKED_OUT)])
        self.assertTrue((self.worktree / "new.txt").exists())
        self.assertFalse(self.review.exists())

    def test_a_commit_not_in_the_pr_and_not_pushed_keeps_the_folder(self):
        (self.worktree / "later.txt").write_text("later")
        git("add", ".", cwd=self.worktree)
        git("commit", "-m", "later", cwd=self.worktree)
        self.assertEqual(self.clean(["1-a"])[0], ("folder", "1-a", KEPT_UNPUSHED))
        self.assertTrue(self.worktree.exists())

    def test_a_commit_on_a_branch_github_no_longer_has_keeps_the_folder(self):
        (self.worktree / "later.txt").write_text("later")
        git("add", ".", cwd=self.worktree)
        git("commit", "-m", "later", cwd=self.worktree)
        git("push", "origin", "kandu/1-a", cwd=self.worktree)
        git("branch", "-D", "kandu/1-a", cwd=self.origin)  # gone on GitHub; the copy still tracks it
        self.assertEqual(self.clean(["1-a"])[0], ("folder", "1-a", KEPT_UNPUSHED))

    def test_a_commit_pushed_after_the_merge_is_safe_on_github(self):
        (self.worktree / "later.txt").write_text("later")
        git("add", ".", cwd=self.worktree)
        git("commit", "-m", "later", cwd=self.worktree)
        git("push", "origin", "kandu/1-a", cwd=self.worktree)
        self.assertEqual(self.clean(["1-a"])[0], ("folder", "1-a", REMOVED))

    def test_a_branch_with_commits_of_its_own_stays(self):
        git("worktree", "remove", "--force", str(self.worktree), cwd=self.copy)
        scratch = self.root / "scratch"
        git("worktree", "add", str(scratch), "kandu/1-a", cwd=self.copy)
        (scratch / "mine.txt").write_text("x")
        git("add", ".", cwd=scratch)
        git("commit", "-m", "mine", cwd=scratch)
        git("worktree", "remove", "--force", str(scratch), cwd=self.copy)
        self.assertEqual(self.clean([]), [("branch", "kandu/1-a", KEPT_UNPUSHED)])
        self.assertIn("kandu/1-a", self.branches())

    def test_a_folder_that_is_no_worktree_of_the_copy_stays(self):
        stray = Path(f"{self.copy}.kandu-worktrees") / "check-1-review"
        stray.mkdir()
        self.assertIn(("folder", "check-1-review", KEPT_NOT_WORKTREE), self.clean())
        self.assertTrue(stray.exists())

    def test_the_users_copy_and_other_branches_stay(self):
        git("branch", "kandu/2-b", cwd=self.copy)
        (self.copy / "mine.txt").write_text("mine")
        self.clean()
        self.assertTrue((self.copy / "mine.txt").exists())
        self.assertIn("kandu/2-b", self.branches())
        self.assertIn("main", self.branches())

    def test_only_a_kandu_branch_and_plain_folder_names(self):
        for branch, folders in (("main", ["1-a"]), ("kandu/1-a", ["../site"]), ("kandu/1-a", [".git"])):
            with self.assertRaises(RunnerError):
                self.runner.clean_task(self.project(), "o/site", branch, folders, self.head)
        self.assertTrue(self.worktree.exists())

    def test_no_copy_on_the_machine_is_nothing_to_do(self):
        project = self.project()
        project.path = "~/work/elsewhere"
        self.assertEqual(self.runner.clean_task(project, "o/site", "kandu/1-a", ["1-a"], self.head), [])


class SetupTest(GitCase):
    """A project's `setup` runs before Kandu's own test runs, only when the checkout needs it."""

    def setUp(self):
        super().setUp()
        self.runs = self.root / "setup-runs"
        self.project_ = self.project()
        self.project_.setup = Setup(f"echo ran >> {self.runs}", ["deps.lock"])
        self.push_commit("main", "deps.lock", "v1")

    def checked(self, head="origin/main"):
        cwd = self.runner.prepare_review(self.project_, "o/site", "kandu/end-check", head)
        return self.runner.run_test(self.project_, cwd, "run-test"), cwd

    def setups(self):
        return len(self.runs.read_text().splitlines()) if self.runs.exists() else 0

    def test_a_fresh_checkout_is_set_up_before_its_tests_and_the_mark_stays_out_of_its_files(self):
        self.project_.test = f"test -f {self.runs}"  # passes only if the setup ran first
        code, cwd = self.checked()
        self.assertEqual((code, self.setups()), (0, 1))
        self.assertEqual(git("status", "--porcelain", cwd=cwd), "")

    def test_it_runs_again_only_when_an_input_changes(self):
        self.checked()
        self.push_commit("main", "other.txt")
        self.checked()
        self.assertEqual(self.setups(), 1)
        self.push_commit("main", "deps.lock", "v2")
        self.checked()
        self.assertEqual(self.setups(), 2)

    def test_with_no_inputs_it_runs_on_each_new_head(self):
        self.project_.setup.inputs = []
        self.checked()
        self.checked()
        self.assertEqual(self.setups(), 1)
        self.push_commit("main", "other.txt")
        self.checked()
        self.assertEqual(self.setups(), 2)

    def test_a_failed_setup_fails_the_tests_and_runs_again_next_time(self):
        self.project_.test = f"echo tested >> {self.root / 'tested'}"
        self.project_.setup.command = f"echo ran >> {self.runs}; echo broken; false"
        code, _ = self.checked()
        self.assertNotEqual(code, 0)
        self.assertFalse((self.root / "tested").exists())
        self.assertIn("broken", (self.root / "logs" / "run-test.log").read_text())
        self.project_.setup.command = f"echo ran >> {self.runs}"
        self.assertEqual(self.checked()[0], 0)
        self.assertEqual(self.setups(), 2)


# Over SSH the remote script runs here: options and the host are skipped, the script goes to sh, as sshd would.
FAKE_SSH = """#!/bin/sh
while [ "$1" = -o ]; do shift 2; done
shift
exec sh -c "$1"
"""
SEE_TMP = 'echo "$TMPDIR" > "$HOME/seen.tmpdir"; echo hi > "$TMPDIR/left.txt"'


class RunFolderTest(GitCase):
    """A run temporary folder (#120): each scoped run on a machine with the option on has its own, removed at its end."""

    def setUp(self):
        super().setUp()
        self.base = self.home / "run-tmp"
        self.system_tmp = self.root / "system-tmp"
        self.system_tmp.mkdir()
        ssh_bin = self.root / "ssh-bin"
        ssh_bin.mkdir()
        install(ssh_bin / "ssh", FAKE_SSH)
        env = mock.patch.dict(os.environ, {"TMPDIR": str(self.system_tmp), "PATH": f"{ssh_bin}:{os.environ['PATH']}"})
        env.start()
        self.addCleanup(env.stop)

    def use(self, reach="local", on=True):
        self.machine = Machine("box", reach, True, run_tmp=on, run_tmp_dir="~/run-tmp" if on else None)
        self.runner = Runner(Config(settings=mock.Mock(machines=[self.machine]), projects=[]), self.root / "logs")

    def seen(self):
        return (self.home / "seen.tmpdir").read_text().strip()

    def left(self):
        return sorted(p.name for p in self.base.iterdir()) if self.base.exists() else []

    def assert_had_its_own_folder_now_gone(self):
        seen = Path(self.seen())
        self.assertEqual(seen.parent, self.base)
        self.assertTrue(seen.name.startswith("kandu-"))
        self.assertFalse(seen.exists())
        self.assertEqual(self.left(), [])

    def test_off_a_command_keeps_the_system_temporary_folder(self):
        for reach in ("local", "ssh box"):
            self.use(reach, on=False)
            self.assertEqual(self.runner.run_command(self.project(), str(self.home), SEE_TMP, "run-off"), 0)
            self.assertEqual(self.seen(), str(self.system_tmp))
            self.assertFalse(self.base.exists())

    def test_off_the_script_is_as_before(self):
        self.use(on=False)
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run, \
                mock.patch("kandu.runner.scope_name", return_value="kandu-u"):
            self.runner.run_command(self.project(), "/w", "make test", "run-off")
        argv = run.call_args_list[0].args[0]
        self.assertEqual(argv, Host(self.machine).argv(Host.scoped(["sh", "-c", "make test"], "kandu-u"), "/w"))
        self.assertNotIn("TMPDIR", " ".join(argv))

    def test_on_a_test_has_its_own_folder_removed_when_it_ends(self):
        for reach in ("local", "ssh box"):
            with self.subTest(reach=reach):
                self.use(reach)
                self.assertEqual(self.runner.run_command(self.project(), str(self.home), SEE_TMP, "run-on"), 0)
                self.assert_had_its_own_folder_now_gone()

    def test_each_run_has_a_fresh_folder(self):
        self.use()
        self.runner.run_command(self.project(), str(self.home), SEE_TMP, "run-a")
        first = self.seen()
        self.runner.run_command(self.project(), str(self.home), SEE_TMP, "run-a")
        self.assertNotEqual(self.seen(), first)

    def test_a_failing_command_loses_its_folder_too(self):
        for reach in ("local", "ssh box"):
            with self.subTest(reach=reach):
                self.use(reach)
                self.assertEqual(self.runner.run_command(self.project(), str(self.home), SEE_TMP + "; exit 3", "run-f"), 3)
                self.assert_had_its_own_folder_now_gone()

    def test_a_command_that_times_out_loses_its_folder(self):
        self.use()
        with mock.patch("kandu.runner.COMMAND_TIMEOUT_SECONDS", 2), self.assertRaises(RunnerError):
            self.runner.run_command(self.project(), str(self.home), SEE_TMP + "; exec sleep 30", "run-t")
        self.assert_had_its_own_folder_now_gone()

    def test_a_setup_has_its_own_folder_too(self):
        self.use()
        project = self.project()
        cwd = self.runner.prepare_worktree(project, "o/site", "kandu/1-x", "main")
        project.setup = Setup(command=SEE_TMP.replace("seen.tmpdir", "setup.tmpdir"))
        project.test = SEE_TMP
        self.assertEqual(self.runner.run_test(project, cwd, "run-s"), 0)
        setup = Path((self.home / "setup.tmpdir").read_text().strip())
        self.assertEqual(setup.parent, self.base)
        self.assertNotEqual(str(setup), self.seen())
        self.assert_had_its_own_folder_now_gone()

    def test_a_deploy_gets_no_run_folder(self):
        self.use()
        self.assertEqual(self.runner.run_command(self.project(), str(self.home), SEE_TMP, "run-d", scoped=False), 0)
        self.assertEqual(self.seen(), str(self.system_tmp))
        self.assertEqual(self.left(), [])

    def claude_sees(self):
        install(self.home / ".local" / "bin" / "claude",
                '#!/bin/sh\necho "$TMPDIR $CLAUDE_CODE_TMPDIR" > "$HOME/seen.tmpdir"\n' + FAKE_CLAUDE.split("\n", 1)[1])

    def agent(self, should_stop=lambda: False):
        return self.runner.run_agent("box", str(self.home), "p", ["Read"], ["Edit"], "r-1", should_stop)

    def test_on_an_agent_run_has_its_own_folder_for_both_variables(self):
        self.claude_sees()
        for reach in ("local", "ssh box"):
            with self.subTest(reach=reach):
                self.use(reach)
                self.assertFalse(self.agent().is_error)
                tmpdir, claude_tmpdir = self.seen().split()
                self.assertEqual(tmpdir, claude_tmpdir)
                self.assertEqual(Path(tmpdir).parent, self.base)
                self.assertFalse(Path(tmpdir).exists())
                self.assertEqual(self.left(), [])

    def test_off_an_agent_run_keeps_the_system_temporary_folder(self):
        self.claude_sees()
        self.use(on=False)
        self.agent()
        self.assertEqual(self.seen(), str(self.system_tmp))

    def test_a_failed_agent_run_loses_its_folder(self):
        install(self.home / ".local" / "bin" / "claude", '#!/bin/sh\necho "$TMPDIR" > "$HOME/seen.tmpdir"\nexit 1\n')
        self.use()
        self.assertTrue(self.agent().is_error)
        self.assert_had_its_own_folder_now_gone()

    def test_a_stopped_agent_run_loses_its_folder(self):
        install(self.home / ".local" / "bin" / "claude",
                '#!/bin/sh\necho "$TMPDIR" > "$HOME/seen.tmpdir"\necho x > "$TMPDIR/f"\nexec sleep 60\n')
        for reach in ("local", "ssh box"):
            with self.subTest(reach=reach):
                self.use(reach)
                with mock.patch("kandu.runner.POLL_SECONDS", 0.01), mock.patch("kandu.runner.STOP_GRACE_SECONDS", 0.5):
                    result = self.agent(should_stop=lambda: (self.home / "seen.tmpdir").exists())
                self.assertTrue(result.stopped)
                self.assert_had_its_own_folder_now_gone()
                (self.home / "seen.tmpdir").unlink()

    def old_folder(self, name, owner=None, days=2):
        folder = self.base / name
        folder.mkdir(parents=True)
        (folder / "junk").write_text("x")
        if owner is not None:
            (folder / ".kandu-run").write_text(f"{owner}\n")
        then = time.time() - days * 86400
        os.utime(folder, (then, then))
        return folder

    def test_a_stale_folder_no_run_owns_goes_before_the_next_run(self):
        self.use()
        gone = subprocess.Popen(["true"])
        gone.wait()
        orphan = self.old_folder("kandu-left-1")
        dead_owner = self.old_folder("kandu-left-2", owner=gone.pid)
        running = self.old_folder("kandu-running", owner=os.getpid())
        recent = self.old_folder("kandu-recent", days=0.5)
        other = self.old_folder("not-kandus")
        self.assertEqual(self.runner.run_command(self.project(), str(self.home), "true", "run-n"), 0)
        self.assertFalse(orphan.exists())
        self.assertFalse(dead_owner.exists())
        self.assertTrue(running.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(other.exists())

    def test_over_ssh_the_stale_folders_go_on_that_machine_too(self):
        self.use("ssh box")
        orphan = self.old_folder("kandu-left-1")
        self.assertEqual(self.runner.run_command(self.project(), str(self.home), "true", "run-n"), 0)
        self.assertFalse(orphan.exists())


if __name__ == "__main__":
    unittest.main()
