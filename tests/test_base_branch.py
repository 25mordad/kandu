"""A project's base branch: the branch Kandu works on and merges into, by default the repo's default branch."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kandu import add, agents, base, bazras, doctor, merge, remove, waits
from kandu.config import Board, ConfigError, load_config
from kandu.dashboard import Api, flower_marks, task_statuses
from kandu.github import GitHub, GitHubError
from kandu.state import StateStore
from test_supervisor import EXAMPLES, KEY, PR_OPENED, REPO, SHA1, SHA2, Base, report

ROOT = Path(__file__).resolve().parent.parent
TIP = "f" * 8
LOOKS = ("dashboard.html", "classic.html", "classic-new.html")


class Branches:
    """GitHub's branches of every repo: the default `main`, and `dev` while `has_dev`. A task's branch is gone, as
    after a merge."""

    def __init__(self, has_dev=True):
        self.has_dev = has_dev
        self.asked = []

    def __call__(self, repo, branch):
        self.asked.append((repo, branch))
        return TIP if branch == "main" or (branch == "dev" and self.has_dev) else None


class SupervisorBase(Base):
    def setUp(self):
        super().setUp()
        self.project = self.config.projects[0]
        self.project.deploy = None
        self.branches = Branches()
        self.github.branch_tip = self.branches
        self.worktrees = []
        self.runner.prepare_worktree = lambda project, repo, branch, start, **kw: self.worktrees.append(start) or "/w"

    def on_dev(self):
        self.project.base_branch = "dev"


class WithoutABaseBranchTest(SupervisorBase):
    """A project that names none works on the repo's default branch, exactly as before."""

    def test_its_worktree_starts_from_the_default_branch_and_its_pr_targets_and_merges_into_it(self):
        self.project.auto_merge = True
        self.github.add(1)
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]
        self.cycle()
        self.assertEqual(self.worktrees, ["main"])
        self.assertEqual([opened[2] for opened in self.github.opened], ["main"])
        self.assertEqual(self.github.merges, [SHA1])
        self.assertEqual(self.github.status(1), "Done")

    def test_github_is_never_asked_for_its_base_branch_and_nothing_is_recorded(self):
        self.github.add(1)
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]
        self.cycle()
        self.assertEqual([branch for _, branch in self.branches.asked if branch in ("main", "dev")], [])
        self.assertEqual(self.events(base.MISSING) + self.events(base.FOUND), [])

    def test_its_agents_get_the_same_prompt_as_before(self):
        self.assertFalse(any("Base branch" in fact for fact in agents._project_facts(self.project)))
        prompt = agents.check_prompt(self.project, REPO, 1, None, None, "Does it build?", "main")
        self.assertIn("- The task has no open PR: your checkout is the default branch\n", prompt)

    def test_a_pr_retargeted_to_another_branch_is_not_merged_either(self):
        self.project.auto_merge = True
        self.github.add(1, "In review")
        self.github.open_pr(REPO, 1, SHA1)["baseRefName"] = "release"
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.assertEqual(self.github.merges, [])
        self.assertEqual([d["reason"] for d in self.decisions("merge_waits")], [merge.WRONG_BASE])


