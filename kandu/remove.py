"""Takes a project out of Kandu in one go: its running work, Kandu's worktrees for it and its entry in `projects.yaml`.

The project itself is not touched: its copy, repo, board and issues stay as they are. What Kandu learned about it,
its memory and its brief, is first offered as a PR to its own repo, never to a public one, then archived on the
brain, where adding the project back finds it again. Its entry is cut out of the text, so every other entry and
comment stays, and the new config is validated on a copy before it reaches `local/`. Anything that fails before the
entry is cut leaves the config as it was.

    .venv/bin/python -m kandu.remove website [--without-memory-pr]
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import yaml

from . import memory
from .add import AddError, _validate_copy, _write
from .config import DEFAULT_LOCAL_DIR, ConfigError, load_config
from .github import GitHubError
from .runner import Runner, RunnerError, repo_path
from .state import StateStore

STOP_WAIT_SECONDS = 120  # a worker told to stop ends within seconds; past this it cannot be stopped
POLL_SECONDS = 2
ITEM = re.compile(r"^(\s*)-(\s|$)")
MEMORY_FILE = "docs/kandu-memory.md"  # where the memory lands in the project's own repo
WITHOUT_PR = "--without-memory-pr"


class RemoveError(Exception):
    pass


class MemoryPrError(RemoveError):
    """The PR with the memory could not be made: nothing is removed until the user decides."""


def _entries(lines):
    """(start, end) of each entry of the `projects:` list. Comments and blank lines after an entry, at the list's own
    indent or less, stay outside it: they belong to what follows."""
    top = next((i for i, line in enumerate(lines) if re.match(r"projects:", line)), None)
    if top is None:
        return []
    starts, indent = [], None
    for i in range(top + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line[0].isspace() and not line.startswith(("#", "-")):
            break  # the next top-level key
        found = ITEM.match(line)
        if found and (indent is None or found.group(1) == indent):
            indent = found.group(1)
            starts.append(i)
    entries = []
    for n, start in enumerate(starts):
        limit = starts[n + 1] if n + 1 < len(starts) else len(lines)
        end = start + 1
        for i in range(start + 1, limit):
            line = lines[i]
            if not line.strip():
                continue
            if len(line) - len(line.lstrip()) <= len(indent):
                break
            end = i + 1
        entries.append((start, end))
    return entries


def _name(lines, start, end):
    try:
        entry = yaml.safe_load("".join(lines[start:end]))
    except yaml.YAMLError:
        return None
    return entry[0].get("name") if isinstance(entry, list) and entry and isinstance(entry[0], dict) else None


def projects_text(text, name):
    """`projects.yaml` without the entry named `name`; every other line stays as it was."""
    lines = text.splitlines(keepends=True)
    entries = _entries(lines)
    found = [(start, end) for start, end in entries if _name(lines, start, end) == name]
    if not found:
        raise RemoveError(f"there is no project named {name!r} in projects.yaml")
    start, end = found[0]
    del lines[start:end]
    # One blank line between what was before it and what came after, not two.
    while start < len(lines) and not lines[start].strip() and (start == 0 or not lines[start - 1].strip()):
        del lines[start]
    text = "".join(lines)
    if len(entries) == 1:
        text = re.sub(r"^projects:[ \t]*(#.*)?$", lambda m: "projects: []" + (f"  {m.group(1)}" if m.group(1) else ""),
                      text, count=1, flags=re.MULTILINE)
    return text


def _running(store, name):
    return [w for w in store.read_current().get("workers", []) if w.get("project") == name]


def stop_work(store, flights, name, wait=STOP_WAIT_SECONDS, sleep=time.sleep, clock=None):
    """Closes the project's task sessions and stops each of its workers. Raises RemoveError if any is left."""
    if flights is not None:
        for record in store.flights():
            if record.get("kind") == "task" and record.get("project") == name:
                flights.close(record["key"])
        left = [r for r in store.flights() if r.get("kind") == "task" and r.get("project") == name]
        if left:
            raise RemoveError(f"the session on {left[0].get('task')} could not be closed; nothing was removed")
    clock = clock or time.monotonic
    asked, deadline = set(), clock() + wait
    while True:
        workers = _running(store, name)
        if not workers:
            return
        for worker in workers:
            run_id = worker.get("run_id")
            if run_id and run_id not in asked:
                store.request_worker_stop(run_id)
                asked.add(run_id)
        if clock() >= deadline:
            tasks = ", ".join(w.get("task", "?") for w in workers)
            raise RemoveError(f"the running work on {name} ({tasks}) could not be stopped in {wait} seconds; "
                              "nothing was removed")
        sleep(POLL_SECONDS)


