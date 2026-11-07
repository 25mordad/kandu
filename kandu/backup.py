"""A daily copy of what Kandu cannot rebuild from Git: `local/` and `state/` (DESIGN §12).

A copy is written under a temporary name, read back whole, and only then given
its dated name; older copies go only after that. So a failed or partial backup
leaves every earlier copy as it was, and the newest good copy always stays.
The run logs (`logs/`) are left out. A copy holds `local/.env`, so it is
readable only by its owner; a failure is logged and told with a fixed text,
never a file's content.
"""
import argparse
import fcntl
import os
import re
import sys
import tarfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import DEFAULT_LOCAL_DIR, ConfigError, load_config
from .notify import notifier_for
from .state import StateStore

ROOT = DEFAULT_LOCAL_DIR.parent
SOURCES = ("local", "state")      # Everything else in the install comes from Git, or is a run log.
COPY = re.compile(r"kandu-(\d{4}-\d{2}-\d{2})-(\d{6})\.tar\.gz")
PARTIAL = ".partial"
LOCK = ".kandu-backup.lock"


class BackupError(Exception):
    """Holds a safe reason: it goes into the log and the dashboard, so never a file's content."""


@dataclass
class Made:
    copy: Path
    files: int
    removed: list[str] = field(default_factory=list)


def destination_of(settings):
    return Path(settings.destination).expanduser()


def copies(destination):
    """The good copies in `destination`, oldest first. A file not named as a copy is not one."""
    destination = Path(destination)
    if not destination.is_dir():
        return []
    return sorted(p for p in destination.iterdir() if COPY.fullmatch(p.name) and p.is_file())


def _inside(path, folder):
    return path == folder or folder in path.parents


def _entries(root):
    """Each folder and file of the sources, parents first, as (path, name in the copy)."""
    for source in SOURCES:
        top = root / source
        if not top.is_dir():
            if source == "local":
                raise BackupError("the config folder is missing")
            continue
        yield top, source
        for folder, dirs, files in os.walk(top):
            dirs.sort()
            for name in sorted(dirs) + sorted(files):
                path = Path(folder) / name
                yield path, path.relative_to(root).as_posix()


def _write(root, partial):
    """Writes the copy and returns what it holds, as {name: size, or None for a folder}."""
    held = {}
    with tarfile.open(partial, "w:gz") as archive:
        for path, name in _entries(root):
            try:
                info = archive.gettarinfo(path, name)
                if info is None or not (info.isfile() or info.isdir()):
                    continue  # A socket or a link is not something Kandu would rebuild from.
                if info.isdir():
                    archive.addfile(info)
                    held[name] = None
                    continue
                with open(path, "rb") as content:
                    archive.addfile(info, content)
                held[name] = info.size
            except FileNotFoundError:
                continue  # It went away while the copy was made, so there is nothing left to keep.
    return held


def _check(partial, held):
    """Reads the copy back whole, so a copy that would not restore is never called good."""
    found = {}
    with tarfile.open(partial, "r:gz") as archive:
        for member in archive:
            if member.isfile():
                with archive.extractfile(member) as content:
                    while content.read(1 << 20):
                        pass
                found[member.name] = member.size
            else:
                found[member.name] = None
    if found != held:
        raise BackupError("the copy did not read back the same")