class OnDevTest(SupervisorBase):
    """A project with `base_branch: dev` branches, opens its PR and merges on `dev`."""

    def setUp(self):
        super().setUp()
        self.on_dev()

    def test_its_worktree_starts_from_dev_and_its_pr_targets_and_merges_into_dev(self):
        self.project.auto_merge = True
        self.github.add(1)
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]
        self.cycle()
        self.assertEqual(self.worktrees, ["dev"])
        self.assertEqual([opened[2] for opened in self.github.opened], ["dev"])
        self.assertEqual(self.github.merges, [SHA1])
        self.assertEqual(self.github.pr(REPO, 7)["baseRefName"], "dev")
        self.assertEqual(self.github.status(1), "Done")

    def test_an_update_round_brings_in_dev(self):
        self.project.auto_merge = True
        self.github.add(1, "In review")
        self.github.open_pr(REPO, 1, SHA1)["baseRefName"] = "dev"
        self.github.prs[(REPO, 1)]["mergeable"] = "CONFLICTING"

        def pushed():
            self.github.prs[(REPO, 1)] |= {"headRefOid": SHA2, "mergeable": "MERGEABLE"}
        self.runner.on_push = pushed
        self.runner.steps = [report(SHA1), report(SHA2)]
        self.cycle()
        self.assertEqual([merged_base for _, merged_base in self.runner.merged], ["dev"])
        self.assertIn("Merge dev into kandu/1-task", self.runner.merge_message)

    def test_the_deploy_and_the_end_check_run_on_dev(self):
        starts = []
        self.runner.prepare_review = lambda project, repo, branch, head: starts.append((branch, head)) or "/review"
        self.project.auto_merge = True
        self.project.deploy = load_config(self.local).projects[0].deploy
        self.github.add(1, "In review")
        self.github.open_pr(REPO, 1, SHA1)["baseRefName"] = "dev"
        self.runner.steps = [report(SHA1), '{"result": "pass", "summary": "seen"}']
        self.cycle()
        self.assertEqual(self.github.merges, [SHA1])
        self.assertIn(("kandu/deploy", "origin/dev"), starts)
        self.assertIn(("kandu/live-check", "origin/dev"), starts)
        starts.clear()
        self.assertEqual(self.sup._check_project(self.project), [])
        self.assertEqual(starts, [("kandu/end-check", "origin/dev")])

    def test_its_agents_are_told_the_base_branch(self):
        facts = agents._project_facts(self.project)
        self.assertIn("- Base branch: `dev`. Work starts from it and merges into it. Where these instructions say "
                      "the default branch, they mean this one.", facts)
        prompt = agents.check_prompt(self.project, REPO, 1, None, None, "Does it build?", "dev")
        self.assertIn("- The task has no open PR: your checkout is the base branch `dev`", prompt)


class MergeGuardTest(SupervisorBase):
    """No yes merges a PR whose base is not the project's base branch."""

    def setUp(self):
        super().setUp()
        self.on_dev()
        self.github.add(1, "In review")
        self.pr = self.github.open_pr(REPO, 1, SHA1)
        self.pr["baseRefName"] = "main"

    def test_auto_merge_does_not_merge_it(self):
        self.project.auto_merge = True
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.assertEqual(self.github.merges, [])
        self.assertEqual(self.github.status(1), "In review")
        self.assertEqual([d["reason"] for d in self.decisions("merge_waits")], [merge.WRONG_BASE])

    def test_the_users_yes_and_the_queens_yes_do_not_merge_it(self):
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.store.record("user_approved", task=KEY, project="website", head_sha=SHA1)
        self.cycle()
        self.store.record("queen_approved", task=KEY, project="website", head_sha=SHA1, note="Merge it.")
        self.cycle()
        self.assertEqual(self.github.merges, [])
        self.assertTrue(all(d["reason"] == merge.WRONG_BASE for d in self.decisions("merge_waits")))

    def test_a_conflicting_pr_gets_no_update_round(self):
        self.project.auto_merge = True
        self.pr["mergeable"] = "CONFLICTING"
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.assertEqual(self.runner.merged, [])

    def test_a_pr_whose_base_changed_after_it_was_read_is_not_merged(self):
        self.project.auto_merge = True
        self.pr["baseRefName"] = "dev"
        real = self.github.refresh_pr

        def retargeted(repo, number):
            if self.events("review_finished"):
                self.pr["baseRefName"] = "main"
            return real(repo, number)
        self.github.refresh_pr = retargeted
        self.runner.steps = [report(SHA1)]
        self.cycle()
        self.assertEqual(self.github.merges, [])

    def test_the_queens_rework_on_a_conflicting_pr_merges_no_branch_into_it(self):
        self.config.settings.supervisor.queen = True
        self.pr["mergeable"] = "CONFLICTING"
        self.sup._carry_out([{"task": KEY, "action": "rework", "note": "Rename the helper."}], [])
        no_decisions = json.dumps({"brief": "Read it.", "decisions": []})
        self.runner.steps = [self.worker_pushes(SHA2), report(SHA2), no_decisions, no_decisions]
        self.cycle()
        self.assertIn("Rename the helper.", self.runner.runs[0].prompt)
        self.assertEqual(self.events("queen_step_done")[0]["result"], "pushed")
        self.assertEqual(self.runner.merged, [])

    def test_the_dashboard_shows_it_waiting_for_the_user_with_that_reason(self):
        self.project.auto_merge = True
        self.runner.steps = [report(SHA1)]
        self.cycle()
        events = [e for day in self.store.days() for e in self.store.events(day)]
        status = task_statuses(events)[KEY]
        self.assertEqual((status["why"], status["detail"]), ("merge_waits", merge.WRONG_BASE))
        self.assertEqual(status["wait"]["on"], waits.DECISION)
        self.assertEqual(status["wait"]["detail"], [merge.WRONG_BASE])
        mark = flower_marks(self.store)[KEY]
        self.assertTrue(mark["yours"])
        self.assertEqual(mark["waited"], [merge.WRONG_BASE])


