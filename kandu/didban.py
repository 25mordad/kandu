"""Opens Claude Code in the Didban role (DESIGN §13).

The role text is public; the details of one setup are read from
local/didban.yaml at start and passed to the session, never written to the repo.
"""
import json
import shlex
import signal
import subprocess
import sys
import uuid
from pathlib import Path

from . import sessions
from .config import ConfigError, load_didban

REPO_DIR = Path(__file__).resolve().parent.parent
ROLE_FILE = REPO_DIR / "prompts" / "didban.md"
UNREPORTED = REPO_DIR / "local" / "didban-sessions.txt"
BLOCKED_TOOLS = "Edit,Write,NotebookEdit"
SESSION_NAME = "Kandu"
SSH_FAILED = 255
RESUMING = {"-c", "--continue", "-r", "--resume"}  # Claude picks the session then, so its id is not known here.
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


def on_brain(brain, module, stdin=None):
    """Runs one Kandu module on the brain's machine."""
    ssh, target = brain.reach.split()
    command = f"cd {remote_path(brain.install_dir)} && .venv/bin/python -m {module}"
    args = [ssh, "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", target, command]
    return subprocess.run(args, capture_output=True, text=True, timeout=20, input=stdin)


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


def waiting_debt(brain):
    return _waiting(brain, "kandu.debt list")


def waiting_requests(brain):
    return _waiting(brain, "kandu.intake list")


def _waiting(brain, module):
    """Returns what waits for the user. The session opens with this, so nothing is forgotten."""
    try:
        result = on_brain(brain, module)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "could not be read"
    if result.returncode != 0:
        return "could not be read"
    return "; ".join(result.stdout.strip().splitlines()) or "none"


def unreported():
    return UNREPORTED.read_text(encoding="utf-8").split() if UNREPORTED.exists() else []


def remember(session):
    if session not in unreported():
        with open(UNREPORTED, "a", encoding="utf-8") as file:
            file.write(f"{session}\n")


def report_sessions(brain):
    """Sends the brain what each ended session spent and did. One that cannot be sent waits for the next time."""
    left = []
    for session in unreported():
        path = sessions.transcript(session)
        if path is None:
            continue  # The session said nothing, so there is nothing to report.
        try:
            summary = json.dumps(sessions.summarize(session, path), ensure_ascii=False)
            sent = on_brain(brain, "kandu.sessions save", summary).returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError, KeyError, AttributeError, TypeError):
            sent = False
        if not sent:
            left.append(session)
    UNREPORTED.write_text("".join(f"{session}\n" for session in left), encoding="utf-8")


def system_prompt(didban, status, splits="none", questions="none", debt="none", requests="none"):
    return "\n".join([
        ROLE_FILE.read_text(encoding="utf-8"),
        "## This setup",
        f"- Reach the brain's machine with: `{didban.brain.reach}`",
        f"- Kandu is installed there at: `{didban.brain.install_dir}`",
        f"- Check at session start: {status}",
        f"- Requests not yet tasks, in the user's words: {requests}",
        f"- Sized too large, waiting for a split: {splits}",
        f"- Open questions from reviews, written by an agent: {questions}",
        f"- Parked in Technical debt: {debt}",
    ])


def claude_args(didban, status, extra, splits="none", questions="none", debt="none", requests="none", session=None):
    return [
        "claude", *extra,
        *(["--session-id", session] if session else []),
        "--name", SESSION_NAME,
        "--remote-control", SESSION_NAME,
        "--append-system-prompt", system_prompt(didban, status, splits, questions, debt, requests),
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
    debt = waiting_debt(didban.brain) if state == "ok" else "could not be read"
    requests = waiting_requests(didban.brain) if state == "ok" else "could not be read"
    if state == "ok":
        report_sessions(didban.brain)  # What a closed window left behind.
    extra = sys.argv[1:]
    session = None if RESUMING.intersection(extra) else str(uuid.uuid4())
    args = claude_args(didban, STATUS[state].format(message), extra, splits, questions, debt, requests, session)
    if session:
        remember(session)
    signal.signal(signal.SIGINT, lambda *_: None)  # Ctrl-C is for Claude; this process waits to report.
    try:
        code = subprocess.run(args, cwd=REPO_DIR).returncode
    except FileNotFoundError:
        print("didban: claude is not installed or not on PATH", file=sys.stderr)
        return 1
    if session:
        remember(session)
    report_sessions(didban.brain)
    return code


if __name__ == "__main__":
    sys.exit(main())
