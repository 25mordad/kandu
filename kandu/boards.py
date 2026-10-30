"""Creates a board with Kandu's standard columns (DESIGN §3).

Only run when the user has said so; `/kandu-add` asks first.
"""
import argparse
import sys

from .config import BOARD_COLUMNS, BOARD_FIELDS, DEADLINE_FIELD
from .github import GitHub, GitHubError


def missing_fields(found):
    """Returns the task fields a board lacks."""
    return [name for name in (*BOARD_FIELDS, DEADLINE_FIELD) if name not in found]


def missing_columns(board, found):
    """Returns the standard columns a board lacks, after its name mapping."""
    return [c for c in BOARD_COLUMNS if board.columns.get(c, c) not in found]


def main():
    parser = argparse.ArgumentParser(description="Create a board with Kandu's columns.")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--title", required=True)
    args = parser.parse_args()
    try:
        print(GitHub().create_board(args.owner, args.title, BOARD_COLUMNS))
    except GitHubError as e:
        print(f"boards: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
