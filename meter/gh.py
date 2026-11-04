"""The `meter/gh` script for Windows, where Python cannot start a bash script by name.

Counts one `gh` call in state/gh/<day>.jsonl, then runs the real `gh` (docs/STATE.md). Only the
caller, the run and the first two words are kept: never a body, a query or a token. It writes the
same line as `meter/gh`. Run it with `python -I`, so it imports nothing from this folder.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAX_DEPTH = 4  # As in `meter/gh`: a `gh` extension may call `gh` again, but never without end.
REST_ONLY = {"run", "release", "workflow", "secret", "variable", "cache", "gist", "ssh-key", "gpg-key", "auth",
             "attestation", "extension", "alias", "config", "ruleset"}


def real_gh(path):
    """The first `gh` on PATH that is not this meter."""
    for folder in path.split(os.pathsep):
        if not folder or Path(folder).resolve() == HERE:
            continue
        found = shutil.which("gh", path=folder)
        if found and Path(found).resolve().parent != HERE:
            return found
    return None


def word(text):
    return re.sub(r"[^a-z-]", "", text)


def safe(text):
    return re.sub(r"[^A-Za-z0-9:_.-]", "", text)


def line(args, code, environ, now):
    first = word(args[0]) if args else ""
    second = args[1] if len(args) > 1 else ""
    if first == "api":
        command, kind = ("api graphql", "graphql") if second == "graphql" else ("api rest", "rest")
    else:
        command = f"{first} {word(second)}".rstrip()
        kind = "rest" if first in REST_ONLY else "graphql"
    return {"v": 1, "ts": now.isoformat(timespec="seconds"), "type": "call",
            "caller": safe(environ.get("GH_METER_CALLER") or "other"), "run": safe(environ.get("GH_METER_RUN", "")),
            "command": command, "kind": kind, "exit": code}


def main(args):
    depth = int(os.environ.get("GH_METER_DEPTH") or 0) + 1
    if depth > MAX_DEPTH:
        print("gh: the meter found itself instead of the real gh", file=sys.stderr)
        return 127
    os.environ["GH_METER_DEPTH"] = str(depth)
    real = real_gh(os.environ.get("PATH", ""))
    if real is None:
        print("gh: the real gh is not on PATH", file=sys.stderr)
        return 127
    code = subprocess.run([real, *args]).returncode
    now = datetime.now().astimezone()
    log = Path(os.environ.get("GH_METER_LOG") or HERE.parent / "state" / "gh")
    try:
        log.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(log / f"{now.date().isoformat()}.jsonl", "a", encoding="utf-8", newline="\n") as out:
            out.write(json.dumps(line(args, code, os.environ, now)) + "\n")
    except OSError:
        pass
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
