"""What each agent is given: its prompt and its permissions (DESIGN §5, §9, §14).

Permissions come from the config (`permissions` in kandu.yaml) and are passed
per run; with none, a role may do everything. An agent gets only the context
it needs: the facts of one task, never the history of others.
"""
import json
import re
from pathlib import Path

from . import reference, sizing
from .config import BOARD_FIELDS, DEFAULT_LOCAL_DIR
from .review import CODE_FENCE

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
# The one command an agent runs to read an issue or a PR: it prints it from Kandu's copy (DESIGN §3), from any folder.
SHOW = PROMPTS_DIR.parent / "bin" / "kandu-show"
SHOW_RULE = f"Bash({SHOW} *)"
WORKER_STATUSES = ("pushed", "needs_info", "blocked")
# A PR description that names its issue this way closes it on merge.
CLOSES = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?) #(\d+)\b", re.IGNORECASE)
# Kandu merges the base, opens a PR and edits its description in code; no worker does, whatever the config says.
KANDU_ONLY = ["Bash(git merge *)", "Bash(git pull *)", "Bash(git rebase *)", "Bash(gh pr create *)",
              "Bash(gh pr edit *)", "Bash(gh pr merge *)"]
LIVE_RESULTS = ("pass", "fail")
QUEEN_ACTIONS = ("release", "retry", "rework", "verify", "new_task", "block_on", "approve", "ask_user", "park",
                 "drop_blocker", "accept_split")
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
                "blocks": {"type": "boolean"}, "proposal": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"},
                **{name.lower(): {"enum": list(values)} for name, values in BOARD_FIELDS.items()}}}},
    },
    "required": ["brief", "decisions"],
    "additionalProperties": False,
}


