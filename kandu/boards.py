"""Creates a board with Kandu's standard columns (DESIGN §3).

Only run when the user has said so; `/kandu-add` asks first.
"""
import argparse
import sys

from .config import BOARD_COLUMNS, BOARD_FIELDS, DEADLINE_FIELD, DEFAULT_LOCAL_DIR, Board, ConfigError, load_config
from .github import GitHub, GitHubError


def missing_fields(found):
    """Returns the task fields a board lacks."""
    return [name for name in (*BOARD_FIELDS, DEADLINE_FIELD) if name not in found]


def missing_columns(board, found):
    """Returns the standard columns a board lacks, after its name mapping."""
    return [c for c in BOARD_COLUMNS if board.columns.get(c, c) not in found]


def main():
    parser = argparse.ArgumentParser(description="Create a board with Kandu's columns, or add those a board lacks.")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--title", help="create a board with this title")
    parser.add_argument("--add-missing", type=int, metavar="NUMBER", help="add the missing columns to this board")
    parser.add_argument("--local", default=DEFAULT_LOCAL_DIR, help="config directory (default: local/)")
    args = parser.parse_args()
    if bool(args.title) == bool(args.add_missing):
        parser.error("give --title or --add-missing")
    try:
        github = GitHub()
        if args.title:
            print(github.create_board(args.owner, args.title, BOARD_COLUMNS))
            return 0
        boards = (p.board for p in load_config(args.local).projects if p.board is not None)
        board = next((b for b in boards if (b.owner, b.number) == (args.owner, args.add_missing)),
                     Board(owner=args.owner, number=args.add_missing))
        missing = missing_columns(board, github.board_columns(board))
        if missing:
            github.add_columns(board, missing)
        print(", ".join(missing) or "nothing is missing")
    except ConfigError as e:
        print(f"boards: {len(e.problems)} problem(s) in config; run: python -m kandu.config", file=sys.stderr)
        return 1
    except GitHubError as e:
        print(f"boards: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
