"""What each agent is given: its prompt and its permissions (DESIGN §5, §9, §14).

Permissions come from the config (`permissions` in kandu.yaml) and are passed
per run; with none, a role may do everything. An agent gets only the context
it needs: the facts of one task, never the history of others.
"""
import json
from pathlib import Path

from . import sizing
from .config import BOARD_FIELDS, DEFAULT_LOCAL_DIR
from .review import CODE_FENCE

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
WORKER_STATUSES = ("pr_opened", "needs_info", "blocked")
LIVE_RESULTS = ("pass", "fail")
QUEEN_ACTIONS = ("release", "retry", "rework", "verify", "new_task", "block_on", "approve", "ask_user", "park")
CHECK_RESULTS = ("pass", "fail", "unclear")
NEW_TASK_FIELDS = ("title", "body", "priority", "kind", "size")
# Given to `claude`, so her reply is always one JSON object. parse_queen_report still checks the rest.
QUEEN_SCHEMA = {
    "type": "object",
    "properties": {
        "brief": {"type": "string"},
        "decisions": {"type": "array", "items": {
            "type": "object", "required": ["task", "action", "note"], "additionalProperties": False, "properties": {
                "task": {"type": "string"}, "action": {"enum": list(QUEEN_ACTIONS)}, "note": {"type": "string"},
                "finding": {"type": "integer"}, "head_sha": {"type": "string"}, "on": {"type": "string"},
                "blocks": {"type": "boolean"}, "title": {"type": "string"}, "body": {"type": "string"},
                **{name.lower(): {"enum": list(values)} for name, values in BOARD_FIELDS.items()}}}},
    },
    "required": ["brief", "decisions"],
    "additionalProperties": False,
}


# Given to `claude`, so a worker or a live check ends on one object. The parsers still check what a schema cannot.
_TEXT = {"type": "string"}
WORKER_SCHEMA = {
    "type": "object", "required": ["status"],
    "properties": {"status": {"enum": list(WORKER_STATUSES)}, "pr": {"type": "integer"}, "question": _TEXT,
                   "reason": _TEXT},
}
CHECK_SCHEMA = {
    "type": "object", "required": ["result", "summary"], "additionalProperties": False,
    "properties": {"result": {"enum": list(CHECK_RESULTS)}, "summary": _TEXT},
}
LIVE_CHECK_SCHEMA = {
    "type": "object", "required": ["result", "summary"], "additionalProperties": False,
    "properties": {"result": {"enum": list(LIVE_RESULTS)}, "summary": _TEXT},
}
_FACTS = {"size": {"enum": list(sizing.ORDER)}, "files": {"type": "integer"}, "door": _TEXT}
SPLIT_SCHEMA = {
    "type": "object", "required": ["size", "files", "door", "reason", "slices"],
    "properties": {**_FACTS, "reason": _TEXT,
                   "slices": {"type": "array", "items": {"type": "object", "required": ["title", "body", *_FACTS],
                                                         "properties": {**_FACTS, "title": _TEXT, "body": _TEXT}}},
                   "user_text": {"type": ["object", "null"]}},
}
NOTES_SCHEMA = {
    "type": "object", "required": ["notes"],
    "properties": {"notes": {"type": "array", "items": {
        "type": "object", "required": ["n", "title", "body", "reason", *_FACTS],
        "properties": {**_FACTS, "n": {"type": "integer"}, "title": _TEXT, "body": _TEXT, "reason": _TEXT}}}},
}


class ReplyError(Exception):
    pass


def reask_prompt(error):
    """Goes to the same session: the work is done, only its last message could not be read."""
    return (f"Kandu could not read your last message: {error}. Do not redo or change any work. "
            "Reply now with only the JSON object that the \"Last message\" part of your instructions asks for.")


def _limits(rules, extra_allowed=(), extra_denied=(), branch=""):
    """Returns (allowed, denied) for one run, from the role's rules in the config.

    With no rules the role may do everything: `allowed` is None, and only
    `extra_denied`, which also comes from the config, still holds.
    `{branch}` in a rule becomes the run's branch.
    """
    if rules is None:
        return None, list(extra_denied)
    allowed = [rule.replace("{branch}", branch) for rule in rules.allow] + list(extra_allowed)
    denied = [rule.replace("{branch}", branch) for rule in rules.deny] + list(extra_denied)
    return allowed, denied


def _forbidden(project):
    # Edit rules cover every tool that changes a file; Claude Code ignores Write(path).
    return [f"Edit(./{area.rstrip('/')}/**)" if area.endswith("/") else f"Edit(./{area})"
            for area in project.forbidden]


def worker_tools(project, branch, rules=None):
    """Returns (allowed, denied) for a worker on `branch`."""
    if rules is None:
        return _limits(None, extra_denied=_forbidden(project))
    allowed = [f"Bash({project.test})", f"Bash({project.test} *)"] + project.allowed_tools
    denied = []
    if project.secrets_from:
        path = project.secrets_from
        denied.append(f"Read({'/' + path if path.startswith('/') else path})")
    return _limits(rules, allowed, denied + _forbidden(project), branch)


