import dataclasses
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kandu import add, remove
from kandu.config import load_config
from kandu.dashboard import Api, dispatch
from kandu.github import GitHubError
from kandu.runner import Host
from kandu.state import StateStore

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "secret-token"
NODE = shutil.which("node")


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.email=k@example.com", "-c", "user.name=k", *args], cwd=cwd, check=True,
                   capture_output=True)


class FakeFlights:
    def __init__(self, store):
        self.store = store
        self.closed = []
        self.spawn = lambda work: work()

    def close(self, key):
        self.closed.append(key)
        self.store.forget_flight(key)
        return True


class StuckFlights(FakeFlights):
    """A session still opening: close() cannot end it, so its flight stays."""

    def close(self, key):
        self.closed.append(key)
        return False


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.local = self.root / "local"
        self.local.mkdir()
        shutil.copy(ROOT / "config.example" / "kandu.example.yaml", self.local / "kandu.yaml")
        # `website` runs on `server`, the machine Kandu runs on: its copy is a real git copy here.
        self.copy = self.root / "website"
        text = (ROOT / "config.example" / "projects.example.yaml").read_text(encoding="utf-8")
        (self.local / "projects.yaml").write_text(text.replace("~/kandu-work/website", str(self.copy))
                                                  .replace("run: clone ", "run: in-place "),
                                                  encoding="utf-8")
        self.copy.mkdir()
        git("init", "-q", "-b", "main", cwd=self.copy)
        (self.copy / "README.md").write_text("site\n")
        git("add", ".", cwd=self.copy)
        git("commit", "-q", "-m", "first", cwd=self.copy)
        self.worktrees = Path(f"{self.copy}.kandu-worktrees")
        for name in ("12-a-task", "12-a-task-review"):
            git("worktree", "add", "-q", "-b", f"kandu/{name}", str(self.worktrees / name), cwd=self.copy)
        self.store = StateStore(self.root / "state")
        self.flights = FakeFlights(self.store)
        self.github = mock.Mock()
        self.api = Api(load_config(self.local), self.store, self.github, self.root / "logs", TOKEN,
                       timer_on=lambda: True, local_dir=self.local, flights_=self.flights)
        self.before = self.registry()

    def registry(self):
        return (self.local / "projects.yaml").read_text(encoding="utf-8")

    def post(self, target):
        return dispatch(self.api, "POST", target, TOKEN)

    def remove(self, name="website", **kw):
        return remove.remove_project(name, self.store, self.flights, self.local, **kw)


class ConfigEntryTest(Base):
    def test_only_the_entry_goes_and_every_other_entry_and_comment_stays(self):
        self.remove()
        after = self.registry()
        config = load_config(self.local)  # the config check passes
        self.assertEqual([p.name for p in config.projects], ["internal-tool"])
        self.assertNotIn("name: website", after)
        self.assertNotIn("To let workers check a page", after)  # a comment inside the entry goes with it
        for line in self.before.splitlines():
            if line.strip().startswith("#") and not line.startswith("    "):
                self.assertIn(line, after)  # every comment outside it stays
        removed = self.before.split("  - name: website")[1].split("  # A project that must run")[0]
        self.assertEqual(after, self.before.replace("  - name: website" + removed, "\n"))

    def test_the_last_entry_leaves_an_empty_list_that_passes_the_check(self):
        (self.local / "projects.yaml").write_text(self.before.replace("machine: laptop ", "machine: server "),
                                                  encoding="utf-8")
        self.remove()
        remove.remove_project("internal-tool", self.store, self.flights, self.local)
        self.assertEqual(load_config(self.local).projects, [])
        self.assertIn("# A project that is not in local/projects.yaml is never touched.", self.registry())

    def test_an_unknown_project_fails_and_leaves_the_config_unchanged(self):
        with self.assertRaisesRegex(remove.RemoveError, "no project named 'nobody'"):
            self.remove("nobody")
        self.assertEqual(self.registry(), self.before)