def make(root, destination, keep, now=None):
    """Writes one dated copy of `root`'s sources into `destination`, then keeps the newest `keep`."""
    now = now or datetime.now().astimezone()
    root, destination = Path(root).resolve(), Path(destination).expanduser().resolve()
    if keep < 1:
        raise BackupError("keep must be at least 1")
    if any(_inside(destination, root / source) for source in SOURCES):
        raise BackupError("the destination is inside a folder it backs up")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(destination / LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("another backup is running") from None
        for left in destination.glob(f"kandu-*{PARTIAL}"):
            left.unlink()  # A crash's leftover, never a copy.
        name = f"kandu-{now.strftime('%Y-%m-%d-%H%M%S')}.tar.gz"
        partial = destination / (name + PARTIAL)
        try:
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.close(fd)
            held = _write(root, partial)
            _check(partial, held)
            with open(partial, "rb") as written:
                os.fsync(written.fileno())
            os.replace(partial, destination / name)
        except (OSError, tarfile.TarError) as e:
            partial.unlink(missing_ok=True)
            raise BackupError(_reason(e)) from None
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        good = copies(destination)
        removed = [old.name for old in good[:-keep] if old.name != name]
        for old in removed:
            (destination / old).unlink()
    return Made(destination / name, sum(1 for size in held.values() if size is not None), removed)


def restore(copy, target):
    """Puts one copy's `local/` and `state/` into `target`, which must be empty."""
    copy, target = Path(copy).expanduser(), Path(target).expanduser()
    if not COPY.fullmatch(copy.name) or not copy.is_file():
        raise BackupError("that is not a copy made by Kandu's backup")
    if target.exists() and any(target.iterdir()):
        raise BackupError("the folder to restore into must be empty")
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tarfile.open(copy, "r:gz") as archive:
        archive.extractall(target, filter="data")


def _reason(error):
    """What went wrong, in words that hold no file's content."""
    if isinstance(error, BackupError):
        return str(error)
    if isinstance(error, OSError) and error.strerror:
        return f"could not write the copy: {error.strerror}"
    return f"could not write the copy: {type(error).__name__}"


def due(settings, now):
    """On, its hour has come today, and today has no copy yet. A wake missed while the brain was off catches up."""
    if not settings.enabled or now.strftime("%H:%M") < settings.at:
        return False
    today = now.date().isoformat()
    return not any(COPY.fullmatch(p.name).group(1) == today for p in copies(destination_of(settings)))


def run(config, store, root=ROOT, now=None, force=False, destination=None):
    """Makes today's copy if it is due, or now with `force`; returns what was made, or None.

    `destination` replaces the config's, for a copy by hand. A failure is logged; the notifier tells it.
    """
    now = now or datetime.now().astimezone()
    settings = config.settings.backup
    if not force and not due(settings, now):
        return None
    try:
        made = make(root, destination or destination_of(settings), settings.keep, now)
    except (BackupError, OSError) as e:
        store.record("backup_failed", now=now, reason=_reason(e))
        return None
    store.record("backup_made", now=now, copy=made.copy.name, files=made.files, removed=made.removed)
    return made


def main():
    parser = argparse.ArgumentParser(description="Back up local/ and state/, list the copies, or restore one.")
    parser.add_argument("command", nargs="?", default="run", choices=("run", "now", "list", "restore"),
                        help="run: make today's copy if it is on and due; now: make one at once, even when off; "
                             "list: the copies, oldest first; restore COPY FOLDER: put a copy into an empty folder")
    parser.add_argument("paths", nargs="*", help="for restore: the copy, then the empty folder")
    parser.add_argument("--destination", help="for now: write the copy here instead of backup.destination")
    parser.add_argument("--local", default=DEFAULT_LOCAL_DIR, help="config directory (default: local/)")
    args = parser.parse_args()
    if args.command == "restore":
        if len(args.paths) != 2:
            print("backup: restore needs the copy and an empty folder", file=sys.stderr)
            return 1
        try:
            restore(*args.paths)
        except (BackupError, OSError, tarfile.TarError) as e:
            print(f"backup: {_reason(e)}", file=sys.stderr)
            return 1
        print(f"backup: restored into {args.paths[1]}")
        return 0
    try:
        config = load_config(args.local)
    except ConfigError as e:
        print(f"backup: {len(e.problems)} problem(s) in config; run: python -m kandu.config", file=sys.stderr)
        return 1
    if args.command == "list":
        for copy in copies(destination_of(config.settings.backup)):
            print(copy)
        return 0
    store = StateStore()
    notifier = notifier_for(config.settings, store)
    if notifier is not None:
        store.listeners.append(notifier.on_event)
    destination = Path(args.destination) if args.destination else None
    made = run(config, store, force=args.command == "now", destination=destination)
    if made is not None:
        print(f"backup: {made.copy} ({made.files} files; removed {len(made.removed)} old)")
        return 0
    failed = store.last_event("backup_failed")
    if args.command == "now" or (failed and failed["ts"][:10] == store.today()):
        print(f"backup: failed: {failed['reason'] if failed else 'unknown'}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