def reviewer_tools(project, rules=None):
    """Returns (allowed, denied) for a reviewer, which runs the project's tests and commands itself."""
    if rules is None:
        return _limits(None)
    return _limits(rules, [f"Bash({project.test})", f"Bash({project.test} *)"] + project.allowed_tools)


def live_check_tools(project, rules=None):
    """Returns (allowed, denied) for a live check."""
    return _limits(rules, project.allowed_tools)


def role_tools(rules=None):
    """Returns (allowed, denied) for the reviewer, the splitter or the queen."""
    return _limits(rules)


def _read(name):
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _memory(project, local_dir):
    path = Path(local_dir) / "memory" / f"{project.name}.md"
    if not path.exists():
        return []
    return ["## Project memory", path.read_text(encoding="utf-8")]


def _project_facts(project):
    facts = [f"- Test command: `{project.test}`",
             "- Repo guides: `CLAUDE.md` and `AGENTS.md` are not loaded for you. If the repo has them, search them "
             "for what concerns this task and read those parts."]
    commands = [rule[len("Bash("):-1] for rule in project.allowed_tools if rule.startswith("Bash(")]
    if commands:
        facts.append(f"- The project's own commands: {', '.join(f'`{c}`' for c in commands)}")
    if project.forbidden:
        facts.append(f"- Forbidden areas: {', '.join(project.forbidden)}")
    if project.repo_rules:
        facts.append(f"- Repo rules: {project.repo_rules}")
    return facts


def _token_facts(project):
    names = "; ".join(f"`{name}`: {purpose}" for name, purpose in project.task_tokens.items())
    return [f"- Tokens in your environment: {names}. This task needs real requests with them, and being given this "
            "task is the owner's yes for what it asks. Nothing else needs asking."]


def worker_prompt(project, repo, number, branch, run_id, findings=None, pr=None, local_dir=DEFAULT_LOCAL_DIR,
                  queen_note=None, tokens=False, open_blockers=(), order=None):
    """The first run gets the PR template; a fix round gets what the last review left for it instead, and a
    worker the queen sends back to a PR gets her order."""
    facts = [f"- Repo: `{repo}`", f"- Issue: #{number}", f"- Your branch: `{branch}`",
             f"- For the `Kandu-Run:` line: `{run_id}`", *_project_facts(project)]
    if tokens:
        facts += _token_facts(project)
    parts = [_read("worker.md"), "## This task", *facts, *_memory(project, local_dir)]
    if open_blockers:
        parts += ["## Blockers not merged yet",
                  f"This task is blocked by {', '.join(open_blockers)}, whose work may still be in an open PR. "
                  "Build it now anyway: your PR merges only after theirs. If you need their code, merge their PR's "
                  "branch into yours first and say so in your PR."]
    if order is not None:
        return "\n".join(parts + [f"## Work the queen sends you on PR #{pr}",
                                  "The queen looked at this PR and sends you back to it. Do exactly this on the "
                                  "same branch, push, and end as after a fix round:", order])
    if queen_note:
        parts += ["## Note from the queen", "This task stopped once. She looked at why:", queen_note]
    if findings is None:
        parts += ["## PR template", _read("templates/pr.md")]
    else:
        parts += [f"## Review findings on PR #{pr}", "A review came back. Fix these and push to the same branch.",
                  "```json", json.dumps(findings, ensure_ascii=False, indent=2), "```"]
    return "\n".join(parts)


def reviewer_prompt(project, repo, number, pr, head_sha, language, companions, criteria, first_n,
                    local_dir=DEFAULT_LOCAL_DIR):
    facts = [f"- Repo: `{repo}`", f"- Issue: #{number}", f"- PR: #{pr}", f"- Head SHA: `{head_sha}`",
             f"- Diff: `gh pr diff {pr} --repo {repo}`", f"- Language of `user_brief`: {language}",
             f"- Number new findings from {first_n}", *_project_facts(project)]
    parts = [_read("reviewer.md"), "## This PR", *facts, *_memory(project, local_dir)]
    if criteria:
        parts += ["## Acceptance criteria", *[f"{i}. {text}" for i, text in enumerate(criteria, 1)]]
    if companions:
        parts += ["## Companion PRs", *[f"- {other}#{n}" for other, n in companions]]
    return "\n".join(parts)


def second_review_prompt(repo, number, pr, head_sha, language, items):
    """Only what the first review listed: this round needs no memory, criteria or companions."""
    facts = [f"- Repo: `{repo}`", f"- Issue: #{number}", f"- PR: #{pr}", f"- Head SHA: `{head_sha}`",
             f"- Diff: `gh pr diff {pr} --repo {repo}`", f"- Language of `user_brief`: {language}"]
    return "\n".join([_read("second-review.md"), "## This PR", *facts, "## The items", "```json",
                      json.dumps(items, ensure_ascii=False, indent=2), "```"])


