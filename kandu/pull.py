"""Brings a live copy up to date for a deploy, or leaves it exactly as it was (DESIGN §5).

A plain `git pull` that conflicts leaves conflict markers in a live copy. This
runs the pull, and if it fails mid-merge or mid-rebase it aborts that first, so
the deploy fails with the copy untouched. A deploy runs in its own folder, so
this is run as a file, and it needs nothing but Python's standard library:

    <kandu>/.venv/bin/python -I <kandu>/kandu/pull.py ~/projects/site                      # --no-rebase --no-edit -q
    <kandu>/.venv/bin/python -I <kandu>/kandu/pull.py ~/projects/site --rebase --autostash  # other pull options
"""
import subprocess
import sys
from pathlib import Path

DEFAULT_OPTIONS = ["--no-rebase", "--no-edit", "-q"]


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")


def _git_dir(path):
    found = _git(path, "rev-parse", "--absolute-git-dir")
    return Path(found.stdout.strip()) if found.returncode == 0 else None


def _undo(path, git_dir):
    """Aborts a merge or rebase the pull left half done. Returns what it aborted, or None."""
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        _git(path, "rebase", "--abort")
        return "rebase"
    if (git_dir / "MERGE_HEAD").exists():
        _git(path, "merge", "--abort")
        return "merge"
    return None


def pull(path, options=()):
    """Returns (exit code, a line for the deploy's log)."""
    git_dir = _git_dir(path)
    if git_dir is None:
        return 2, f"pull: {path} is not a git checkout"
    result = _git(path, "pull", *(options or DEFAULT_OPTIONS))
    if result.returncode == 0:
        return 0, "pull: up to date"
    undone = _undo(path, git_dir)
    detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
    if undone:
        return 1, f"pull: it conflicted, so the {undone} was aborted and the copy is as it was. {detail[0]}"
    return 1, f"pull: it failed. {detail[0]}"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0].startswith("-"):
        print("usage: python -I kandu/pull.py <checkout> [git pull options]", file=sys.stderr)
        return 2
    code, line = pull(Path(argv[0]).expanduser(), argv[1:])
    print(line, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