class MissingBaseTest(SupervisorBase):
    """When `dev` is not on GitHub, none of the project's tasks starts, and the dashboard and the doctor say why."""

    def setUp(self):
        super().setUp()
        self.on_dev()
        self.branches.has_dev = False
        self.github.add(1)

    def test_its_tasks_wait_and_the_log_says_why_once(self):
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]
        self.cycle()
        self.cycle()
        self.assertEqual(self.runner.runs, [])
        self.assertEqual(self.github.status(1), "Ready")
        missing = self.events(base.MISSING)
        self.assertEqual([(e["project"], e["branch"], e["repos"]) for e in missing], [("website", "dev", [REPO])])

    def test_its_backlog_is_not_sized(self):
        self.github.on_add = lambda key, size: None  # left unsized, for the splitter
        self.github.add(2, "Backlog")
        self.cycle()
        self.assertEqual(self.runner.runs, [])

    def test_once_dev_is_there_its_tasks_start_again(self):
        self.cycle()
        self.branches.has_dev = True
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]
        self.cycle()
        self.assertEqual(self.worktrees, ["dev"])
        self.assertEqual(len(self.events(base.FOUND)), 1)

    def test_a_read_that_fails_keeps_it_held(self):
        self.cycle()

        def fails(repo, branch):
            raise GitHubError("gh api: down")
        self.github.branch_tip = fails
        self.runner.steps = [self.worker_makes_pr(), report(SHA1)]
        self.cycle()
        self.assertEqual(self.runner.runs, [])
        self.assertEqual(self.github.status(1), "Ready")
        self.assertEqual(self.events(base.FOUND), [])

    def test_another_project_goes_on(self):
        self.github.add(5, repo="acme-org/internal-tool")
        self.runner.steps = [self.worker_makes_pr(5)]
        self.cycle()
        self.assertEqual(len(self.runner.runs), 1)
        self.assertEqual(self.github.status(1), "Ready")

    def test_the_dashboard_says_why_until_dev_is_there(self):
        self.cycle()
        board = Api.__new__(Api)
        board.config, board.store = self.config, self.store
        self.assertEqual(board._base_missing(), {"website": {
            "branch": "dev", "repos": [REPO], "why": f"the base branch dev is not on GitHub in {REPO}"}})
        self.branches.has_dev = True
        self.cycle()
        self.assertEqual(board._base_missing(), {})

    def test_every_look_shows_the_notice(self):
        for look in LOOKS:
            page = (ROOT / "kandu" / look).read_text(encoding="utf-8")
            with self.subTest(look=look):
                self.assertIn('id="basemissing"', page)
                self.assertIn("renderBaseMissing(s.base_missing);", page)
                self.assertEqual(page.count('"base_missing_title":'), 2)  # English and Persian

    def test_every_look_says_the_merge_reason_in_persian(self):
        for look in LOOKS:
            page = (ROOT / "kandu" / look).read_text(encoding="utf-8")
            with self.subTest(look=look):
                self.assertIn(f'  "{merge.WRONG_BASE}": "پایهٔ این PR شاخهٔ پایهٔ پروژه نیست",', page)