class WorkTest(Base):
    def test_worktrees_go_and_the_copy_stays(self):
        result = self.remove()
        self.assertEqual(sorted(result["worktrees"]), sorted(str(self.worktrees / n) for n in ("12-a-task", "12-a-task-review")))
        self.assertFalse(self.worktrees.exists())
        self.assertEqual((self.copy / "README.md").read_text(), "site\n")
        listed = subprocess.run(["git", "worktree", "list"], cwd=self.copy, capture_output=True, text=True).stdout
        self.assertEqual(len(listed.splitlines()), 1)

    def test_a_running_worker_is_stopped_first(self):
        self.store.write_current({"workers": [{"task": "acme-org/website#12", "project": "website", "run_id": "r-1"},
                                              {"task": "acme-org/internal-tool#3", "project": "internal-tool",
                                               "run_id": "r-2"}]})
        seen = []
        def supervisor_stops_it(_):
            seen.append(self.store.worker_stop_file("r-1").exists())
            self.store.write_current({"workers": [{"task": "acme-org/internal-tool#3", "project": "internal-tool",
                                                   "run_id": "r-2"}]})
            self.assertEqual(self.registry(), self.before)  # still in the config while it runs
        self.remove(sleep=supervisor_stops_it)
        self.assertEqual(seen, [True])
        self.assertFalse(self.store.worker_stop_file("r-2").exists())  # another project's work runs on
        self.assertNotIn("name: website", self.registry())

    def test_a_free_flight_on_the_project_is_closed(self):
        self.store.write_flight({"key": "t-1", "kind": "task", "started": "1", "task": "acme-org/website#12", "project": "website"})
        self.store.write_flight({"key": "t-2", "kind": "task", "started": "2", "task": "acme-org/internal-tool#3",
                                 "project": "internal-tool"})
        self.remove()
        self.assertEqual(self.flights.closed, ["t-1"])

    def test_work_that_cannot_be_stopped_fails_and_leaves_everything(self):
        self.store.write_current({"workers": [{"task": "acme-org/website#12", "project": "website", "run_id": "r-1"}]})
        clock = iter(range(0, 1000, 10))
        with mock.patch("kandu.remove.time.monotonic", lambda: next(clock)):
            with self.assertRaisesRegex(remove.RemoveError, "could not be stopped"):
                remove.stop_work(self.store, self.flights, "website", wait=30, sleep=lambda _: None)
            with self.assertRaisesRegex(remove.RemoveError, "could not be stopped"):
                self.remove(wait=30, sleep=lambda _: None)
        self.assertEqual(self.registry(), self.before)
        self.assertTrue((self.worktrees / "12-a-task").exists())
        self.assertNotIn("website", self.store.paused_projects())


