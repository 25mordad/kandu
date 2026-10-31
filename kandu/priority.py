"""Raises a task to `High` on the user's word (DESIGN §2).

The event wakes the queen at her next look, and code then lets nothing else of
that project go first. Nothing that is running gives way.
"""
import argparse
import sys

from .config import DEFAULT_LOCAL_DIR, ConfigError, load_config
from .github import BoardCache, GitHub, GitHubError
from .queen import HIGH
from .split import find_item
from .state import StateStore
from .tasks import TaskError


def raise_task(config, github, store, key):
    project, item = find_item(config, github, key)
    github.set_field(project.board, item.url, "Priority", HIGH)
    store.record("priority_raised", task=key, project=project.name)
    return f"{key} is {HIGH}: it goes next, once the work in hand ends"


def main():
    parser = argparse.ArgumentParser(description="Raise a task to High.")
    parser.add_argument("task", help="owner/repo#number")
    parser.add_argument("--local", default=DEFAULT_LOCAL_DIR, help="config directory (default: local/)")
    args = parser.parse_args()
    try:
        print(raise_task(load_config(args.local), GitHub(cache=BoardCache()), StateStore(), args.task))
    except ConfigError as e:
        print(f"priority: {len(e.problems)} problem(s) in config; run: python -m kandu.config", file=sys.stderr)
        return 1
    except (TaskError, GitHubError) as e:
        print(f"priority: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