def splitter_prompt(project, repo, number, language, local_dir=DEFAULT_LOCAL_DIR):
    facts = [f"- Repo: `{repo}`", f"- Issue: #{number}", f"- Read it with: `gh issue view {number} --repo {repo}`",
             f"- Language of `user_text`: {language}", *_project_facts(project)]
    return "\n".join([_read("splitter.md"), "## This task", *facts, "## The size scale", *sizing.describe(),
                      "## Issue template", _read("templates/issue.md"), *_memory(project, local_dir)])


def notes_prompt(project, repo, number, pr, notes, local_dir=DEFAULT_LOCAL_DIR):
    facts = [f"- Repo: `{repo}`", f"- Issue: #{number}", f"- PR: #{pr}", *_project_facts(project)]
    return "\n".join([_read("splitter-notes.md"), "## This PR", *facts, "## The size scale", *sizing.describe(),
                      "## The notes", "```json", json.dumps(notes, ensure_ascii=False, indent=2), "```",
                      *_memory(project, local_dir)])


def queen_prompt(events, projects, steps, step_limit, language, logs_dir, today, debts=()):
    facts = [f"- Today: {today}", f"- Language of the brief: {language}", f"- Logs folder: `{logs_dir}`",
             f"- Each of {', '.join(f'`{a}`' for a in ('retry', 'rework', 'verify', 'new_task', 'block_on'))} "
             f"may be taken {step_limit} times per task"]
    parts = [_read("queen.md"), "## This look", *facts, "## Projects", "```json",
             json.dumps(projects, ensure_ascii=False, indent=2), "```", "## Your steps so far on these tasks", "```json",
             json.dumps(steps, ensure_ascii=False, indent=2), "```", "## Events since your last look", "```json",
             json.dumps(events, ensure_ascii=False, indent=2), "```"]
    if debts:
        parts += ["## Notes to park", "```json", json.dumps(list(debts), ensure_ascii=False, indent=2), "```"]
    return "\n".join(parts)


def check_prompt(project, repo, number, pr, head_sha, question, local_dir=DEFAULT_LOCAL_DIR):
    facts = [f"- Repo: `{repo}`", f"- Issue: #{number}", *_project_facts(project)]
    facts += [f"- PR: #{pr}", f"- Your checkout is its head `{head_sha}`", f"- Diff: `gh pr diff {pr} --repo {repo}`"] \
        if pr else ["- The task has no open PR: your checkout is the default branch"]
    return "\n".join([_read("check.md"), "## This check", *facts, "## The queen's question", question,
                      *_memory(project, local_dir)])


def parse_check(text):
    """Takes a checker's last message; returns its result object."""
    try:
        reply = _final_json(text)
    except json.JSONDecodeError as e:
        raise ReplyError("check reply is not JSON") from e
    if not isinstance(reply, dict) or reply.get("result") not in CHECK_RESULTS:
        raise ReplyError("check reply has no known result")
    if not isinstance(reply.get("summary"), str) or not reply["summary"].strip():
        raise ReplyError("check reply has no summary")
    return reply


def live_check_prompt(project, repo, number, local_dir=DEFAULT_LOCAL_DIR):
    facts = [f"- Repo: `{repo}`", f"- Issue: #{number}", f"- Live URL: {project.deploy.live_url}"]
    return "\n".join([_read("live-check.md"), "## This check", *facts, *_memory(project, local_dir)])


def parse_live_check(text):
    """Takes a live check's last message; returns its result object."""
    try:
        reply = _final_json(text)
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
        if decision["action"] == "park" and type(decision.get("finding")) is not int:
            raise ReplyError("a park decision has no finding")
        if decision["action"] == "approve" and not isinstance(decision.get("head_sha"), str):
            raise ReplyError("an approve decision has no head_sha")
        if decision["action"] == "block_on" and not isinstance(decision.get("on"), str):
            raise ReplyError("a block_on decision has no task it waits on")
        if decision["action"] == "new_task" and not all(isinstance(decision.get(name), str) and decision[name].strip()
                                                         for name in NEW_TASK_FIELDS):
            raise ReplyError(f"a new_task decision needs {', '.join(NEW_TASK_FIELDS)}")
    return report


def _final_json(text):
    """The reply's JSON: the whole text, else a code block or a line that ends it, since an agent may sum up its
    work above it."""
    text = text.strip()
    fence = CODE_FENCE.search(text)
    for candidate in (text, fence and fence.group(1), text.rsplit("\n", 1)[-1]):
        if candidate:
            try:
                return json.loads(candidate.strip())
            except json.JSONDecodeError:
                pass
    raise json.JSONDecodeError("no JSON at the end of the reply", text, 0)


def parse_worker_reply(text):
    """Takes a worker's last message; returns the status object it reports."""
    try:
        reply = _final_json(text)
    except json.JSONDecodeError as e:
        raise ReplyError("worker reply is not JSON") from e
    if not isinstance(reply, dict) or reply.get("status") not in WORKER_STATUSES:
        raise ReplyError("worker reply has no known status")
    if reply["status"] == "needs_info" and not isinstance(reply.get("question"), str):
        raise ReplyError("needs_info has no question")
    if reply["status"] == "blocked" and not isinstance(reply.get("reason"), str):
        raise ReplyError("blocked has no reason")
    return reply
