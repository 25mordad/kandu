"""Gives a project a new name, in one step that is either complete or fully undone (DESIGN §1).

After it, Kandu knows the project only by the new name: its entry in `projects.yaml` (and `upstream.project` in
`kandu.yaml`, when it names it), its memory, brief and Bazras reports in `local/`, and in `state/` its pause, the
settings the dashboard chose and its open requests. The event log is never edited: the rename is kept in
`state/renames.json`, and each event written before it is read under the new name, so its token history and its
events count as one project. Its copy, repo, board and issues stay as they are.

A rename is refused while a supervisor cycle runs, or a worker or a free flight runs on the project, and when the new
name is empty, is not lowercase letters, digits and dashes, or is another project's.
"""
import fcntl
import os
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import yaml

from . import memory
from .add import PROJECT_NAME, AddError, _validate_copy, _write
from .config import DEFAULT_LOCAL_DIR, load_config

RUNNING = "stop or wait for the running work first"
EMPTY = "give the project a name"
NOT_A_NAME = "a project's name uses only lowercase letters, digits and dashes, and does not start with a dash"
TAKEN = "another project already has this name"
KEPT = "Kandu still keeps a memory, brief or Bazras report under this name"


class RenameError(Exception):
    pass


def _entry_line(name):
    return re.compile(rf"^(\s*-\s+name:\s*)([\"']?){re.escape(name)}\2(\s*(#.*)?)$")


def projects_text(text, old, new):
    """`projects.yaml` with the entry named `old` named `new`; every other line stays as it was."""
    lines = text.split("\n")
    hits = [i for i, line in enumerate(lines) if _entry_line(old).match(line)]
    if len(hits) != 1:
        raise RenameError(f"the entry of {old} in projects.yaml could not be found; rename it there by hand")
    lines[hits[0]] = _entry_line(old).sub(lambda m: f"{m.group(1)}{m.group(2)}{new}{m.group(2)}{m.group(3)}",
                                          lines[hits[0]])
    return "\n".join(lines)


def kandu_text(text, old, new):
    """`kandu.yaml` with `upstream.project` following the rename, or None when it does not name the project."""
    if ((yaml.safe_load(text) or {}).get("upstream") or {}).get("project") != old:
        return None
    line = re.compile(rf"^(\s+project:\s*)([\"']?){re.escape(old)}\2(\s*(#.*)?)$", re.MULTILINE)
    text, found = line.subn(lambda m: f"{m.group(1)}{m.group(2)}{new}{m.group(2)}{m.group(3)}", text)
    if found != 1:
        raise RenameError(f"upstream.project in kandu.yaml names {old}, but could not be changed; change it by hand")
    return text


def running(store, name):
    """True while a worker or a free flight runs on the project."""
    workers = any(w.get("project") == name for w in store.read_current().get("workers", []))
    return workers or any(f.get("kind") == "task" and f.get("project") == name for f in store.flights())


@contextmanager
def _no_cycle(store):
    """Holds `state/supervisor.lock`, the lock a cycle holds while it runs, so none starts during the rename.

    A cycle reads `projects.yaml` once when it wakes and may work on the project at any step after, under its old
    name. Raises RenameError(RUNNING) while one runs.
    """
    store.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(store.root / "supervisor.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RenameError(RUNNING) from None
        yield


def _places(local_dir, name):
    """What Kandu keeps of the project in `local/`, by its name."""
    local_dir = Path(local_dir)
    return [memory.path(local_dir, name, kind) for kind in memory.KINDS] + [local_dir / "reports" / "bazras" / name]


class _Steps:
    """The steps done so far, each with its undo, so a failure leaves everything as it was."""

    def __init__(self):
        self.undo = []

    def do(self, step, undo):
        step()
        self.undo.append(undo)

    def roll_back(self):
        for undo in reversed(self.undo):
            undo()


def _set_paused(store, name, paused):
    (store.pause_project if paused else store.resume_project)(name)


def rename_project(old, new, store, local_dir=DEFAULT_LOCAL_DIR, now=None):
    """Renames the project `old` to `new`. Raises RenameError, with nothing changed, when it may not or it fails."""
    local_dir = Path(local_dir)
    config = load_config(local_dir)
    if not any(p.name == old for p in config.projects):
        raise RenameError(f"there is no project named {old!r} in Kandu")
    if not new.strip():
        raise RenameError(EMPTY)
    if not PROJECT_NAME.fullmatch(new):
        raise RenameError(NOT_A_NAME)
    if new == old:
        return {"name": old}
    if any(p.name == new for p in config.projects):
        raise RenameError(TAKEN)
    with _no_cycle(store):
        if running(store, old):
            raise RenameError(RUNNING)
        return _rename(old, new, store, local_dir, now)


def _rename(old, new, store, local_dir, now):
    """The rename itself, once nothing runs on the project and no cycle can start."""
    if any(place.exists() for place in _places(local_dir, new)):
        raise RenameError(KEPT)

    registry_path, settings_path = local_dir / "projects.yaml", local_dir / "kandu.yaml"
    registry_was, settings_was = (path.read_text(encoding="utf-8") for path in (registry_path, settings_path))
    registry, settings = projects_text(registry_was, old, new), kandu_text(settings_was, old, new)
    try:
        _validate_copy(local_dir, kandu_text=settings, projects_text=registry)
    except AddError as e:
        raise RenameError(f"the config would not pass its check after the rename: {e}") from None

    at = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    steps = _Steps()
    try:
        for source, target in zip(_places(local_dir, old), _places(local_dir, new)):
            if source.exists():
                steps.do(lambda s=source, t=target: os.rename(s, t), lambda s=source, t=target: os.rename(t, s))
        brief = memory.path(local_dir, new, "brief")
        if brief.exists():
            text = brief.read_text(encoding="utf-8")
            if text.startswith(f"# {old}\n"):  # its title, as `kandu.brief` writes it
                steps.do(lambda: _write(brief, f"# {new}\n" + text[len(f'# {old}\n'):]),
                         lambda: _write(brief, text))

        paused = {name: name in store.paused_projects() for name in (old, new)}
        steps.do(lambda: (_set_paused(store, new, paused[old]), _set_paused(store, old, False)),
                 lambda: [_set_paused(store, name, value) for name, value in paused.items()])

        chosen = store.project_settings()
        moved = {new if k == old else k: v for k, v in chosen.items() if k != new}  # in its place
        steps.do(lambda: store.write_project_settings(moved), lambda: store.write_project_settings(chosen))

        for request in store.requests():
            if request.get("project") == old:
                steps.do(lambda r=request: store.write_request(r | {"project": new}),
                         lambda r=request: store.write_request(r))

        renames = store.renames()
        steps.do(lambda: store.write_renames(renames + [{"old": old, "new": new, "at": at}]),
                 lambda: store.write_renames(renames))

        steps.do(lambda: _write(registry_path, registry), lambda: _write(registry_path, registry_was))
        if settings is not None:
            steps.do(lambda: _write(settings_path, settings), lambda: _write(settings_path, settings_was))

        store.record("project_renamed", now=datetime.fromisoformat(at), old=old, new=new)
    except Exception as e:
        steps.roll_back()
        raise RenameError(f"{old} could not be renamed: {e}; it keeps its name and everything stays as it was") from None
    return {"name": new, "old": old}

