"""What each agent is given: its prompt and its permissions (DESIGN §5, §9, §14).

Permissions are set here, in code, per run. An agent gets only the context it
needs: the facts of one task, never the history of others.
"""
import json
from pathlib import Path

from .config import DEFAULT_LOCAL_DIR

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
ALWAYS_DENIED = ["Read(~/.ssh/**)", "Read(~/.claude/.credentials.json)", "Read(**/.env)", "Bash(git push --force*)",
                 "Bash(git push -f*)"]
WORKER_STATUSES = ("pr_opened", "needs_info", "blocked")
LIVE_RESULTS = ("pass", "fail")
QUEEN_ACTIONS = ("release", "retry", "ask_user")


class ReplyError(Exception):
    pass


def _command(text):
    return [f"Bash({text})", f"Bash({text} *)"]


def worker_tools(project, branch):
    """Returns (allowed, denied) for a worker on `branch`."""
    allowed = ["Read", "Glob", "Grep", "Edit", "Write"]
    for command in ("git add", "git rm", "rm", "git status", "git diff", "git log", "git commit", "gh pr create",
                    "gh pr view", "gh pr comment", "gh issue view", project.test):
        allowed += _command(command)
    allowed += [f"Bash(git push origin {branch})", f"Bash(git push -u origin {branch})"]
    allowed += project.allowed_tools
    denied = list(ALWAYS_DENIED)
    for area in project.forbidden:
        pattern = f"./{area.rstrip('/')}/**" if area.endswith("/") else f"./{area}"
        denied += [f"Edit({pattern})", f"Write({pattern})"]
    return allowed, denied


def reviewer_tools():
    """Returns (allowed, denied) for a reviewer, who reads and changes nothing."""
    allowed = ["Read", "Glob", "Grep"]
    for command in ("gh pr diff", "gh pr view", "gh issue view", "git log", "git diff"):
        allowed += _command(command)
    return allowed, ["Edit", "Write", "NotebookEdit"] + ALWAYS_DENIED


def live_check_tools(project):
    """Returns (allowed, denied) for a live check, which looks and changes nothing."""
    allowed = ["Read", "Glob", "Grep", "WebFetch"] + _command("curl") + _command("gh issue view") + project.allowed_tools
    return allowed, ["Edit", "Write", "NotebookEdit"] + ALWAYS_DENIED


def queen_tools():
    """Returns (allowed, denied) for the queen, who reads and changes nothing."""
    allowed = ["Read", "Glob", "Grep"]
    for command in ("gh pr diff", "gh pr view", "gh issue view"):
        allowed += _command(command)
    return allowed, ["Edit", "Write", "NotebookEdit", "Read(./local/**)"] + ALWAYS_DENIED


def _read(name):
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _memory(project, local_dir):
    path = Path(local_dir) / "memory" / f"{project.name}.md"
    if not path.exists():
        return []
    return ["## Project memory", path.read_text(encoding="utf-8")]


def _project_facts(project):
    facts = [f"- Test command: `{project.test}`"]
    if project.forbidden:
        facts.append(f"- Forbidden areas: {', '.join(project.forbidden)}")
    if project.repo_rules:
        facts.append(f"- Repo rules: {project.repo_rules}")
    return facts


def worker_prompt(project, repo, number, branch, run_id, findings=None, pr=None, local_dir=DEFAULT_LOCAL_DIR,
                  queen_note=None):
    """The first run gets the PR template; a fix round gets the open findings instead."""
    facts = [f"- Repo: `{repo}`", f"- Issue: #{number}", f"- Your branch: `{branch}`",
             f"- For the `Kandu-Run:` line: `{run_id}`", *_project_facts(project)]
    parts = [_read("worker.md"), "## This task", *facts, *_memory(project, local_dir)]
    if queen_note:
        parts += ["## Note from the queen", "This task stopped once. She looked at why:", queen_note]
    if findings is None:
        parts += ["## PR template", _read("templates/pr.md")]
    else:
        parts += [f"## Review findings on PR #{pr}", "A review came back. Push your fixes to the same branch.",
                  "```json", json.dumps(findings, ensure_ascii=False, indent=2), "```"]
    return "\n".join(parts)