# Given to `claude`, so a worker or a live check ends on one object. The parsers still check what a schema cannot.
_TEXT = {"type": "string"}
WORKER_SCHEMA = {
    "type": "object", "required": ["status"],
    "properties": {"status": {"enum": list(WORKER_STATUSES)}, "pr_title": _TEXT, "pr_body": _TEXT,
                   "question": _TEXT, "reason": _TEXT},
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
    "type": "object", "required": ["size", "files", "door", "reason", "slices", "forbidden"],
    "properties": {**_FACTS, "reason": _TEXT, "forbidden": {"type": "array", "items": _TEXT},
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


def _limits(rules, extra_allowed=(), extra_denied=(), branch="", remote=False):
    """Returns (allowed, denied) for one run, from the role's rules in the config.

    With no rules the role may do everything: `allowed` is None, and only
    `extra_denied`, which also comes from the config, still holds.
    `{branch}` in a rule becomes the run's branch. Every limited role may read an issue or a PR with `SHOW`,
    except on a `remote` machine, away from the brain, where `SHOW` is not: there its prompt holds them.
    """
    if rules is None:
        return None, list(extra_denied)
    allowed = [rule.replace("{branch}", branch) for rule in rules.allow] + list(extra_allowed) + ([] if remote else [SHOW_RULE])
    denied = [rule.replace("{branch}", branch) for rule in rules.deny] + list(extra_denied)
    return allowed, denied


def _test_tools(project):
    """The project's test and, if it has one, its setup: a worker or reviewer may run both in its checkout."""
    commands = [project.test] + ([project.setup.command] if project.setup else [])
    return [rule for command in commands for rule in (f"Bash({command})", f"Bash({command} *)")]


def _forbidden(project):
    # Edit rules cover every tool that changes a file; Claude Code ignores Write(path).
    return [f"Edit(./{area.rstrip('/')}/**)" if area.endswith("/") else f"Edit(./{area})"
            for area in project.forbidden]


def worker_tools(project, branch, rules=None, remote=False, reference=None):
    """Returns (allowed, denied) for a worker on `branch`. With `reference`, the folder of a read-only copy, it may
    read that folder too."""
    if rules is None:
        return _limits(None, extra_denied=_forbidden(project) + KANDU_ONLY)
    allowed = _test_tools(project) + project.allowed_tools + _reference_rule(reference)
    denied = []
    if project.secrets_from:
        path = project.secrets_from
        denied.append(f"Read({'/' + path if path.startswith('/') else path})")
    return _limits(rules, allowed, denied + _forbidden(project) + KANDU_ONLY, branch, remote)


def _reference_rule(folder):
    return [reference.read_rule(folder)] if folder else []


def reviewer_tools(project, rules=None, remote=False, reference=None):
    """Returns (allowed, denied) for a reviewer, which runs the project's tests and commands itself and reads the
    diff with git in its checkout. With `reference`, the folder of a read-only copy, it may read that folder too."""
    if rules is None:
        return _limits(None)
    return _limits(rules, _test_tools(project) + ["Bash(git diff *)"] + project.allowed_tools
                   + _reference_rule(reference), remote=remote)


def live_check_tools(project, rules=None, remote=False):
    """Returns (allowed, denied) for a live check."""
    return _limits(rules, project.allowed_tools, remote=remote)


def role_tools(rules=None, remote=False):
    """Returns (allowed, denied) for the splitter or the queen; `remote` when it runs away from the brain."""
    return _limits(rules, remote=remote)


def show_command(repo, number):
    """How an agent reads issue or PR `number` of `repo`, from the copy, so the read costs GitHub nothing."""
    return f"{SHOW} {repo}#{number}"


def _how(repo, number, copy):
    """How a prompt tells an agent to read issue or PR `number`. With `copy`, the agent runs away from the brain,
    where `SHOW` is not, so the item's text from Kandu's copy goes into the prompt instead (see `_copies`)."""
    if copy is None:
        return f"Read it with: `{show_command(repo, number)}`"
    return f"Kandu's copy of it is below, under `{_copy_head(repo, number)}`"


def _copy_head(repo, number):
    return f"## Kandu's copy of {repo}#{number}"


def _copies(copy, items):
    """The text of each (repo, number) in `items` as `kandu.show` prints it, for an agent that cannot run it."""
    if copy is None:
        return []
    return [part for repo, number in items for part in (_copy_head(repo, number), copy(repo, number))]


WORKER_SPEC_RULE = ("The spec's decisions are part of your assignment, like the issue's own text. A change that "
                    "contradicts one is out of bounds: do not make it. If the task cannot be done without "
                    "contradicting one, report `blocked` and name the decision.")
REVIEWER_SPEC_RULE = ("The spec's decisions bind this PR like the issue's own text. A change that contradicts a "
                      "decision is a blocker: name the decision in the finding.")


def _spec(spec, rule=None):
    """The spec of the task, from Kandu's copy, for a prompt; nothing when the task has none (`spec` is None)."""
    if not spec:
        return []
    return ["## The task's spec", *([rule] if rule else []), spec]


def _diff(base, head_sha):
    return f"- Diff: `git diff origin/{base}...{head_sha}` in your checkout"


def _read(name):
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _memory(project, local_dir):
    path = Path(local_dir) / "memory" / f"{project.name}.md"
    if not path.exists():
        return []
    return ["## Project memory", path.read_text(encoding="utf-8")]


def _project_facts(project):
    facts = [f"- Test command: `{project.test}`"]
    if project.base_branch:
        facts.append(f"- Base branch: `{project.base_branch}`. Work starts from it and merges into it. Where these "
                     "instructions say the default branch, they mean this one.")
    if project.setup:
        facts.append(f"- Setup command: `{project.setup.command}`. Run it before the tests in a checkout that has not "
                     "had it yet, or after its dependencies change.")
    facts.append("- Repo guides: `CLAUDE.md` and `AGENTS.md` are not loaded for you. If the repo has them, search them "
                 "for what concerns this task and read those parts.")
    commands = [rule[len("Bash("):-1] for rule in project.allowed_tools if rule.startswith("Bash(")]
    if commands:
        facts.append(f"- The project's own commands: {', '.join(f'`{c}`' for c in commands)}")
    if project.forbidden:
        facts.append(f"- Forbidden areas: {', '.join(project.forbidden)}")
    if project.repo_rules:
        facts.append(f"- Repo rules: {project.repo_rules}")
    if project.pr_test_output:
        facts.append(f"- Kandu writes the PR body's `## {project.pr_test_output}` section from its own test run on the "
                     "pushed branch: leave it out of `pr_body`.")
    return facts


def _token_facts(project):
    names = "; ".join(f"`{name}`: {purpose}" for name, purpose in project.task_tokens.items())
    return [f"- Tokens in your environment: {names}. This task needs real requests with them, and being given this "
            "task is the owner's yes for what it asks. Nothing else needs asking."]


def worker_prompt(project, repo, number, branch, run_id, findings=None, pr=None, local_dir=DEFAULT_LOCAL_DIR,
                  queen_note=None, tokens=False, open_blockers=(), order=None, copy=None, reference=None, spec=None):
    """The first run gets the PR template; a fix round gets what the last review left for it instead, and a
    worker the queen sends back to a PR gets her order. `copy(repo, number)` gives an item's text from Kandu's
    copy, for a worker away from the brain: the prompt then holds the issue and the PR instead of naming `SHOW`.
    `reference` is the text about the read-only copy of the task's reference (`reference.prompt_section`)."""
    facts = [f"- Repo: `{repo}`", f"- Issue: #{number}", f"- {_how(repo, number, copy)}",
             f"- Your branch: `{branch}`", f"- For the `Kandu-Run:` line: `{run_id}`", *_project_facts(project)]
    if tokens:
        facts += _token_facts(project)
    parts = [_read("worker.md"), "## This task", *facts, *_memory(project, local_dir),
             *_spec(spec, WORKER_SPEC_RULE)]
    if open_blockers:
        parts += ["## Blockers not merged yet",
                  f"This task is blocked by {', '.join(open_blockers)}, whose work may still be in an open PR. "
                  "Build it now anyway, on the default branch: your PR merges only after theirs. Do not merge their "
                  "branches; Kandu does not bring their code into yours unless the two conflict. If the task cannot "
                  "be done without their code, report `blocked`."]
    parts += [reference] if reference else []
    read = [(repo, number)] + ([(repo, pr)] if pr else [])
    if order is not None:
        return "\n".join(parts + [f"## Work the queen sends you on PR #{pr}",
                                  "The queen looked at this PR and sends you back to it. Do exactly this on the "
                                  "same branch, push, and end as after a fix round:", order,
                                  f"PR #{pr}: {_how(repo, pr, copy)}", *_copies(copy, read)])
    if queen_note:
        parts += ["## Note from the queen", "This task stopped once. She looked at why:", queen_note]
    if findings is None:
        parts += ["## PR template", _read("templates/pr.md")]
    else:
        parts += [f"## Review findings on PR #{pr}", "A review came back. Fix these and push to the same branch.",
                  f"PR #{pr}: {_how(repo, pr, copy)}",
                  "```json", json.dumps(findings, ensure_ascii=False, indent=2), "```"]
    return "\n".join(parts + _copies(copy, read))


def _pr_facts(repo, number, pr, head_sha, base, copy):
    return [f"- Repo: `{repo}`", f"- Issue: #{number}", f"- {_how(repo, number, copy)}",
            f"- PR: #{pr}", f"- {_how(repo, pr, copy)}", f"- Head SHA: `{head_sha}`",
            _diff(base, head_sha)]


def reviewer_prompt(project, repo, number, pr, head_sha, language, companions, criteria, first_n, base,
                    local_dir=DEFAULT_LOCAL_DIR, copy=None, reference=None, spec=None):
    facts = [*_pr_facts(repo, number, pr, head_sha, base, copy), f"- Language of `user_brief`: {language}",
             f"- Number new findings from {first_n}", *_project_facts(project)]
    parts = [_read("reviewer.md"), "## This PR", *facts, *_memory(project, local_dir),
             *_spec(spec, REVIEWER_SPEC_RULE)]
    if criteria:
        parts += ["## Acceptance criteria", *[f"{i}. {text}" for i, text in enumerate(criteria, 1)]]
    if companions:
        parts += ["## Companion PRs", *[f"- {other}#{n}: {_how(other, n, copy)}" for other, n in companions]]
    parts += [reference] if reference else []
    return "\n".join(parts + _copies(copy, [(repo, number), (repo, pr), *companions]))


def second_review_prompt(repo, number, pr, head_sha, language, items, base, copy=None, reference=None, spec=None):
    """Only what the first review listed: this round needs no memory, criteria or companions."""
    facts = [*_pr_facts(repo, number, pr, head_sha, base, copy), f"- Language of `user_brief`: {language}"]
    return "\n".join([_read("second-review.md"), "## This PR", *facts, *_spec(spec, REVIEWER_SPEC_RULE),
                      "## The items", "```json",
                      json.dumps(items, ensure_ascii=False, indent=2), "```",
                      *([reference] if reference else []), *_copies(copy, [(repo, number), (repo, pr)])])


def splitter_prompt(project, repo, number, language, local_dir=DEFAULT_LOCAL_DIR, copy=None, spec=None):
    facts = [f"- Repo: `{repo}`", f"- Issue: #{number}", f"- {_how(repo, number, copy)}",
             f"- Language of `user_text`: {language}", *_project_facts(project)]
    return "\n".join([_read("splitter.md"), "## This task", *facts, "## The size scale", *sizing.describe(),
                      "## Issue template", _read("templates/issue.md"), *_memory(project, local_dir),
                      *_spec(spec), *_copies(copy, [(repo, number)])])


def notes_prompt(project, repo, number, pr, notes, local_dir=DEFAULT_LOCAL_DIR, copy=None):
    facts = [f"- Repo: `{repo}`", f"- Issue: #{number}", f"- {_how(repo, number, copy)}",
             f"- PR: #{pr}", f"- {_how(repo, pr, copy)}", *_project_facts(project)]
    return "\n".join([_read("splitter-notes.md"), "## This PR", *facts, "## The size scale", *sizing.describe(),
                      "## The notes", "```json", json.dumps(notes, ensure_ascii=False, indent=2), "```",
                      *_memory(project, local_dir), *_copies(copy, [(repo, number), (repo, pr)])])


def queen_prompt(events, projects, steps, step_limit, language, logs_dir, today, debts=(), drop_blockers=False):
    facts = [f"- Today: {today}", f"- Language of the brief: {language}", f"- Logs folder: `{logs_dir}`",
             f"- Read an issue or a PR with: `{show_command('OWNER/REPO', 'NUMBER')}`",
             f"- Each of {', '.join(f'`{a}`' for a in ('retry', 'rework', 'verify', 'new_task', 'block_on'))} "
             f"may be taken {step_limit} times per task",
             f"- `drop_blocker` is {'on' if drop_blockers else 'off: code refuses it, so do not use it'}"]
    parts = [_read("queen.md"), "## This look", *facts, "## Projects", "```json",
             json.dumps(projects, ensure_ascii=False, indent=2), "```", "## Your steps so far on these tasks", "```json",
             json.dumps(steps, ensure_ascii=False, indent=2), "```", "## Events since your last look", "```json",
             json.dumps(events, ensure_ascii=False, indent=2), "```"]
    if debts:
        parts += ["## Notes to park", "```json", json.dumps(list(debts), ensure_ascii=False, indent=2), "```"]
    return "\n".join(parts)


def check_prompt(project, repo, number, pr, head_sha, question, base, local_dir=DEFAULT_LOCAL_DIR, copy=None):
    facts = [f"- Repo: `{repo}`", f"- Issue: #{number}", f"- {_how(repo, number, copy)}",
             *_project_facts(project)]
    facts += [f"- PR: #{pr}", f"- {_how(repo, pr, copy)}", f"- Your checkout is its head `{head_sha}`",
              _diff(base, head_sha)] \
        if pr else ["- The task has no open PR: your checkout is the "
                    + (f"base branch `{base}`" if project.base_branch else "default branch")]
    return "\n".join([_read("check.md"), "## This check", *facts, "## The queen's question", question,
                      *_memory(project, local_dir), *_copies(copy, [(repo, number)] + ([(repo, pr)] if pr else []))])


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


def live_check_prompt(project, repo, number, local_dir=DEFAULT_LOCAL_DIR, copy=None):
    facts = [f"- Repo: `{repo}`", f"- Issue: #{number}", f"- {_how(repo, number, copy)}",
             f"- Live URL: {project.deploy.live_url}"]
    return "\n".join([_read("live-check.md"), "## This check", *facts, *_memory(project, local_dir),
                      *_copies(copy, [(repo, number)])])


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
        if decision["action"] == "drop_blocker" and not isinstance(decision.get("on"), str):
            raise ReplyError("a drop_blocker decision names no blocker")
        if decision["action"] == "accept_split" and not isinstance(decision.get("proposal"), str):
            raise ReplyError("an accept_split decision names no proposal")
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
    for name in ("pr_title", "pr_body"):
        if reply["status"] == "pushed" and name in reply and not (isinstance(reply[name], str) and reply[name].strip()):
            raise ReplyError(f"{name} is empty")
    return reply


def parse_first_reply(text):
    """A first run's reply: Kandu opens the PR from it, so `pushed` must bring its title and description."""
    reply = parse_worker_reply(text)
    if reply["status"] == "pushed" and not ("pr_title" in reply and "pr_body" in reply):
        raise ReplyError("pushed has no pr_title and pr_body")
    return reply


def closing_body(body, number):
    """The PR description as Kandu sends it: it always closes its own issue."""
    if str(number) in {m.group(1) for m in CLOSES.finditer(body)}:
        return body
    return f"{body.rstrip()}\n\nCloses #{number}\n"