def remove_worktrees(config, project, runner=None):
    """Removes each worktree Kandu made beside the project's copies, and nothing else there. Returns their paths."""
    runner = runner or Runner(config)
    host = runner.hosts.get(project.machine)
    if host is None:
        raise RemoveError(f"unknown machine {project.machine!r}; the config was not changed")
    # Without this, a machine out of reach would look like one with no copy, and its worktrees would stay.
    if not runner.reachable(project.machine):
        raise RemoveError(f"{project.machine} cannot be reached, so Kandu's worktrees for {project.name} there "
                          "could not be removed; the config was not changed")
    removed = []
    try:
        for repo in project.repos:
            copy = host.absolute(repo_path(project, repo)).rstrip("/")
            root = f"{copy}.kandu-worktrees/"
            if not host.succeeds(["test", "-e", ".git"], cwd=copy):
                continue
            listed = host.check(["git", "worktree", "list", "--porcelain"], cwd=copy)
            for line in listed.splitlines():
                path = line.removeprefix("worktree ").rstrip("/")
                if line.startswith("worktree ") and path.startswith(root):
                    host.check(["git", "worktree", "remove", "--force", "--force", path], cwd=copy)
                    removed.append(path)
            host.check(["git", "worktree", "prune"], cwd=copy)
            host.run(["rmdir", root])  # only when empty: anything else in it is not Kandu's to delete
    except RunnerError as e:
        raise RemoveError(f"Kandu's worktrees for {project.name} could not be removed on {project.machine}: {e}; "
                          "the config was not changed") from None
    return removed


def offer_memory(project, github, local_dir, memory_pr=True):
    """Offers what Kandu learned about the project as a PR to its first repo, for its owner to merge.

    Says what happened and why. Raises MemoryPrError if the PR cannot be made, so nothing is removed until the user
    decides; `memory_pr=False` is that decision to go on without it.
    """
    text = memory.offered_text(local_dir, project.name)
    if text is None:
        return {"pr": None, "why": "none", "note": "Kandu has no memory of this project."}
    if not memory_pr:
        return {"pr": None, "why": "declined", "note": "No PR with its memory, as you decided. Its memory is archived on the brain."}
    repo = project.repos[0]
    try:
        if github is None:
            raise GitHubError("GitHub cannot be reached from here")
        if github.repo_is_public(repo):
            return {"pr": None, "why": "public", "note": f"No PR with its memory: {repo} is public, and the memory may hold private "
                                        "details. Its memory is archived on the brain."}
        url = github.offer_file_pr(
            repo, f"kandu/memory-{project.name}", MEMORY_FILE, text,
            "Keep what Kandu learned about this project",
            f"This project was removed from Kandu. This adds what Kandu learned about it, its memory and brief, as "
            f"`{MEMORY_FILE}`, so it stays with the project. Merge it if you want to keep it.",
            **({"base": project.base_branch} if project.base_branch else {}))  # none named: the default branch
    except GitHubError as e:
        raise MemoryPrError(f"the PR with the memory of {project.name} could not be offered to {repo}: {e}. "
                            "Nothing was removed. Try again, or remove it without that PR; its memory is archived "
                            "on the brain either way") from None
    return {"pr": url, "why": "offered", "note": f"Its memory is offered to {repo} in {url}, for its owner to merge. "
                               "It is archived on the brain too."}


def remove_project(name, store, flights=None, local_dir=DEFAULT_LOCAL_DIR, github=None, memory_pr=True,
                   wait=STOP_WAIT_SECONDS, sleep=time.sleep, runner=None):
    """Offers the project's memory to its repo, stops its work, removes Kandu's worktrees for it, cuts its entry out
    of `projects.yaml`, then archives its memory. Returns what was done."""
    local_dir = Path(local_dir)
    config = load_config(local_dir)
    project = next((p for p in config.projects if p.name == name), None)
    if project is None:
        raise RemoveError(f"there is no project named {name!r} in Kandu")
    path = local_dir / "projects.yaml"
    text = projects_text(path.read_text(encoding="utf-8"), name)
    try:
        _validate_copy(local_dir, projects_text=text)
    except AddError as e:
        raise RemoveError(f"the config would not pass its check without {name}: {e}") from None
    kept = offer_memory(project, github, local_dir, memory_pr)
    was_paused = name in store.paused_projects()
    store.pause_project(name)  # nothing new starts on it while it is taken out
    try:
        stop_work(store, flights, name, wait, sleep)
        worktrees = remove_worktrees(config, project, runner)
    except RemoveError:
        if not was_paused:
            store.resume_project(name)
        raise
    _write(path, text)
    store.resume_project(name)  # a project added back later does not start paused
    archived = memory.archive(local_dir, name)
    store.record("project_removed", project=name)
    try:
        load_config(local_dir)
    except ConfigError as e:  # validated on a copy above, so only a change made meanwhile gets here
        raise RemoveError(f"{name} was removed, but the config does not pass its check: {e.problems[0]}") from None
    return {"removed": name, "worktrees": worktrees, "memory": kept, "archived": archived}


def main():
    parser = argparse.ArgumentParser(description="Take a project out of Kandu. Its copy, repo, board and issues stay; "
                                                 "its memory is offered as a PR to its repo, then archived.")
    parser.add_argument("project")
    parser.add_argument("--local", default=DEFAULT_LOCAL_DIR, help="config directory (default: local/)")
    parser.add_argument(WITHOUT_PR, action="store_true",
                        help="remove it without the PR with its memory, once the user has decided to")
    args = parser.parse_args()
    from .flights import Flights
    from .mirror import open_github
    store = StateStore()
    try:
        config = load_config(args.local)
        github = open_github(config)
        result = remove_project(args.project, store, Flights(config, store, github=github), args.local, github=github,
                                memory_pr=not args.without_memory_pr)
    except (RemoveError, ConfigError) as e:
        print(f"remove: {e.problems[0] if isinstance(e, ConfigError) else e}", file=sys.stderr)
        if isinstance(e, MemoryPrError):
            print(f"remove: to go on without that PR, run again with {WITHOUT_PR}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
