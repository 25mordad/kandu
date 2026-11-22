"""A project whose `main` is red (DESIGN §5): its tests fail on its default branch.

Kandu tests `main` only when a PR's tests fail in the merge gate, and only on a project that turned `watch_main` on.
A result is kept for each `main` commit, so one commit is tested at most twice, and `main` is red only when both runs
fail: one failure and one pass count as passing. Everything here reads and writes the local log; the supervisor runs
the tests and the notification and the dashboard read what is kept.
"""
import re

RED_REASON = "main's tests fail"
RUNS = 2
MAX_NAMES = 20
# The names of failing tests as `unittest` and `pytest` print them; any other runner leaves the list empty.
_UNITTEST = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([\w.]+)\)", re.MULTILINE)
_PYTEST = re.compile(r"^(?:FAILED|ERROR) ([\w./:\[\]-]+)", re.MULTILINE)


def failing_tests(output):
    """The names of the failing tests in a test run's output, without repeats, as many as `MAX_NAMES`."""
    names = [path for _, path in _UNITTEST.findall(output)]
    names += _PYTEST.findall(output)
    return list(dict.fromkeys(names))[:MAX_NAMES]


def runs(store, project, repo, commit):
    """The runs kept for this commit, oldest first."""
    found = []
    for day in store.days():
        found += [e for e in store.events(day) if e["type"] == "main_tested" and e["project"] == project.name
                  and e["repo"] == repo and e["commit"] == commit]
    return found


def decided(kept):
    """True when a commit needs no other run: one run passed, or both runs failed."""
    return any(r["passed"] for r in kept) or len(kept) >= RUNS


def is_red(kept):
    return len(kept) >= RUNS and not any(r["passed"] for r in kept)


def red(store, project, repo):
    """The `main_red` event while `repo`'s `main` is red, or None: a later `main_green` ends it."""
    last = None
    for day in store.days():
        for e in store.events(day):
            if e["type"] in ("main_red", "main_green") and e["project"] == project.name and e["repo"] == repo:
                last = e
    return last if last is not None and last["type"] == "main_red" else None


def red_events(store, project):
    """Every repo of the project whose `main` is red now."""
    return [e for repo in project.repos if (e := red(store, project, repo)) is not None]


def fix_tasks(store, project):
    """The keys of the project's fix tasks, `OWNER/REPO#N`, each the one task of a red commit."""
    found = []
    for day in store.days():
        found += [e["task"] for e in store.events(day) if e["type"] == "main_fix_opened" and e["project"] == project.name]
    return found


def fix_event(store, project, repo, commit):
    """The `main_fix_opened` event of this red commit, or None."""
    for day in store.days():
        for e in store.events(day):
            if (e["type"] == "main_fix_opened" and e["project"] == project.name and e["repo"] == repo
                    and e["commit"] == commit):
                return e
    return None


def fix_opened(store, project, repo, commit):
    return fix_event(store, project, repo, commit) is not None


def fix_body(repo, commit, failing, test_command):
    """The fix task, in the issue template's sections, naming the failing tests."""
    names = "\n".join(f"- `{n}`" for n in failing) or "- the test run's output did not name a failing test"
    return (f"## Goal\n`main` of {repo} passes its tests again.\n\n"
            f"## Current behavior\nThe tests fail on `main` at commit `{commit[:12]}`, in two runs on that commit. "
            f"Failing tests:\n{names}\n\n"
            f"## Acceptance criteria\n- [ ] The project's tests pass on `main` after this change: `{test_command}`\n\n"
            "## How to test\nRun the project's test command on this branch.\n\n"
            "## Out of scope\n- Any change that the failing tests do not need.\n\n"
            "## Blocked by\nnone\n")
