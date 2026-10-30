"""Opens Claude Code in the Didban role (DESIGN §13).

The role text is public; the details of one setup are read from
local/didban.yaml at start and passed to the session, never written to the repo.
"""
import os
import shlex
import subprocess
import sys
from pathlib import Path

from .config import ConfigError, load_didban

REPO_DIR = Path(__file__).resolve().parent.parent
ROLE_FILE = REPO_DIR / "prompts" / "didban.md"
BLOCKED_TOOLS = "Edit,Write,NotebookEdit"


def remote_path(path):
    """Quotes a path for a remote shell, keeping a leading `~` meaningful."""
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def check_brain(brain):
    """Returns (reachable, message) from Kandu's config check on the brain's machine."""
    ssh, *target = shlex.split(brain.reach)
    command = f"cd {remote_path(brain.install_dir)} && .venv/bin/python -m kandu.config"
    args = [ssh, "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", *target, command]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return False, "no answer in time"
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output.splitlines()[-1] if output else "no output"


def system_prompt(didban, status):
    return "\n".join([
        ROLE_FILE.read_text(encoding="utf-8"),
        "## This setup",
        f"- Reach the brain's machine with: `{didban.brain.reach}`",
        f"- Kandu is installed there at: `{didban.brain.install_dir}`",
        f"- Check at session start: {status}",
    ])


def claude_args(didban, status, extra):
    return [
        "claude", *extra,
        "--name", "didban",
        "--append-system-prompt", system_prompt(didban, status),
        "--disallowedTools", BLOCKED_TOOLS,
    ]


def main():
    try:
        didban = load_didban()
    except ConfigError as e:
        print("didban: config problem:", file=sys.stderr)
        for problem in e.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    reachable, message = check_brain(didban.brain)
    if reachable:
        status = f"brain reachable; {message}"
    else:
        status = f"brain NOT reachable; {message}. Tell the user live state is unavailable."
        print(f"didban: warning: {didban.brain.reach} is not reachable: {message}", file=sys.stderr)

    os.chdir(REPO_DIR)
    os.execvp("claude", claude_args(didban, status, sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
