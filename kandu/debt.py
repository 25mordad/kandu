"""The `Technical debt` column (DESIGN §2): tasks parked until the user refines them.

No agent reads this column: the loop goes by the columns it names. Didban offers
these tasks to the user, and `promote` holds one to the rules of any task before
it reaches `Backlog`.
"""
import argparse
import sys
from pathlib import Path

from .config import BOARD_FIELDS, DEFAULT_LOCAL_DIR, TECHNICAL_DEBT, ConfigError, load_config
from .github import AGENT_LABEL, BoardCache, GitHub, GitHubError
from .split import BACKLOG, find_item
from .tasks import TaskError, check_task, check_text, find_project, set_fields


def parked(config, github):
    """Returns the parked tasks of every registered project, by the light board read."""
    boards = {}
    for project in config.projects:
        if project.board is not None:
            boards.setdefault((project.board.owner, project.board.number), project.board)
    repos = {repo for project in config.projects for repo in project.repos}
    return [item for board in boards.values() for item in github.board_items(board)
            if item.status == TECHNICAL_DEBT and item.repo in repos and AGENT_LABEL in item.labels]


def add(config, github, project_name, repo, title, body, local_dir):
    """Parks a task and returns its URL. It has no fields until it is refined."""
    project, repo = find_project(config, project_name, repo)
    if not body.strip():
        raise TaskError("say in a few words what this task is")
    check_text(github, repo, title, body, local_dir)
    url = github.create_issue(repo, title, body)
    github.add_to_board(project.board, url)
    github.set_status(project.board, url, TECHNICAL_DEBT)
    return url


def promote(config, github, key, title, body, local_dir, priority, kind, size, deadline=None):
    """Gives a parked task its refined text and fields. The column moves last, so nothing sees it half done."""
    project, item = find_item(config, github, key)
    if item.status != TECHNICAL_DEBT:
        raise TaskError(f"{key} is not in {TECHNICAL_DEBT}")
    check_task(github, item.repo, title, body, local_dir, priority, kind, size, deadline)
    github.edit_issue(item.repo, item.number, title, body)
    set_fields(github, project.board, item.url, priority, kind, size, deadline)
    github.set_status(project.board, item.url, BACKLOG)


def read_body(path):
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Park tasks, list them and bring them back.")
    parser.add_argument("--local", default=DEFAULT_LOCAL_DIR, help="config directory (default: local/)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    new = commands.add_parser("add")
    new.add_argument("--project", required=True)
    new.add_argument("--repo", help="needed when the project has several repos")
    new.add_argument("--title", required=True)
    new.add_argument("--body-file", required=True, help="- reads stdin")
    back = commands.add_parser("promote")
    back.add_argument("task", help="owner/repo#number")
    back.add_argument("--title", required=True)
    back.add_argument("--body-file", required=True, help="- reads stdin")
    back.add_argument("--priority", required=True, choices=BOARD_FIELDS["Priority"])
    back.add_argument("--kind", required=True, choices=BOARD_FIELDS["Kind"])
    back.add_argument("--size", required=True, choices=BOARD_FIELDS["Size"])
    back.add_argument("--deadline", help="the date it must be done by, YYYY-MM-DD")
    args = parser.parse_args()
    try:
        config = load_config(args.local)
        github = GitHub(cache=BoardCache())
        if args.command == "list":
            for item in parked(config, github):
                print(f"{item.repo}#{item.number}: {item.title}")
        elif args.command == "add":
            print(add(config, github, args.project, args.repo, args.title, read_body(args.body_file), args.local))
        else:
            promote(config, github, args.task, args.title, read_body(args.body_file), args.local,
                    args.priority, args.kind, args.size, args.deadline)
    except ConfigError as e:
        print(f"debt: {len(e.problems)} problem(s) in config; run: python -m kandu.config", file=sys.stderr)
        return 1
    except (TaskError, GitHubError) as e:
        print(f"debt: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
