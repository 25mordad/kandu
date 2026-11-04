"""Starts one of Kandu's modules the way a `systemd` unit does: from the install folder, with `local/.env`.

Task Scheduler on Windows has no `EnvironmentFile` and keeps no journal, so this does both:

    .venv/Scripts/pythonw.exe -X utf8 -m kandu.launch supervisor cycle

reads `local/.env` and appends what the module prints to `logs/<module>.log`.
"""
import os
import runpy
import sys
from datetime import datetime
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
MODULES = ("supervisor", "sync", "dashboard")


def read_env(path):
    """The NAME=value lines of an env file, as `systemd` reads them: no shell, quotes taken off."""
    values = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in MODULES:
        print(f"usage: python -m kandu.launch {{{','.join(MODULES)}}} [args]", file=sys.stderr)
        return 2
    module, args = argv[0], argv[1:]
    os.chdir(INSTALL_DIR)
    os.environ.update(read_env(INSTALL_DIR / "local" / ".env"))
    logs = INSTALL_DIR / "logs"
    logs.mkdir(mode=0o700, exist_ok=True)
    log = open(logs / f"{module}.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log
    print(f"--- {datetime.now().astimezone().isoformat(timespec='seconds')} {module} {' '.join(args)}")
    sys.argv = [f"kandu.{module}", *args]
    try:
        runpy.run_module(f"kandu.{module}", run_name="__main__", alter_sys=True)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
