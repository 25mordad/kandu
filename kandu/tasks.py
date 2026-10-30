"""Puts a task on a board (DESIGN §2).

`/kandu-task` writes the text; this code enforces what must hold, so a rule
does not depend on the conversation.
"""
import argparse
import re
import sys
from pathlib import Path

from .config import DEFAULT_LOCAL_DIR, ConfigError, load_config
from .github import GitHub, GitHubError

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


def create_task(config, github, project_name, repo, title, body, local_dir):
    """Creates the issue, puts it on the board in `Ready` and returns its URL."""
    project = next((p for p in config.projects if p.name == project_name), None)
    if project is None:
        raise TaskError(f"unknown project: {project_name}")
    repo = repo or (project.repos[0] if len(project.repos) == 1 else None)
    if repo not in project.repos:
        raise TaskError(f"name one of the project's repos: {', '.join(project.repos)}")
    if project.board is None:
        raise TaskError(f"{project_name} has no board")
    check_body(body)
    if github.repo_is_public(repo):
        check_private(f"{title}\n{body}", private_words(local_dir))
    url = github.create_issue(repo, title, body)
    github.add_to_board(project.board, url)
    github.set_status(project.board, url, "Ready")
    return url


def main():
    parser = argparse.ArgumentParser(description="Put a task on a board.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--repo", help="needed when the project has several repos")
    parser.add_argument("--title", required=True)
    parser.add_argument("--body-file", required=True)
    parser.add_argument("--local", default=DEFAULT_LOCAL_DIR, help="config directory (default: local/)")
    args = parser.parse_args()
    try:
        config = load_config(args.local)
        body = Path(args.body_file).read_text(encoding="utf-8")
        print(create_task(config, GitHub(), args.project, args.repo, args.title, body, args.local))
    except ConfigError as e:
        print(f"tasks: {len(e.problems)} problem(s) in config; run: python -m kandu.config", file=sys.stderr)
        return 1
    except (TaskError, GitHubError) as e:
        print(f"tasks: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
