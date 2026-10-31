"""Puts a task on a board (DESIGN §2).

`/kandu-task` writes the text; this code enforces what must hold, so a rule
does not depend on the conversation.
"""
import argparse
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path

from . import sizing
from .config import BOARD_FIELDS, DEFAULT_LOCAL_DIR, DEADLINE_FIELD, ConfigError, load_config
from .github import BoardCache, GitHub, GitHubError

TEMPLATE = Path(__file__).resolve().parent.parent / "prompts" / "templates" / "issue.md"


class TaskError(Exception):
    pass


def required_sections():
    return re.findall(r"^## (.+)$", TEMPLATE.read_text(encoding="utf-8"), re.MULTILINE)


def check_body(body):
    sections = re.findall(r"^## (.+?)\s*$", body, re.MULTILINE)
    missing = [s for s in required_sections() if s not in sections]
    if missing:
        raise TaskError(f"the issue misses: {', '.join(missing)}")


def check_english(text):
    """Agents write and read tasks in English, so nobody pays for a translation."""
    if any(c.isalpha() and not unicodedata.name(c, "").startswith("LATIN") for c in text):
        raise TaskError("a task is written in English: its title, body and sub-tasks")


def private_words(local_dir):
    path = Path(local_dir) / "private-words.txt"
    if not path.exists():
        return []
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def check_private(text, words):
    found = [word for word in words if word.lower() in text.lower()]
    if found:
        raise TaskError(f"this repo is public, and the text holds {len(found)} private word(s)")


def check_fields(priority, kind, size, deadline):
    """The queen paces work from these alone, so a task without them cannot exist."""
    for name, value in (("Priority", priority), ("Kind", kind), ("Size", size)):
        if value not in BOARD_FIELDS[name]:
            raise TaskError(f"{name} must be one of: {', '.join(BOARD_FIELDS[name])}")
    if deadline is not None:
        try:
            date.fromisoformat(deadline)
        except ValueError as e:
            raise TaskError("the deadline must be a date, YYYY-MM-DD") from e


def check_size(body, size):
    """Files and the door are the splitter's to judge; the criteria can be counted here."""
    criteria = sizing.criteria_in(body)
    if not sizing.fits(size, criteria, 0):
        raise TaskError(f"{criteria} acceptance criteria do not fit {size}: size it {sizing.LARGE}")


def check_not_large(size):
    """Didban splits a task while it is defined. The splitter still catches one that turns out large."""
    if size == sizing.LARGE:
        raise TaskError(f"a {sizing.LARGE} task does not go on the board: split it into slices first")


def find_project(config, project_name, repo):
    """Returns the project and the repo a new task goes in."""
    project = next((p for p in config.projects if p.name == project_name), None)
    if project is None:
        raise TaskError(f"unknown project: {project_name}")
    repo = repo or (project.repos[0] if len(project.repos) == 1 else None)
    if repo not in project.repos:
        raise TaskError(f"name one of the project's repos: {', '.join(project.repos)}")
    if project.board is None:
        raise TaskError(f"{project_name} has no board")
    return project, repo


def check_text(github, repo, title, body, local_dir):
    check_english(f"{title}\n{body}")
    if github.repo_is_public(repo):
        check_private(f"{title}\n{body}", private_words(local_dir))


def check_task(github, repo, title, body, local_dir, priority, kind, size, deadline):
    """All that must hold before a task may reach `Backlog`."""
    check_body(body)
    check_fields(priority, kind, size, deadline)
    check_not_large(size)
    check_size(body, size)
    check_text(github, repo, title, body, local_dir)


def set_fields(github, board, url, priority, kind, size, deadline):
    for name, value in (("Priority", priority), ("Kind", kind), ("Size", size), (DEADLINE_FIELD, deadline)):
        if value is not None:
            github.set_field(board, url, name, value)


def create_task(config, github, project_name, repo, title, body, local_dir, priority, kind, size, deadline=None,
                by_hand=False):
    """Creates the issue, puts it on the board in `Backlog` and returns its URL."""
    project, repo = find_project(config, project_name, repo)
    check_task(github, repo, title, body, local_dir, priority, kind, size, deadline)
    url = github.create_issue(repo, title, body, by_hand=by_hand)
    github.add_to_board(project.board, url)
    github.set_status(project.board, url, "Backlog")
    set_fields(github, project.board, url, priority, kind, size, deadline)
    return url


def main():
    parser = argparse.ArgumentParser(description="Put a task on a board.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--repo", help="needed when the project has several repos")
    parser.add_argument("--title", required=True)
    parser.add_argument("--body-file", required=True)
    parser.add_argument("--priority", required=True, choices=BOARD_FIELDS["Priority"])
    parser.add_argument("--kind", required=True, choices=BOARD_FIELDS["Kind"])
    parser.add_argument("--size", required=True, choices=BOARD_FIELDS["Size"])
    parser.add_argument("--deadline", help="the date it must be done by, YYYY-MM-DD")
    parser.add_argument("--by-hand", action="store_true", help="a person does this: no `agent` label")
    parser.add_argument("--local", default=DEFAULT_LOCAL_DIR, help="config directory (default: local/)")
    args = parser.parse_args()
    try:
        config = load_config(args.local)
        body = Path(args.body_file).read_text(encoding="utf-8")
        print(create_task(config, GitHub(cache=BoardCache()), args.project, args.repo, args.title, body, args.local,
                          args.priority, args.kind, args.size, args.deadline, args.by_hand))
    except ConfigError as e:
        print(f"tasks: {len(e.problems)} problem(s) in config; run: python -m kandu.config", file=sys.stderr)
        return 1
    except (TaskError, GitHubError) as e:
        print(f"tasks: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