class DoctorTest(unittest.TestCase):
    def check(self, base_branch="", has=True, fails=False):
        host = mock.Mock()
        host.absolute.side_effect = lambda p: p
        host.succeeds.return_value = True
        github = mock.Mock()
        github.branch_tip.side_effect = GitHubError("gh api: down") if fails else (
            lambda repo, branch: TIP if has else None)
        item = mock.Mock(path="~/p", repos=["o/r"], run="clone", machine="m", test="npm test", board=None,
                         secrets_from=None, task_tokens={}, base_branch=base_branch)
        item.name = "site"
        return doctor.check_project(item, mock.Mock(hosts={"m": host}), github), github

    def test_a_missing_base_branch_fails_the_project(self):
        results, _ = self.check("dev", has=False)
        self.assertIn((doctor.FAIL, "site: the base branch dev is not on GitHub in o/r; none of its tasks starts"),
                      results)

    def test_a_base_branch_that_cannot_be_read_fails_the_project(self):
        results, _ = self.check("dev", fails=True)
        self.assertIn((doctor.FAIL, "site: base branch dev cannot be read"), results)

    def test_a_base_branch_that_is_there_passes(self):
        results, _ = self.check("dev")
        self.assertIn((doctor.OK, "site: base branch dev"), results)

    def test_a_project_without_one_is_not_asked(self):
        results, github = self.check()
        github.branch_tip.assert_not_called()
        self.assertFalse(any("base branch" in text for _, text in results))


class RegistryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        shutil.copy(EXAMPLES / "kandu.example.yaml", self.local / "kandu.yaml")
        shutil.copy(EXAMPLES / "projects.example.yaml", self.local / "projects.yaml")

    def named(self, value):
        path = self.local / "projects.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("# base_branch: dev", f"base_branch: {value}", 1),
                        encoding="utf-8")

    def test_without_it_a_project_has_none(self):
        self.assertEqual({p.base_branch for p in load_config(self.local).projects}, {""})

    def test_the_example_shows_it_and_a_project_may_set_it(self):
        self.named("dev")
        self.assertEqual(load_config(self.local).projects[0].base_branch, "dev")

    def test_a_name_that_is_no_branch_is_refused(self):
        self.named('"-x"')
        with self.assertRaises(ConfigError) as caught:
            load_config(self.local)
        self.assertTrue(any("base_branch" in problem for problem in caught.exception.problems))

    def test_kandu_add_writes_it_only_when_given(self):
        fields = {"name": "shop", "family": "shop", "repo": "acme-org/shop", "run": "clone", "machine": "server",
                  "path": "~/kandu-work/shop", "test": "make test"}
        self.assertNotIn("base_branch", add._entry(fields, Board("acme-org", 1)))
        self.assertEqual(add._entry(fields | {"base_branch": "dev"}, Board("acme-org", 1))["base_branch"], "dev")

    def test_the_commands_ask_for_it_and_change_it(self):
        commands = ROOT / ".claude" / "commands"
        self.assertIn("base_branch", (commands / "kandu-add.md").read_text(encoding="utf-8"))
        self.assertIn("Propose the repo's default branch", (commands / "kandu-add.md").read_text(encoding="utf-8"))
        self.assertIn("base_branch", (commands / "kandu-config.md").read_text(encoding="utf-8"))

    def test_the_settings_pop_up_shows_it_read_only_in_every_look(self):
        self.named("dev")
        config = load_config(self.local)
        board = Api.__new__(Api)
        board.config, board.store = config, StateStore(self.local / "state")
        rows = {row["name"]: row for row in board._project_settings()}
        self.assertEqual((rows["website"]["base_branch"], rows["internal-tool"]["base_branch"]), ("dev", ""))
        for look in LOOKS:
            page = (ROOT / "kandu" / look).read_text(encoding="utf-8")
            with self.subTest(look=look):
                self.assertIn("data-base-branch>${esc(row.base_branch || t(\"set_base_branch_default\"))}</b>", page)
                self.assertNotIn('data-draft="base_branch"', page)  # shown, never changed here
                self.assertEqual(page.count('"set_base_branch":'), 2)

    def test_the_glossary_has_the_entry(self):
        self.assertIn("| **Base branch** | The branch of a project's repo that Kandu works on and merges into; by "
                      "default the repo's default branch |", (ROOT / "GLOSSARY.md").read_text(encoding="utf-8"))


