import json
import shlex
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

from kandu import agents, ghmeter, review
from kandu.config import Config, Machine, Project, Setup
from kandu.runner import (KEPT_CHECKED_OUT, KEPT_NOT_WORKTREE, KEPT_UNCOMMITTED, KEPT_UNPUSHED, REMOVED, Host, LocalWork, Runner,
                          RunnerError, SCOPE_PROBE, Unreachable, child_env, repo_path)

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
# What `codex exec --json` prints, as Codex 0.154 does: a thread, steps, its messages and the turn's usage.
CODEX_EVENTS = [
    {"type": "thread.started", "thread_id": "t-1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "Looking first."}},
    {"type": "item.started", "item": {"id": "item_1", "type": "command_execution", "command": "ls",
                                      "aggregated_output": "", "exit_code": None, "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "item_1", "type": "command_execution", "command": "ls",
                                        "aggregated_output": "a.txt\n", "exit_code": 0, "status": "completed"}},
    {"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": "the reply"}},
    {"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 600,
                                         "cache_write_input_tokens": 50, "output_tokens": 30,
                                         "reasoning_output_tokens": 10}},
]
# Keeps what it was given, then prints the events the test left in `$HOME/codex.events`.
FAKE_CODEX = """#!/bin/sh
pwd > "$HOME/codex.cwd"
printf '%s\\n' "$@" > "$HOME/codex.args"
cat > "$HOME/codex.stdin"
for arg in "$@"; do [ "$previous" = --output-schema ] && cp "$arg" "$HOME/codex.schema"; previous=$arg; done
echo '2026-11-17T08:39:01Z ERROR a line of its own on stderr'
cat "$HOME/codex.events"
"""
# Runs the script it was given on this machine, as the far machine would.
FAKE_SSH = """#!/bin/sh
while [ "$1" = -o ]; do shift 2; done
echo "$1" >> "$HOME/ssh.targets"
shift
exec sh -c "$1"
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
        (Path(scratch) / name).parent.mkdir(parents=True, exist_ok=True)
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
        found = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.split()
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


if __name__ == "__main__":
    unittest.main()


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


class ChangedPathsTest(GitCase):
    def test_the_paths_a_branch_changed_since_it_left_the_base(self):
        self.push_commit("kandu/1-a", "migrations/001.sql")
        self.push_commit("main", "later-on-main.txt")  # the base moved on: not the branch's change
        head = self.push_commit("kandu/1-a", "src/a.py")
        cwd = self.runner.prepare_review(self.project(), "o/site", "kandu/1-a", head)
        self.assertEqual(sorted(self.runner.changed_paths(self.project(), "o/site", cwd, "main", head)),
                         ["migrations/001.sql", "src/a.py"])

    def test_a_file_moved_out_of_a_folder_counts_on_both_sides(self):
        self.push_commit("main", "migrations/001.sql")
        scratch = tempfile.mkdtemp(dir=self.root)
        git("clone", str(self.origin), scratch)
        git("checkout", "-b", "kandu/1-a", cwd=scratch)
        git("mv", "migrations/001.sql", "kept.sql", cwd=scratch)
        git("commit", "-m", "move", cwd=scratch)
        git("push", "origin", "kandu/1-a", cwd=scratch)
        head = git("rev-parse", "HEAD", cwd=scratch)
        cwd = self.runner.prepare_review(self.project(), "o/site", "kandu/1-a", head)
        self.assertEqual(sorted(self.runner.changed_paths(self.project(), "o/site", cwd, "main", head)),
                         ["kept.sql", "migrations/001.sql"])


class CodexLoginTest(GitCase):
    def test_codex_logged_in_on_a_machine(self):
        install(self.home / ".local" / "bin" / "codex", "#!/bin/sh\n[ \"$1 $2\" = \"login status\" ]\n")
        self.assertIsNone(self.runner.codex_login("box"))

    def test_codex_logged_out_on_a_machine(self):
        install(self.home / ".local" / "bin" / "codex", "#!/bin/sh\necho 'Not logged in' >&2\nexit 1\n")
        self.assertEqual(self.runner.codex_login("box"), "not logged in; run: codex login")

    def test_codex_missing_on_a_machine(self):
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            self.assertEqual(self.runner.codex_login("box"), "not installed")

    def test_a_machine_that_does_not_answer_is_not_a_missing_codex(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        install(bin_dir / "ssh", "#!/bin/sh\nexit 255\n")
        far = Machine("far", "ssh far.example", False)
        runner = Runner(Config(settings=mock.Mock(machines=[far]), projects=[]), self.root / "logs")
        with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"}):
            with self.assertRaises(Unreachable):
                runner.codex_login("far")


    def test_a_codex_login_status_that_hangs_is_no_answer(self):
        install(self.home / ".local" / "bin" / "codex", "#!/bin/sh\nexec sleep 30\n")
        with mock.patch("kandu.runner.CODEX_LOGIN_TIMEOUT", 0.5):
            self.assertEqual(self.runner.codex_login("box"), "codex login status did not answer")

class ClaudeCommandTest(GitCase):
    def test_a_caller_that_names_no_agent_cli_runs_claude_with_todays_command_line(self):
        self.runner.run_agent("box", str(self.home), "p", ["Read", "Bash(npm test *)"], ["Edit"], "r-1", lambda: False,
                              "m-1", schema={"type": "object"}, resume="s-1")
        self.assertEqual((self.home / "claude.args").read_text().splitlines(),
                         ["-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "dontAsk",
                          "--allowedTools", "Read", "Bash(npm test *)", "--setting-sources", "user", "--model", "m-1",
                          "--resume", "s-1", "--json-schema", '{"type": "object"}', "--disallowedTools", "Edit"])
        self.assertFalse((self.home / "codex.args").exists())


class CodexTest(GitCase):
    """`codex` is a stub that prints Codex's JSON events, as `claude` is in the tests above."""

    def setUp(self):
        super().setUp()
        install(self.home / ".local" / "bin" / "codex", FAKE_CODEX)
        self.events(CODEX_EVENTS)

    def events(self, events):
        (self.home / "codex.events").write_text("".join(json.dumps(e) + "\n" for e in events))

    def codex(self, allowed=("Read", "Bash(npm test *)"), machine="box", cwd=None, **options):
        return self.runner.run_agent(machine, cwd or str(self.home), options.pop("prompt", "the prompt"),
                                     None if allowed is None else list(allowed), ["Edit"], "r-1",
                                     options.pop("should_stop", lambda: False), options.pop("model", None),
                                     cli="codex", **options)

    def args(self):
        return (self.home / "codex.args").read_text().splitlines()

    def test_the_prompt_reaches_codex_and_its_last_message_is_the_reply(self):
        result = self.codex()
        self.assertEqual(self.args()[:2], ["exec", "--json"])
        self.assertEqual(self.args()[-1], "-")  # the prompt comes on stdin, whatever its size
        self.assertEqual((self.home / "codex.stdin").read_text(), "the prompt")
        self.assertEqual((self.home / "codex.cwd").read_text().strip(), str(self.home))
        self.assertEqual((result.text, result.is_error, result.stopped, result.session_id),
                         ("the reply", False, False, "t-1"))
        self.assertEqual(result.cli, "codex")
        self.assertFalse((self.home / "claude.args").exists())

    def test_a_model_is_passed_only_when_one_is_set(self):
        self.codex()
        self.assertNotIn("--model", self.args())
        self.codex(model="gpt-5.5-codex")
        self.assertEqual(self.args()[self.args().index("--model") + 1], "gpt-5.5-codex")

    def test_the_repos_own_agent_files_are_not_loaded(self):
        self.codex()
        self.assertEqual(self.args()[self.args().index("-c") + 1], "project_doc_max_bytes=0")

    def test_with_no_allow_list_the_run_may_do_everything(self):
        self.codex(allowed=None)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", self.args())
        self.assertNotIn("--sandbox", self.args())

    def test_a_run_that_may_change_no_file_is_read_only(self):
        self.codex(allowed=["Read", "Grep", "Bash(git diff *)"])
        self.assertEqual(self.args()[self.args().index("--sandbox") + 1], "read-only")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", self.args())

    def test_a_run_that_may_edit_writes_its_checkout_and_commits_and_pushes(self):
        worktree = self.runner.prepare_worktree(self.project(), "o/site", "kandu/1-a", "main")
        self.codex(allowed=["Read", "Edit", "Bash(git commit *)"], cwd=worktree)
        args = self.args()
        self.assertEqual(args[args.index("--sandbox") + 1], "workspace-write")
        self.assertIn("sandbox_workspace_write.network_access=true", args)
        # A worktree's commits go to the copy's git folder, outside the checkout.
        self.assertEqual(args[args.index("--add-dir") + 1], str(self.copy / ".git"))

    def test_the_schema_reaches_codex_in_the_form_it_holds_a_reply_to(self):
        schema = {"type": "object", "required": ["status"], "properties": {
            "status": {"enum": ["pushed", "blocked"]}, "reason": {"type": "string"},
            "items": {"type": "array", "items": {"type": "object", "required": ["n"], "properties": {
                "n": {"type": "integer"}, "why": {"type": ["string", "null"]}}}}}}
        tmp = self.root / "tmp"
        tmp.mkdir()
        with mock.patch.dict(os.environ, {"TMPDIR": str(tmp)}):
            self.codex(schema=schema)
        given = json.loads((self.home / "codex.schema").read_text())
        # OpenAI's structured outputs hold a reply only to a schema where every object names every key as
        # required and allows no other: an optional key may be null instead.
        self.assertEqual(given["required"], ["status", "reason", "items"])
        self.assertIs(given["additionalProperties"], False)
        self.assertEqual(given["properties"]["status"], schema["properties"]["status"])
        self.assertIn({"type": "null"}, given["properties"]["reason"]["anyOf"])
        item = given["properties"]["items"]["anyOf"][0]["items"]
        self.assertEqual((item["required"], item["additionalProperties"]), (["n", "why"], False))
        self.assertIn({"type": "null"}, item["properties"]["why"]["anyOf"])
        self.assertEqual(list(tmp.iterdir()), [])  # it does not stay behind

    def test_no_schema_no_schema_file(self):
        self.codex()
        self.assertNotIn("--output-schema", self.args())

    def reply(self, obj):
        text = obj if isinstance(obj, str) else json.dumps(obj)
        events = [dict(e) for e in CODEX_EVENTS]
        events[5] = {"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": text}}
        self.events(events)

    def test_a_worker_reply_with_optional_fields_left_out_reads_as_from_claude(self):
        self.reply({"status": "pushed", "pr_title": "T", "pr_body": "B", "question": None, "reason": None})
        result = self.codex(schema=agents.WORKER_SCHEMA)
        self.assertEqual(agents.parse_first_reply(result.text), {"status": "pushed", "pr_title": "T", "pr_body": "B"})
        self.reply({"status": "blocked", "pr_title": None, "pr_body": None, "question": None, "reason": "no access"})
        result = self.codex(schema=agents.WORKER_SCHEMA)
        self.assertEqual(agents.parse_worker_reply(result.text), {"status": "blocked", "reason": "no access"})

    def test_a_review_with_optional_fields_left_out_reads_as_from_claude(self):
        finding = {"n": 1, "path": "a.py", "line": None, "blocker": True, "criterion": None, "body": "b"}
        self.reply({"head_sha": "abc", "summary": "s", "criteria": [{"criterion": None, "result": "met"}],
                    "earlier": [], "findings": [finding], "checked_ok": [], "door": "two-way", "wait_for_user": [],
                    "blast_radius": "r", "brief": "b", "user_brief": "u"})
        report = review.parse_report(self.codex(schema=review.REPORT_SCHEMA).text)
        self.assertEqual(review.validate_report(report, []), [])
        self.assertEqual(report["criteria"], [{"result": "met"}])
        self.assertEqual(report["findings"], [finding])  # a null the schema itself allows stays

    def test_a_reply_that_is_not_json_comes_back_as_it_is(self):
        self.reply("All done!")
        self.assertEqual(self.codex(schema=agents.WORKER_SCHEMA).text, "All done!")

    def test_a_broken_reply_is_asked_for_again_in_the_same_session(self):
        self.codex(prompt="again", resume="t-1", schema=agents.WORKER_SCHEMA)
        args = self.args()
        self.assertEqual(args[args.index("resume"):], ["resume", "t-1", "-"])
        self.assertLess(args.index("--output-schema"), args.index("resume"))
        self.assertLess(args.index("--sandbox"), args.index("resume"))
        self.assertEqual((self.home / "codex.stdin").read_text(), "again")

    def test_tokens_land_in_kandus_fields_with_cached_input_as_cache_reads(self):
        result = self.codex()
        self.assertEqual(result.models, {"codex": {"input": 400, "output": 30, "cache_read": 600,
                                                   "cache_creation": 50}})
        self.assertEqual(self.codex(model="gpt-5.5").models["gpt-5.5"]["cache_read"], 600)

    def test_the_runs_duration_is_measured(self):
        install(self.home / ".local" / "bin" / "codex", FAKE_CODEX.replace("cat \"$HOME/codex.events\"",
                                                                           "sleep 0.3; cat \"$HOME/codex.events\""))
        self.assertGreaterEqual(self.codex().duration_ms, 300)

    def test_a_failed_turn_is_an_error(self):
        self.events(CODEX_EVENTS[:3] + [{"type": "error", "message": "stream disconnected"},
                                        {"type": "turn.failed", "error": {"message": "stream disconnected"}}])
        self.assertTrue(self.codex().is_error)

    def test_a_run_with_no_finished_turn_is_an_error(self):
        self.events(CODEX_EVENTS[:6])
        self.assertTrue(self.codex().is_error)

    def test_a_failing_exit_is_an_error_even_with_a_turn(self):
        install(self.home / ".local" / "bin" / "codex", FAKE_CODEX + "exit 1\n")
        self.assertTrue(self.codex().is_error)

    def test_its_events_are_logged_for_the_dashboard(self):
        self.codex()
        log = (self.root / "logs" / "r-1.jsonl").read_text()
        self.assertIn('"command_execution"', log)
        self.assertEqual((self.root / "logs" / "r-1.jsonl").stat().st_mode & 0o777, 0o600)

    def test_a_token_task_gets_its_tokens(self):
        install(self.home / ".local" / "bin" / "codex", "#!/bin/sh\nenv > \"$HOME/codex.env\"\n" + FAKE_CODEX.split("\n", 1)[1])
        (self.home / "vars").write_text("A_KEY=one\nOTHER=hidden\n")
        self.assertFalse(self.codex(tokens=(str(self.home / "vars"), ["A_KEY"])).is_error)
        seen = (self.home / "codex.env").read_text().splitlines()
        self.assertIn("A_KEY=one", seen)
        self.assertNotIn("OTHER=hidden", seen)

    def test_stop_ends_a_running_codex(self):
        install(self.home / ".local" / "bin" / "codex", "#!/bin/sh\nexec sleep 60\n")
        started = time.monotonic()
        with mock.patch("kandu.runner.POLL_SECONDS", 0.01):
            result = self.codex(should_stop=lambda: True)
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(result.stopped)

    @unittest.skipUnless(subprocess.run(["sh", "-c", SCOPE_PROBE]).returncode == 0, "needs a user systemd")
    def test_what_codex_leaves_running_ends_with_it(self):
        marker = f"sleep 300.{os.getpid()}3"
        install(self.home / ".local" / "bin" / "codex",
                f"#!/bin/sh\nsetsid {marker} </dev/null >/dev/null 2>&1 &\n" + FAKE_CODEX.split("\n", 1)[1])
        self.assertFalse(self.codex().is_error)
        found = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.split()
        self.assertEqual(found, [])

    def test_on_a_remote_machine(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        install(bin_dir / "ssh", FAKE_SSH)
        far = Machine("far", "ssh far.example", False)
        runner = Runner(Config(settings=mock.Mock(machines=[far]), projects=[]), self.root / "logs")
        with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"}):
            result = runner.run_agent("far", str(self.home), "the prompt", ["Read"], [], "r-1", lambda: False,
                                      "gpt-5.5", schema=agents.CHECK_SCHEMA, cli="codex")
        self.assertEqual(set((self.home / "ssh.targets").read_text().split()), {"far.example"})
        self.assertEqual((result.text, result.is_error), ("the reply", False))
        self.assertEqual(self.args()[self.args().index("--model") + 1], "gpt-5.5")
        self.assertEqual(json.loads((self.home / "codex.schema").read_text())["required"], ["result", "summary"])
        self.assertEqual((self.home / "codex.stdin").read_text(), "the prompt")

    def run_held(self, script, denied=agents.KANDU_ONLY + ["Bash(git push --force*)"]):
        """A Codex run with the worker's deny list, whose `codex` runs `script` as one of its shell commands. As
        Codex does, it runs the command in a login shell unless it was told `allow_login_shell=false`."""
        login = ('case " $* " in *" allow_login_shell=false "*) shell="bash -c" ;; *) shell="bash -lc" ;; esac; '
                 f'$shell {shlex.quote(script)}')
        install(self.home / ".local" / "bin" / "codex",
                FAKE_CODEX.replace('cat "$HOME/codex.events"', f'{{ {login}; }} > "$HOME/held.out" 2>&1; '
                                   'echo "exit $?" >> "$HOME/held.out"; cat "$HOME/codex.events"'))
        tmp = self.root / "tmp"
        tmp.mkdir(exist_ok=True)
        with mock.patch.dict(os.environ, {"TMPDIR": str(tmp)}):
            result = self.runner.run_agent("box", str(self.home), "p", ["Read", "Edit"], denied, "r-1",
                                           lambda: False, cli="codex")
        self.assertFalse(result.is_error)
        self.assertEqual([p for p in tmp.iterdir() if p.name.startswith("kandu-")], [])  # nothing stays behind
        return (self.home / "held.out").read_text()

    def test_a_codex_run_cannot_run_what_kandu_alone_runs(self):
        install(self.home / ".local" / "bin" / "gh", "#!/bin/sh\necho \"gh ran: $*\"\n")
        for command in ("git merge main", "git pull", "git rebase main", "git pull --rebase origin main",
                        "gh pr create --fill", "gh pr edit 7 --body x", "gh pr merge 7"):
            out = self.run_held(command)
            self.assertIn("denied", out, command)
            self.assertNotIn("ran:", out, command)
            self.assertTrue(out.rstrip().endswith("exit 1"), command)

    def test_a_login_profile_cannot_put_another_program_ahead_of_the_hold(self):
        # As Ubuntu's stock ~/.profile does with ~/bin and ~/.local/bin, where a gh installed without root lives.
        (self.home / ".profile").write_text('PATH="$HOME/bin:$PATH"\n')
        (self.home / "bin").mkdir()
        install(self.home / "bin" / "gh", "#!/bin/sh\necho \"gh ran: $*\"\n")
        out = self.run_held("gh pr merge 7")
        self.assertIn("denied", out)
        self.assertNotIn("ran:", out)

    def test_a_codex_run_cannot_run_what_its_deny_list_names(self):
        self.assertIn("denied", self.run_held("git push --force origin kandu/1-a"))

    def test_a_codex_run_runs_every_other_command_as_it_is(self):
        install(self.home / ".local" / "bin" / "gh", "#!/bin/sh\necho \"gh ran: $*\"\n")
        self.assertIn("gh ran: pr comment 7 --body ok", self.run_held("gh pr comment 7 --body ok"))
        self.assertIn("gh ran: pr list", self.run_held("gh pr list"))
        out = self.run_held("git --version && git mergetool --help >/dev/null 2>&1; git status --short")
        self.assertIn("git version", out)
        self.assertNotIn("denied", out)

    def test_a_codex_run_with_nothing_denied_runs_as_before(self):
        self.assertIn("git version", self.run_held("git --version", denied=["Read(**/.env)"]))

    def test_an_unknown_agent_cli_is_refused(self):
        with self.assertRaises(RunnerError):
            self.runner.run_agent("box", str(self.home), "p", None, [], "r-1", lambda: False, cli="gemini")
