import json
import os
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from kandu.config import Config, Machine, Project
from kandu.runner import Host, Runner, RunnerError, repo_path

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

    def test_tools_in_the_users_home_are_found(self):
        self.assertIn('export PATH="$HOME/.local/bin:$PATH"', Host(Machine("a", "local", True)).argv(["true"])[2])


class RepoPathTest(unittest.TestCase):
    def project(self, repos):
        return mock.Mock(path="~/w/site", repos=repos)

    def test_one_repo_is_the_path_itself(self):
        self.assertEqual(repo_path(self.project(["o/a"]), "o/a"), "~/w/site")

    def test_several_repos_have_a_folder_each(self):
        self.assertEqual(repo_path(self.project(["o/a", "o/b"]), "o/b"), "~/w/site/b")


class RunnerTest(unittest.TestCase):
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

    def push_commit(self, branch, name):
        scratch = tempfile.mkdtemp(dir=self.root)
        git("clone", str(self.origin), scratch)
        pushed = branch in git("branch", "-r", cwd=scratch)
        git("checkout", "-B", branch, *([f"origin/{branch}"] if pushed else []), cwd=scratch)
        (Path(scratch) / name).write_text("x")
        git("add", ".", cwd=scratch)
        git("commit", "-m", "c", cwd=scratch)
        git("push", "origin", branch, cwd=scratch)
        return git("rev-parse", "HEAD", cwd=scratch)

    def project(self, run="clone"):
        return Project(name="site", family="f", repos=["o/site"], run=run, machine="box", path="~/work/site",
                       test="true", priority=1, max_parallel_workers=1, paused=False, daily_token_limit=1000)

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

    def agent(self, should_stop=lambda: False, prompt="the prompt"):
        return self.runner.run_agent("box", str(self.home), prompt, ["Read", "Bash(npm test *)"], ["Edit"], "r-1",
                                     should_stop)

    def test_agent_result_and_arguments(self):
        result = self.agent()
        self.assertEqual((result.text, result.is_error, result.stopped), ("the reply", False, False))
        self.assertEqual(result.models["model-a"]["output"], 2)
        args = (self.home / "claude.args").read_text().splitlines()
        self.assertIn("Bash(npm test *)", args)
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(args[args.index("--output-format") + 1], "stream-json")
        self.assertNotIn("--dangerously-skip-permissions", args)
        self.assertEqual((self.home / "claude.stdin").read_text(), "the prompt")
        self.assertEqual((self.home / "claude.cwd").read_text().strip(), str(self.home))

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

    def test_stop_ends_a_running_agent(self):
        install(self.home / ".local" / "bin" / "claude", "#!/bin/sh\nexec sleep 60\n")
        started = time.monotonic()
        with mock.patch("kandu.runner.POLL_SECONDS", 0.01):
            result = self.agent(should_stop=lambda: True)
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(result.stopped)


if __name__ == "__main__":
    unittest.main()