class ElsewhereTest(unittest.TestCase):
    """Bazras, a Kanduban and the memory PR of a removed project use the base branch too."""

    def test_bazras_looks_at_the_base_branch(self):
        host = mock.Mock()
        project = SimpleNamespace(base_branch="dev")
        self.assertEqual(bazras._base_branch(project, host, "/copy"), "dev")
        host.run.assert_not_called()
        host.run.return_value = SimpleNamespace(returncode=0, stdout="origin/trunk\n")
        self.assertEqual(bazras._base_branch(SimpleNamespace(base_branch=""), host, "/copy"), "trunk")

    def test_the_memory_pr_targets_the_base_branch(self):
        github = GitHub(run=mock.Mock())
        calls = []
        github._json = lambda args: calls.append(args) or (
            [] if "pulls?state=open" in args[1] else {"object": {"sha": TIP}, "html_url": "u", "sha": None})
        github._run = lambda args: calls.append(args)
        github.default_branch = mock.Mock(return_value="main")
        github.offer_file_pr("o/r", "kandu/memory-site", "KANDU-MEMORY.md", "text", "T", "B", base="dev")
        github.default_branch.assert_not_called()
        self.assertIn(["api", "repos/o/r/git/ref/heads/dev"], calls)
        self.assertIn("base=dev", calls[-1])


class RemoveTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name) / "local"
        self.local.mkdir()
        shutil.copy(EXAMPLES / "kandu.example.yaml", self.local / "kandu.yaml")
        shutil.copy(EXAMPLES / "projects.example.yaml", self.local / "projects.yaml")
        (self.local / "memory").mkdir()
        (self.local / "memory" / "website.md").write_text("# website\n- cart: the basket\n", encoding="utf-8")
        self.github = mock.Mock()
        self.github.repo_is_public.return_value = False
        self.github.offer_file_pr.return_value = "https://github.com/acme-org/website/pull/5"
        self.store = StateStore(Path(tmp.name) / "state")

    def remove(self):
        flights = mock.Mock()
        flights.list.return_value = {"open": [], "closed": []}
        return remove.remove_project("website", self.store, flights, self.local, github=self.github)

    def test_the_memory_pr_is_offered_against_the_base_branch(self):
        path = self.local / "projects.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("# base_branch: dev", "base_branch: dev", 1),
                        encoding="utf-8")
        self.remove()
        self.assertEqual(self.github.offer_file_pr.call_args.kwargs, {"base": "dev"})

    def test_without_one_it_is_offered_as_before(self):
        self.remove()
        self.assertEqual(self.github.offer_file_pr.call_args.kwargs, {})


if __name__ == "__main__":
    unittest.main()