def reviewer_prompt(project, repo, number, pr, head_sha, companions, still_open, first_n,
                    local_dir=DEFAULT_LOCAL_DIR):
    facts = [f"- Repo: `{repo}`", f"- Issue: #{number}", f"- PR: #{pr}", f"- Head SHA: `{head_sha}`",
             f"- Diff: `gh pr diff {pr} --repo {repo}`",
             f"- Number new findings from {first_n}", *_project_facts(project)]
    parts = [_read("reviewer.md"), "## This PR", *facts, *_memory(project, local_dir)]
    if companions:
        parts += ["## Companion PRs", *[f"- {other}#{n}" for other, n in companions]]
    if still_open:
        parts += ["## Findings of the earlier rounds", "```json",
                  json.dumps(still_open, ensure_ascii=False, indent=2), "```"]
    return "\n".join(parts)


def queen_prompt(events, projects, retries, retry_limit, language, logs_dir, today):
    facts = [f"- Today: {today}", f"- Language of the brief: {language}", f"- Logs folder: `{logs_dir}`",
             f"- Retries a task may have: {retry_limit}"]
    parts = [_read("queen.md"), "## This look", *facts, "## Projects", "```json",
             json.dumps(projects, ensure_ascii=False, indent=2), "```", "## Retries used", "```json",
             json.dumps(retries, ensure_ascii=False), "```", "## Events since your last look", "```json",
             json.dumps(events, ensure_ascii=False, indent=2), "```"]
    return "\n".join(parts)


def live_check_prompt(project, repo, number, local_dir=DEFAULT_LOCAL_DIR):
    facts = [f"- Repo: `{repo}`", f"- Issue: #{number}", f"- Live URL: {project.deploy.live_url}"]
    return "\n".join([_read("live-check.md"), "## This check", *facts, *_memory(project, local_dir)])


def parse_live_check(text):
    """Takes a live check's last message; returns its result object."""
    try:
        reply = json.loads(text.strip())
    except json.JSONDecodeError as e:
        raise ReplyError("live check reply is not JSON") from e
    if not isinstance(reply, dict) or reply.get("result") not in LIVE_RESULTS:
        raise ReplyError("live check reply has no known result")
    if not isinstance(reply.get("summary"), str) or not reply["summary"].strip():
        raise ReplyError("live check reply has no summary")
    return reply


def parse_queen_report(text):
    """Takes the queen's last message; returns her brief and decisions."""
    try:
        report = json.loads(text.strip())
    except json.JSONDecodeError as e:
        raise ReplyError("queen reply is not JSON") from e
    if not isinstance(report, dict) or not isinstance(report.get("brief"), str) or not report["brief"].strip():
        raise ReplyError("queen reply has no brief")
    decisions = report.get("decisions")
    if not isinstance(decisions, list):
        raise ReplyError("queen reply has no decisions")
    for decision in decisions:
        if not isinstance(decision, dict) or decision.get("action") not in QUEEN_ACTIONS:
            raise ReplyError("queen decision has no known action")
        if not isinstance(decision.get("task"), str) or not isinstance(decision.get("note"), str) \
                or not decision["note"].strip():
            raise ReplyError("queen decision has no task or note")
    return report


def parse_worker_reply(text):
    """Takes a worker's last message; returns the status object it reports."""
    try:
        reply = json.loads(text.strip())
    except json.JSONDecodeError as e:
        raise ReplyError("worker reply is not JSON") from e
    if not isinstance(reply, dict) or reply.get("status") not in WORKER_STATUSES:
        raise ReplyError("worker reply has no known status")
    if reply["status"] == "needs_info" and not isinstance(reply.get("question"), str):
        raise ReplyError("needs_info has no question")
    if reply["status"] == "blocked" and not isinstance(reply.get("reason"), str):
        raise ReplyError("blocked has no reason")
    return reply