class FailureTest(Base):
    """Each way removing can fail leaves projects.yaml byte for byte as it was and the project not paused."""

    def assertNothingChanged(self):
        self.assertEqual(self.registry(), self.before)
        self.assertNotIn("website", self.store.paused_projects())
        self.assertTrue((self.worktrees / "12-a-task").exists())
        self.assertIsNone(self.store.last_event("project_removed"))

    def stuck_flight(self):
        self.flights = StuckFlights(self.store)
        self.api.flights = self.flights
        self.store.write_flight({"key": "t-1", "kind": "task", "started": "1", "task": "acme-org/website#12",
                                 "project": "website"})

    def test_a_session_that_stays_after_close_fails(self):
        self.stuck_flight()
        with self.assertRaisesRegex(remove.RemoveError, "acme-org/website#12 could not be closed"):
            self.remove()
        self.assertEqual(self.flights.closed, ["t-1"])
        self.assertNothingChanged()

    def test_a_worktree_that_cannot_be_removed_fails(self):
        real = Host.run
        def refuse_remove(host, command, cwd=None, **kw):
            if command[:3] == ["git", "worktree", "remove"]:
                return subprocess.CompletedProcess(command, 1, "", "fatal: cannot remove")
            return real(host, command, cwd, **kw)
        with mock.patch.object(Host, "run", refuse_remove):
            with self.assertRaisesRegex(remove.RemoveError, "worktrees for website could not be removed on server"):
                self.remove()
        self.assertNothingChanged()

    def test_a_project_already_paused_stays_paused_after_a_failure(self):
        self.store.pause_project("website")
        self.stuck_flight()
        with self.assertRaises(remove.RemoveError):
            self.remove()
        self.assertEqual(self.registry(), self.before)
        self.assertIn("website", self.store.paused_projects())

    def test_a_machine_out_of_reach_fails_instead_of_skipping_its_worktrees(self):
        # internal-tool's copy has an absolute path on the laptop, and the laptop cannot be reached.
        kandu = (self.local / "kandu.yaml").read_text(encoding="utf-8")
        (self.local / "kandu.yaml").write_text(kandu.replace("reach: ssh laptop", "reach: ssh kandu-test.invalid"),
                                               encoding="utf-8")
        (self.local / "projects.yaml").write_text(self.before.replace("~/code/internal-tool", "/srv/internal-tool"),
                                                  encoding="utf-8")
        before = self.registry()
        with self.assertRaisesRegex(remove.RemoveError, "laptop cannot be reached"):
            remove.remove_project("internal-tool", self.store, self.flights, self.local)
        self.assertEqual(self.registry(), before)
        self.assertNotIn("internal-tool", self.store.paused_projects())

    def test_an_unknown_machine_fails(self):
        project = next(p for p in load_config(self.local).projects if p.name == "website")
        with self.assertRaisesRegex(remove.RemoveError, "unknown machine 'nowhere'"):
            remove.remove_worktrees(load_config(self.local), dataclasses.replace(project, machine="nowhere"))
        self.assertTrue((self.worktrees / "12-a-task").exists())

    def test_a_config_that_needs_the_entry_fails_before_anything_stops(self):
        # Another entry uses an anchor defined in this one: without it the file no longer loads.
        text = self.before.replace("    machine: server", "    machine: &where server", 1)
        text = text.replace("    machine: laptop ", "    machine: *where ", 1)
        (self.local / "projects.yaml").write_text(text, encoding="utf-8")
        self.before = self.registry()
        self.store.write_current({"workers": [{"task": "acme-org/website#12", "project": "website", "run_id": "r-1"}]})
        with self.assertRaisesRegex(remove.RemoveError, "would not pass its check without website"):
            self.remove(wait=0, sleep=lambda _: None)
        self.assertFalse(self.store.worker_stop_file("r-1").exists())  # its work was not stopped
        self.assertNothingChanged()

    def test_the_dashboard_answers_409_for_a_known_project_that_cannot_be_removed(self):
        self.stuck_flight()
        status, body = self.post("/api/v1/projects/website/remove")
        self.assertEqual(status, 409, body)
        self.assertIn("could not be closed", json.dumps(body))
        self.assertEqual(sorted(p.name for p in self.api.config.projects), ["internal-tool", "website"])
        self.assertNothingChanged()


class DashboardTest(Base):
    def test_the_button_removes_the_project_and_touches_no_board(self):
        status, body = self.post("/api/v1/projects/website/remove")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["removed"], "website")
        self.assertEqual([p.name for p in self.api.config.projects], ["internal-tool"])
        self.assertEqual(self.github.mock_calls, [])
        self.assertEqual(self.store.last_event("project_removed")["project"], "website")

    def test_an_unknown_project_answers_404_and_changes_nothing(self):
        status, body = self.post("/api/v1/projects/nobody/remove")
        self.assertEqual(status, 404)
        self.assertIn("no such project", json.dumps(body))
        self.assertEqual(self.registry(), self.before)

    def test_removing_needs_the_token(self):
        self.assertEqual(dispatch(self.api, "POST", "/api/v1/projects/website/remove", "wrong")[0], 403)
        self.assertEqual(self.registry(), self.before)


