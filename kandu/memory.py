"""What Kandu learned about a project, kept on the brain: its memory, `local/memory/PROJECT.md`, and Didban's brief,
`local/brief/PROJECT.md` (DESIGN §14).

A removed project's files move to an `archive/` folder beside them, never deleted, and come back when the project
is added again.
"""
from pathlib import Path

KINDS = ("memory", "brief")  # each is a folder in `local/`


def path(local_dir, name, kind="memory"):
    return Path(local_dir) / kind / f"{name}.md"


def archived_path(local_dir, name, kind="memory"):
    return Path(local_dir) / kind / "archive" / f"{name}.md"


def read(local_dir, name, kind="memory"):
    """The project's file of this kind, or None when there is none or it is empty."""
    found = path(local_dir, name, kind)
    text = found.read_text(encoding="utf-8") if found.exists() else ""
    return text if text.strip() else None


def _join(first, second):
    return first.rstrip("\n") + "\n\n" + second if first.strip() else second


def _move(source, target, older_first=True):
    """Moves `source` to `target`. When `target` is there already, both texts stay, the older one first: `target`'s
    when archiving, `source`'s when restoring (`older_first=False`)."""
    if not source.exists():
        return None
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.exists():
        texts = [target.read_text(encoding="utf-8"), source.read_text(encoding="utf-8")]
        target.write_text(_join(*(texts if older_first else texts[::-1])), encoding="utf-8")
        source.unlink()
    else:
        source.replace(target)
    return target


def archive(local_dir, name):
    """Moves each of the project's files into its archive. Returns the archived paths."""
    moved = (_move(path(local_dir, name, kind), archived_path(local_dir, name, kind)) for kind in KINDS)
    return [str(p) for p in moved if p]


def restore(local_dir, name):
    """Brings archived files back for a project added again; anything written meanwhile comes after the old text.
    Returns the restored paths."""
    moved = (_move(archived_path(local_dir, name, kind), path(local_dir, name, kind), older_first=False)
             for kind in KINDS)
    return [str(p) for p in moved if p]


def offered_text(local_dir, name):
    """One file with what Kandu learned about the project, as offered to its own repo; None when there is nothing."""
    memory, brief = read(local_dir, name), read(local_dir, name, "brief")
    if memory is None and brief is None:
        return None
    parts = [f"# What Kandu learned about {name}\n"]
    if memory is not None:
        parts.append("## Memory\n\n" + memory.strip() + "\n")
    if brief is not None:
        parts.append("## Brief\n\n" + brief.strip() + "\n")
    return "\n".join(parts)
