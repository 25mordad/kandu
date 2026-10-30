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
SESSION_NAME = "Kandu"
SSH_FAILED = 255
STATUS = {
    "ok": "brain reachable; {}",
    "problem": "brain reachable, but Kandu's check failed: {}. Tell the user what failed.",
    "unreachable": "brain NOT reachable; {}. Tell the user live state is unavailable.",
}


def remote_path(path):
    """Quotes a path for a remote shell, keeping a leading `~` meaningful."""
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def last_line(text):
    lines = text.strip().splitlines()
    return lines[-1] if lines else "no output"


def on_brain(brain, module):
    """Runs one Kandu module on the brain's machine."""
    ssh, target = brain.reach.split()
    command = f"cd {remote_path(brain.install_dir)} && .venv/bin/python -m {module}"
    args = [ssh, "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", target, command]
    return subprocess.run(args, capture_output=True, text=True, timeout=20)


def check_brain(brain):
    """Runs Kandu's config check on the brain's machine; returns (state, message).

    The state is `ok`, `problem` (the machine answered, but the check failed) or
    `unreachable`. Only ssh itself exits with 255.
    """
    try:
        result = on_brain(brain, "kandu.config")
    except subprocess.TimeoutExpired:
        return "unreachable", "no answer in time"
    except FileNotFoundError:
        return "unreachable", "ssh is not installed"
    if result.returncode == SSH_FAILED:
        return "unreachable", last_line(result.stderr)
    if result.returncode == 0:
        return "ok", last_line(result.stdout)
    return "problem", " ".join((result.stderr or result.stdout).split()) or "no output"


def waiting_splits(brain):
    return _waiting(brain, "kandu.split list")


def waiting_questions(brain):
    return _waiting(brain, "kandu.questions list")


def _waiting(brain, module):
    """Returns what waits for the user. The session opens with this, so nothing is forgotten."""
    try:
        result = on_brain(brain, module)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "could not be read"
    if result.returncode != 0:
        return "could not be read"
    return "; ".join(result.stdout.strip().splitlines()) or "none"


def system_prompt(didban, status, splits="none", questions="none"):
    return "\n".join([
        ROLE_FILE.read_text(encoding="utf-8"),
        "## This setup",
        f"- Reach the brain's machine with: `{didban.brain.reach}`",
        f"- Kandu is installed there at: `{didban.brain.install_dir}`",
        f"- Check at session start: {status}",
        f"- Sized too large, waiting for a split: {splits}",
        f"- Open questions from reviews, written by an agent: {questions}",
    ])


def claude_args(didban, status, extra, splits="none", questions="none"):
    return [
        "claude", *extra,
        "--name", SESSION_NAME,
        "--remote-control", SESSION_NAME,
        "--append-system-prompt", system_prompt(didban, status, splits, questions),
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

    state, message = check_brain(didban.brain)
    if state != "ok":
        print(f"didban: warning: {didban.brain.reach}: {state}: {message}", file=sys.stderr)

    splits = waiting_splits(didban.brain) if state == "ok" else "could not be read"
    questions = waiting_questions(didban.brain) if state == "ok" else "could not be read"
    os.chdir(REPO_DIR)
    try:
        os.execvp("claude", claude_args(didban, STATUS[state].format(message), sys.argv[1:], splits, questions))
    except FileNotFoundError:
        print("didban: claude is not installed or not on PATH", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