class PageTest(unittest.TestCase):
    STUBS = """
const t = key => key;
const fillIn = (key, values) => Object.entries(values).reduce((text, [k, v]) => text.replaceAll(`{${k}}`, v), t(key));
const calls = [], toasts = [];
let answers = [];
const post = {failed: null};
async function act(button, path) { calls.push(path); const answer = answers.shift(); post.failed = answer.ok ? null : answer.body; return answer.ok && answer.body; }
function toast(text) { toasts.push(text); }
function renderSettings() {}
function closeSettings() {}
async function refreshState() {}
"""

    def page(self):
        return (ROOT / "kandu" / "dashboard.html").read_text(encoding="utf-8")

    @unittest.skipUnless(NODE, "needs node to run the page's script")
    def test_the_button_asks_first_and_acts_on_the_second_click(self):
        page = self.page()
        begin = page.index("async function removeProject(")
        script = page[begin:page.index("\n}\n", begin) + 3]
        done = subprocess.run([NODE, "-e", self.STUBS + script + """
(async () => {
  const button = {dataset: {remove: "site"}, textContent: "remove_project"};
  answers = [{ok: false, body: {error: "no", memory_pr_failed: true}},
             {ok: true, body: {removed: "site", memory: {pr: null, why: "declined"}}}];
  await removeProject(button);
  const first = [calls.length, button.textContent];
  await removeProject(button);
  const failed = button.textContent;
  await removeProject(button);
  await removeProject(button);
  console.log(JSON.stringify({first, failed, calls, toasts}));
})();
"""], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout)
        self.assertEqual(result["first"], [0, "remove_sure"])  # the first click only asks
        self.assertEqual(result["failed"], "remove_without_pr")  # a PR that failed offers to go on without it
        self.assertEqual(result["calls"], ["/projects/site/remove", "/projects/site/remove?without_memory_pr=true"])
        self.assertEqual(result["toasts"], ["removed_msg memory_declined"])

    def test_each_projects_settings_pop_up_has_the_button_and_nothing_is_typed(self):
        page = self.page()
        render = page.split("function settingsBody(", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('data-remove="${esc(row.name)}"', render)
        self.assertNotIn("<input type=\"text", render)
        self.assertIn("closeSettings(name);", page.split("async function removeProject(", 1)[1].split("\n}\n", 1)[0])
        self.assertIn('if (hit("remove")) { removeProject(hit("remove")); return; }', page)

    def test_the_words_are_there_in_both_languages(self):
        page = self.page()
        for key in ("remove_project", "remove_project_hint", "remove_sure", "remove_without_pr", "removed_msg",
                    "memory_offered", "memory_public", "memory_declined", "memory_none"):
            self.assertEqual(page.count(f'"{key}": '), 2, key)


class DidbanTest(unittest.TestCase):
    def test_didban_removes_with_one_command_after_the_yes(self):
        text = (ROOT / "prompts" / "didban.md").read_text(encoding="utf-8")
        part = text.split("## Removing a project\n")[1].split("\n## ")[0]
        self.assertIn("Only on that yes, run `python -m kandu.remove PROJECT`", part)
        self.assertLess(part.index("ask for their yes"), part.index("python -m kandu.remove"))


class CommandTest(Base):
    def run_command(self, name):
        with mock.patch("kandu.remove.StateStore", lambda: self.store), \
                mock.patch("sys.argv", ["remove", name, "--local", str(self.local)]), \
                mock.patch("sys.stdout") as out, mock.patch("sys.stderr") as err:
            code = remove.main()
        return code, "".join(c.args[0] for c in out.write.call_args_list + err.write.call_args_list)

    def test_one_command_takes_the_project_out(self):
        code, said = self.run_command("website")
        self.assertEqual(code, 0, said)
        self.assertEqual([p.name for p in load_config(self.local).projects], ["internal-tool"])
        self.assertFalse(self.worktrees.exists())

    def test_an_unknown_project_says_so_and_changes_nothing(self):
        code, said = self.run_command("nobody")
        self.assertEqual(code, 1)
        self.assertIn("no project named 'nobody'", said)
        self.assertEqual(self.registry(), self.before)


MEMORY = "# website\n\n## Glossary\n- cart: the basket\n\n## Decisions\n- prices include tax\n"
PR_URL = "https://github.com/acme-org/website/pull/5"


class MemoryTest(Base):
    def setUp(self):
        super().setUp()
        self.memory = self.local / "memory" / "website.md"
        self.memory.parent.mkdir()
        self.memory.write_text(MEMORY, encoding="utf-8")
        self.archived = self.local / "memory" / "archive" / "website.md"
        self.github.repo_is_public.return_value = False
        self.github.offer_file_pr.return_value = PR_URL

    def remove(self, name="website", **kw):
        return remove.remove_project(name, self.store, self.flights, self.local, github=self.github, **kw)

    def test_the_memory_is_offered_as_a_pr_to_the_projects_repo_before_the_entry_goes(self):
        seen = []
        def offer(repo, branch, path, content, title, body):
            seen.append(self.registry() == self.before)
            return PR_URL
        self.github.offer_file_pr.side_effect = offer
        result = self.remove()
        self.assertEqual(seen, [True])
        args = self.github.offer_file_pr.call_args.args
        self.assertEqual(args[0], "acme-org/website")
        self.assertIn(MEMORY.strip(), args[3])
        self.assertEqual(args[2], remove.MEMORY_FILE)
        self.assertEqual(result["memory"]["pr"], PR_URL)
        self.assertNotIn("name: website", self.registry())
        self.github.merge_pr.assert_not_called()  # left for the owner to merge

    def test_the_memory_is_archived_not_deleted(self):
        self.remove()
        self.assertFalse(self.memory.exists())
        self.assertEqual(self.archived.read_text(encoding="utf-8"), MEMORY)

    def test_the_brief_is_offered_and_archived_with_the_memory(self):
        brief = self.local / "brief" / "website.md"
        brief.parent.mkdir()
        brief.write_text("# website\nA shop.\n", encoding="utf-8")
        self.github.create_board.return_value = 7
        result = self.remove()
        self.assertIn("A shop.", self.github.offer_file_pr.call_args.args[3])
        self.assertFalse(brief.exists())
        self.assertEqual((self.local / "brief" / "archive" / "website.md").read_text(encoding="utf-8"),
                         "# website\nA shop.\n")
        self.assertEqual(len(result["archived"]), 2)
        add.add_project({"machine": "server", "path": str(self.copy), "repo": "acme-org/website", "name": "website",
                         "test": "true", "run": "in-place", "deploy_task": "false", "test_task": "false"},
                        self.github, self.local)
        self.assertEqual(brief.read_text(encoding="utf-8"), "# website\nA shop.\n")

    def test_a_public_repo_gets_no_pr_and_the_user_is_told_why(self):
        self.github.repo_is_public.return_value = True
        result = self.remove()
        self.github.offer_file_pr.assert_not_called()
        self.assertIsNone(result["memory"]["pr"])
        self.assertEqual(result["memory"]["why"], "public")
        self.assertIn("public", result["memory"]["note"])
        self.assertEqual(self.archived.read_text(encoding="utf-8"), MEMORY)
        self.assertNotIn("name: website", self.registry())

    def test_a_pr_that_cannot_be_made_stops_the_removal_until_the_user_decides(self):
        self.github.offer_file_pr.side_effect = GitHubError("gh api: HTTP 403")
        with self.assertRaisesRegex(remove.RemoveError, "memory.*could not be offered.*HTTP 403"):
            self.remove()
        self.assertEqual(self.registry(), self.before)
        self.assertEqual(self.memory.read_text(encoding="utf-8"), MEMORY)
        self.assertFalse(self.archived.exists())
        self.assertTrue((self.worktrees / "12-a-task").exists())
        self.assertNotIn("website", self.store.paused_projects())

    def test_once_the_user_decides_it_is_removed_without_the_pr(self):
        result = self.remove(memory_pr=False)
        self.github.offer_file_pr.assert_not_called()
        self.assertIsNone(result["memory"]["pr"])
        self.assertEqual(self.archived.read_text(encoding="utf-8"), MEMORY)
        self.assertNotIn("name: website", self.registry())

    def test_the_board_issues_and_copy_are_not_touched(self):
        self.remove()
        self.assertEqual(sorted(c[0] for c in self.github.method_calls), ["offer_file_pr", "repo_is_public"])
        self.assertEqual((self.copy / "README.md").read_text(), "site\n")
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.copy, capture_output=True, text=True)
        self.assertEqual(status.stdout, "")

    def test_a_project_added_back_uses_its_archived_memory_again(self):
        self.remove()
        self.github.create_board.return_value = 7
        add.add_project({"machine": "server", "path": str(self.copy), "repo": "acme-org/website", "name": "website",
                         "test": "true", "run": "in-place", "deploy_task": "false", "test_task": "false"}, self.github, self.local)
        self.assertEqual(self.memory.read_text(encoding="utf-8"), MEMORY)
        self.assertFalse(self.archived.exists())

    def test_a_second_removal_keeps_the_older_archive_with_the_new_memory_after_it(self):
        older = "# website\n\n## Decisions\n- ship on Fridays\n"
        self.archived.parent.mkdir()
        self.archived.write_text(older, encoding="utf-8")
        self.remove()
        self.assertFalse(self.memory.exists())
        self.assertEqual(self.archived.read_text(encoding="utf-8"), older + "\n" + MEMORY)
        self.assertEqual(list(self.archived.parent.iterdir()), [self.archived])

    def test_a_project_added_back_with_new_memory_keeps_the_archive_first(self):
        self.remove()
        newer = "# website\n\n## Glossary\n- sku: stock unit\n"
        self.memory.write_text(newer, encoding="utf-8")
        self.github.create_board.return_value = 7
        add.add_project({"machine": "server", "path": str(self.copy), "repo": "acme-org/website", "name": "website",
                         "test": "true", "run": "in-place", "deploy_task": "false", "test_task": "false"}, self.github, self.local)
        self.assertEqual(self.memory.read_text(encoding="utf-8"), MEMORY + "\n" + newer)
        self.assertFalse(self.archived.exists())

    def test_the_dashboard_tells_about_the_pr_and_can_remove_without_it(self):
        status, body = self.post("/api/v1/projects/website/remove")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["memory"]["pr"], PR_URL)
        self.assertIn(PR_URL, body["memory"]["note"])

    def test_the_dashboard_refuses_when_the_pr_fails_and_removes_once_decided(self):
        self.github.offer_file_pr.side_effect = GitHubError("gh api: HTTP 403")
        status, body = self.post("/api/v1/projects/website/remove")
        self.assertEqual(status, 409)
        self.assertIn("HTTP 403", json.dumps(body))
        self.assertTrue(body["memory_pr_failed"])
        self.assertEqual(self.registry(), self.before)
        status, body = self.post("/api/v1/projects/website/remove?without_memory_pr=true")
        self.assertEqual(status, 200, body)
        self.assertEqual(self.archived.read_text(encoding="utf-8"), MEMORY)

    def test_the_command_can_remove_without_the_pr(self):
        self.github.offer_file_pr.side_effect = GitHubError("gh api: HTTP 403")
        def run(*extra):
            with mock.patch("kandu.remove.StateStore", lambda: self.store), \
                    mock.patch("kandu.mirror.open_github", lambda config: self.github), \
                    mock.patch("sys.argv", ["remove", "website", "--local", str(self.local), *extra]), \
                    mock.patch("sys.stdout") as out, mock.patch("sys.stderr") as err:
                code = remove.main()
            return code, "".join(c.args[0] for c in out.write.call_args_list + err.write.call_args_list)
        code, said = run()
        self.assertEqual(code, 1)
        self.assertIn("--without-memory-pr", said)
        self.assertEqual(self.registry(), self.before)
        code, said = run("--without-memory-pr")
        self.assertEqual(code, 0, said)
        self.assertEqual(self.archived.read_text(encoding="utf-8"), MEMORY)

    def test_didban_asks_the_user_when_the_pr_cannot_be_made(self):
        text = (ROOT / "prompts" / "didban.md").read_text(encoding="utf-8")
        part = text.split("## Removing a project\n")[1].split("\n## ")[0]
        self.assertIn("--without-memory-pr", part)
        self.assertLess(part.index("ask whether"), part.index("--without-memory-pr"))


if __name__ == "__main__":
    unittest.main()
