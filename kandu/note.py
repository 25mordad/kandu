"""The note a project's repo carries to say that Kandu changes it (DESIGN §1B).

`python -m kandu.note NAME` renders it from the registry and prints it. It
changes nothing: the note reaches the repo only as a task for that project,
which `/kandu-add` offers on the user's yes.
"""
import argparse
import sys
from pathlib import Path

from .config import DEFAULT_LOCAL_DIR, ConfigError, load_config

TEMPLATE = Path(__file__).resolve().parent.parent / "prompts" / "templates" / "kandu-note.md"
FILE = "KANDU.md"
POINTER = f"Changes to this repo come from Kandu, as tasks and PRs: read `{FILE}` before changing it."


class NoteError(Exception):
    pass


def _code(items):
    return ", ".join(f"`{item}`" for item in items)


def render(config, project):
    board = project.board
    if board:
        where = f"GitHub project {board.number} of `{board.owner}`"
        tasks = (f"Its tasks are issues on {where}, its board. Kandu works only on an issue labelled `agent`, "
                 "and moves its card as the work goes.")
        breaks = (f"File a task on {where} that says what broke and how to see it, "
                  "or tell the user to ask Didban, Kandu's control session, for one.")
    else:
        tasks = ("This project has no board yet: its tasks are kept by the user, "
                 "through Didban, Kandu's control session.")
        breaks = "Tell the user to ask Didban, Kandu's control session, for a task that says what broke and how to see it."
    if config.auto_merge_for(project):
        merge = "Kandu merges a PR by itself once its review passes and every merge condition holds."
    else:
        merge = "Kandu merges a PR only after the user approves it."
    forbidden = (f"Kandu never changes {_code(project.forbidden)}. A task that needs one of them is settled with the user first."
                 if project.forbidden else "None: Kandu may change any part of this repo a task needs.")
    return TEMPLATE.read_text(encoding="utf-8").format(
        file=FILE, pointer=POINTER, repos=_code(project.repos), merge=merge,
        board=tasks, forbidden=forbidden, breaks=breaks)


def main():
    parser = argparse.ArgumentParser(description="Print the note that says Kandu changes a project's repo. Changes nothing.")
    parser.add_argument("project")
    parser.add_argument("--local", default=DEFAULT_LOCAL_DIR, help="config directory (default: local/)")
    args = parser.parse_args()
    try:
        config = load_config(args.local)
        project = next((p for p in config.projects if p.name == args.project), None)
        if project is None:
            raise NoteError(f"no project {args.project!r} in the registry")
        sys.stdout.write(render(config, project))
    except ConfigError as e:
        print("note: config problem: " + "; ".join(e.problems), file=sys.stderr)
        return 1
    except NoteError as e:
        print(f"note: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
