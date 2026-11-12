"""The cleanup after a merge (DESIGN §9): what Kandu made for a task goes once its PR is merged.

Code, not an agent. Each cycle looks at the merged PRs the copy holds. For a task merged after the cleanup began,
it removes the task's folders beside the copy (its worktree and its review checkouts), its local `kandu/` branch
and its branch on GitHub. A folder or branch that holds work found neither in the merged PR nor on GitHub stays,
and the log says why. Nothing else is touched: not the copy, not a deploy checkout, not another branch.
"""
import re
from datetime import datetime

from . import queen
from .flights import task_key
from .github import GitHubError
from .runner import REMOVED, RunnerError

# A task's own branch: `kandu/<issue>-<slug>`. Kandu's other branches, such as Bazras's, are not a task's.
TASK_BRANCH = re.compile(r"kandu/(\d+)-[A-Za-z0-9-]+")
KEPT_PUSHED_AFTER = "it has commits pushed after the merge"


def folders(branch, number):
    """The folders a task's runs make beside the copy: the worker's, its review's and a checker's on the base."""
    slug = branch.removeprefix("kandu/")
    return [slug, f"{slug}-review", f"check-{number}-review"]


def flight_open(store, task):
    """A free flight on the task is open, or still opening. One that failed to start holds nothing."""
    record = store.flight(task_key(task))
    return record is not None and (not record.get("error") or store.flight_socket(record["key"]).exists())


def kept(store, task):
    """What stayed after the task's merge, and why, for its page."""
    return [{"what": e["what"], "target": e["target"], "reason": e["reason"]}
            for e in queen.all_events(store) if e["type"] == "clean_kept" and e["task"] == task]


def _merged_at(pr):
    try:
        return datetime.fromisoformat(pr["mergedAt"])
    except (KeyError, TypeError, ValueError):
        return None


class Cleaner:
    def __init__(self, config, store, github, runner):
        self.config = config
        self.store = store
        self.github = github
        self.runner = runner

    def _due(self, since):
        """(project, repo, task, PR) for each merged task not cleaned yet, in the registry's order."""
        done = {(e["task"], e["pr"]) for e in queen.all_events(self.store) if e["type"] == "clean_finished"}
        paused = self.store.paused_projects()
        for project in self.config.projects:
            if project.paused or project.name in paused:
                continue
            for repo in project.repos:
                try:
                    prs = self.github.merged_prs(repo)
                except GitHubError:
                    continue  # the next cycle reads it again
                for pr in sorted(prs, key=lambda pr: pr["number"]):
                    match = TASK_BRANCH.fullmatch(pr["headRefName"])
                    merged = _merged_at(pr)
                    if match is None or pr.get("isCrossRepository") or merged is None or merged < since:
                        continue
                    task = f"{repo}#{match.group(1)}"
                    if (task, pr["number"]) not in done:
                        yield project, repo, task, pr

    def run(self, since):
        """Cleans each merged task it can. A machine that cannot be reached waits for the next cycle, alone."""
        reachable = {}
        for project, repo, task, pr in list(self._due(since)):
            if flight_open(self.store, task):
                continue
            machine = project.machine
            if machine not in reachable:
                reachable[machine] = self.runner.reachable(machine)
            if not reachable[machine]:
                continue
            try:
                self._clean(project, repo, task, pr)
            except RunnerError as e:
                reachable[machine] = False  # it went away: its other tasks wait too, the rest go on
                self._failed(project, task, e)
            except GitHubError as e:
                self._failed(project, task, e)

    def _failed(self, project, task, error):
        self.store.record("decision", task=task, project=project.name, decision="clean_failed", reason=str(error))

    def _clean(self, project, repo, task, pr):
        branch, sha = pr["headRefName"], pr["headRefOid"]
        number = TASK_BRANCH.fullmatch(branch).group(1)
        for what, target, outcome in self.runner.clean_task(project, repo, branch, folders(branch, number), sha):
            self._note(project, task, what, target, outcome)
        tip = self.github.branch_tip(repo, branch)
        if tip is not None and tip != sha:
            self._note(project, task, "remote_branch", branch, KEPT_PUSHED_AFTER)
        elif tip is not None:
            self.github.delete_branch(repo, branch)
            self._note(project, task, "remote_branch", branch, REMOVED)
        self.store.record("clean_finished", task=task, project=project.name, pr=pr["number"])

    def _note(self, project, task, what, target, outcome):
        if outcome == REMOVED:
            self.store.record("cleaned", task=task, project=project.name, what=what, target=target)
        else:
            self.store.record("clean_kept", task=task, project=project.name, what=what, target=target, reason=outcome)
