"""Puts a task on a board (DESIGN §2).

`/kandu-task` writes the text; this code enforces what must hold, so a rule
does not depend on the conversation.
"""
import argparse
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path

from . import sizing
from .config import BOARD_FIELDS, DEFAULT_LOCAL_DIR, DEADLINE_FIELD, ConfigError, load_config
from .github import (BLOCKED_BY, BY_HAND_DESCRIPTION, BY_HAND_LABEL, HTML_COMMENT, ISSUE_REFERENCE, GitHubError,
                     blockers_in)
from .mirror import open_github
from .privateterms import all_terms
from .queen import all_events
from .state import StateStore

TEMPLATE = Path(__file__).resolve().parent.parent / "prompts" / "templates" / "issue.md"
SPEC_TEMPLATE = TEMPLATE.with_name("spec.md")
OUT_OF_SCOPE = "Out of scope"


class TaskError(Exception):
    pass


def _headings(path):
    return re.findall(r"^## (.+)$", path.read_text(encoding="utf-8"), re.MULTILINE)


def required_sections():
    return _headings(TEMPLATE)


def spec_sections():
    return _headings(SPEC_TEMPLATE)


def _check_sections(body, required, what):
    sections = re.findall(r"^## (.+?)\s*$", body, re.MULTILINE)
    missing = [s for s in required if s not in sections]
    if missing:
        raise TaskError(f"{what} misses: {', '.join(missing)}")


def check_body(body):
    _check_sections(body, required_sections(), "the issue")


def check_spec_body(body):
    _check_sections(body, spec_sections(), "the spec")


def check_english(text):
    """Agents write and read tasks in English, so nobody pays for a translation."""
    if any(c.isalpha() and not unicodedata.name(c, "").startswith("LATIN") for c in text):
        raise TaskError("a task is written in English: its title, body and sub-tasks")


def private_words(local_dir):
    """The user's private words and the terms of the setup itself, as the git hooks see them."""
    return all_terms(local_dir)


def check_private(text, words):
    found = [word for word in words if word.lower() in text.lower()]
    if found:
        raise TaskError(f"this repo is public, and the text holds {len(found)} private word(s)")


def check_fields(priority, kind, size, deadline):
    """The queen paces work from these alone, so a task without them cannot exist."""
    for name, value in (("Priority", priority), ("Kind", kind), ("Size", size)):
        if value not in BOARD_FIELDS[name]:
            raise TaskError(f"{name} must be one of: {', '.join(BOARD_FIELDS[name])}")
    if deadline is not None:
        try:
            date.fromisoformat(deadline)
        except ValueError as e:
            raise TaskError("the deadline must be a date, YYYY-MM-DD") from e


def check_size(body, size):
    """Files and the door are the splitter's to judge; the criteria can be counted here."""
    criteria = sizing.criteria_in(body)
    if not sizing.fits(size, criteria, 0):
        raise TaskError(f"{criteria} acceptance criteria do not fit {size}: size it {sizing.LARGE}")


def check_not_large(size):
    """Didban splits a task while it is defined. The splitter still catches one that turns out large."""
    if size == sizing.LARGE:
        raise TaskError(f"a {sizing.LARGE} task does not go on the board: split it into slices first")


def find_project(config, project_name, repo):
    """Returns the project and the repo a new task goes in."""
    project = next((p for p in config.projects if p.name == project_name), None)
    if project is None:
        raise TaskError(f"unknown project: {project_name}")
    repo = repo or (project.repos[0] if len(project.repos) == 1 else None)
    if repo not in project.repos:
        raise TaskError(f"name one of the project's repos: {', '.join(project.repos)}")
    if project.board is None:
        raise TaskError(f"{project_name} has no board")
    return project, repo


def check_text(github, repo, title, body, local_dir):
    check_english(f"{title}\n{body}")
    if github.repo_is_public(repo):
        check_private(f"{title}\n{body}", private_words(local_dir))


def check_task(github, repo, title, body, local_dir, priority, kind, size, deadline):
    """All that must hold before a task may reach `Backlog`."""
    check_body(body)
    check_fields(priority, kind, size, deadline)
    check_not_large(size)
    check_size(body, size)
    check_text(github, repo, title, body, local_dir)


def last_slice(store, key):
    """The slice that ends a split task, or None when `key` was not split. A slice split again leads on to its own."""
    last, seen = None, set()
    while key not in seen:
        seen.add(key)
        split = store.last_event("task_split", task=key)
        if split is None or not split["children"]:
            break
        key = last = split["children"][-1]
    return last


def waits_for_slices(body, repo, last_of):
    """Returns the body with every split blocker replaced by its last slice, and the (old, new) pairs replaced.

    Slices merge in order, so the last one closing means all are done. `last_of(key)` names a split task's last
    slice, or None. Only the references change: the author's own words and comments in `## Blocked by` stay.
    A body with no split blocker comes back as it was; no blocker is named twice.
    """
    blockers = [f"{other}#{number}" for other, number in blockers_in(body, repo)]
    rewrites = [(old, last_of(old)) for old in dict.fromkeys(blockers)]
    rewrites = [(old, new) for old, new in rewrites if new is not None and new != old]
    if not rewrites:
        return body, []
    new_of, seen = dict(rewrites), set()

    def reference(match):
        old = f"{match.group(1) or repo}#{match.group(2)}"
        key = new_of.get(old, old)
        if key in seen:
            return ""
        seen.add(key)
        other, _, number = key.partition("#")
        return f"#{number}" if other == repo and not match.group(1) else key

    def line(text):
        changed = ISSUE_REFERENCE.sub(reference, text)
        return None if changed != text and not changed.strip(" \t-*,") else changed

    def outside_comments(text):
        lines = (line(part) for part in text.split("\n"))
        return "\n".join(part for part in lines if part is not None)

    def section(match):
        head, text = match.group(0)[:match.start(1) - match.start(0)], match.group(1)
        parts, last = [], 0
        for comment in HTML_COMMENT.finditer(text):
            parts += [outside_comments(text[last:comment.start()]), comment.group(0)]
            last = comment.end()
        return head + "".join(parts + [outside_comments(text[last:])])

    return BLOCKED_BY.sub(section, body, count=1), rewrites


def record_rewrites(store, task, project, rewrites, when):
    """`when` is `split` for a task that waited before the split, `defined` for one defined after it."""
    for old, new in rewrites:
        store.record("blocker_rewritten", task=task, project=project, old=old, new=new, when=when)


def _section(name):
    return re.compile(rf"^## {re.escape(name)}[ \t]*\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)


def section_text(body, name):
    """The text under `## name`, up to the next section, or None when the body has no such section."""
    match = _section(name).search(body)
    return match.group(1) if match else None


def with_section(body, name, text):
    """The body with the text under `## name` replaced, or the section added at the end when it is missing."""
    text = text.rstrip("\n") + "\n"
    if _section(name).search(body) is None:
        return f"{body.rstrip()}\n\n## {name}\n{text}"
    return _section(name).sub(lambda m: f"## {name}\n{text}" + ("\n" if m.end() < len(body) else ""), body, count=1)


def specs(store):
    """The spec issues Kandu made, open or closed. None of them is work."""
    return {e["task"] for e in all_events(store) if e["type"] == "spec_created"}


def spec_key(reference, repo):
    """`#12`, `12` or `owner/repo#12` as `owner/repo#12`; a bare number is in `repo`."""
    match = re.fullmatch(r"\s*([\w.-]+/[\w.-]+)?#?(\d+)\s*", reference or "")
    if match is None:
        raise TaskError("--spec takes the spec issue as #12 or owner/repo#12")
    return f"{match.group(1) or repo}#{match.group(2)}"


def with_spec(github, store, project, repo, body, reference):
    """Returns (body, spec): the body names its spec at the top and takes the spec's Out of scope as its own."""
    key = spec_key(reference, repo)
    made = store.last_event("spec_created", task=key)
    if made is None or made["project"] != project.name:
        raise TaskError(f"{key} is not a spec of {project.name}: make one with --spec-issue first")
    spec_repo, _, number = key.partition("#")
    if not github.issue_is_open(spec_repo, int(number)):
        raise TaskError(f"the spec {key} is closed: its tasks are done")
    out_of_scope = section_text(github.issue_text(spec_repo, int(number))["body"], OUT_OF_SCOPE)
    if out_of_scope is None:
        raise TaskError(f"the spec {key} has no {OUT_OF_SCOPE}")
    named = f"#{number}" if spec_repo == repo else key
    return f"Spec: {named}\n\n{with_section(body, OUT_OF_SCOPE, out_of_scope)}", key


def create_spec(config, github, project_name, repo, title, body, local_dir, store):
    """Creates a spec issue and returns its URL. It holds a request of several tasks and is never work itself:
    it has no `agent` label, no fields and no card, so it is never sized and never reaches the queen.
    """
    project, repo = find_project(config, project_name, repo)
    check_spec_body(body)
    check_text(github, repo, title, body, local_dir)
    url = github.create_issue(repo, title, body, by_hand=True, spec=True)
    store.record("spec_created", task=f"{repo}#{int(url.rstrip().rsplit('/', 1)[1])}", project=project.name)
    return url


def set_fields(github, board, url, priority, kind, size, deadline):
    for name, value in (("Priority", priority), ("Kind", kind), ("Size", size), (DEADLINE_FIELD, deadline)):
        if value is not None:
            github.set_field(board, url, name, value)


def create_task(config, github, project_name, repo, title, body, local_dir, priority, kind, size, deadline=None,
                by_hand=False, tokens=False, created=None, store=None, rewritten=None, spec=None):
    """Creates the issue, puts it on the board in `Backlog` and returns its URL.

    `created(url)` runs as soon as the issue exists, so a caller can record it before the
    board steps, which can fail on their own. With the local log in `store`, a blocker that
    was split is replaced by its last slice, logged, and passed to `rewritten(old, new)`.
    `spec` names the spec issue the task belongs to (`with_spec`); it needs `store`.
    """
    project, repo = find_project(config, project_name, repo)
    rewrites = []
    if store is not None:
        body, rewrites = waits_for_slices(body, repo, lambda key: last_slice(store, key))
    if spec is not None:
        if store is None:
            raise TaskError("a task of a spec needs the local log")
        body, spec = with_spec(github, store, project, repo, body, spec)
    if tokens and not project.task_tokens:
        raise TaskError(f"{project_name} has no task_tokens")
    if tokens and by_hand:
        raise TaskError("a task done by hand runs no worker, so it takes no tokens")
    check_task(github, repo, title, body, local_dir, priority, kind, size, deadline)
    url = github.create_issue(repo, title, body, by_hand=by_hand, tokens=tokens)
    key = f"{repo}#{int(url.rstrip().rsplit('/', 1)[1])}"
    if spec is not None:
        store.record("spec_named", task=key, project=project.name, spec=spec)
    if rewrites:
        record_rewrites(store, key, project.name, rewrites, "defined")
        for old, new in rewrites if rewritten is not None else ():
            rewritten(old, new)
    if created is not None:
        created(url)
    place_task(github, project.board, url, priority, kind, size, deadline)
    return url


def place_task(github, board, url, priority, kind, size, deadline=None):
    """Puts an issue on the board in `Backlog` with its fields. Running it again changes nothing."""
    github.add_to_board(board, url)
    github.set_status(board, url, "Backlog")
    set_fields(github, board, url, priority, kind, size, deadline)


def place_existing(config, github, project_name, url, priority, kind, size, deadline=None):
    """Finishes a task whose issue exists but whose board steps failed. Creates nothing."""
    if "github.com/" not in url or "/issues/" not in url:
        raise TaskError("--place takes the issue's URL")
    check_fields(priority, kind, size, deadline)
    repo = "/".join(url.split("github.com/")[1].split("/")[:2])
    project, _ = find_project(config, project_name, repo)
    place_task(github, project.board, url, priority, kind, size, deadline)
    return url


def label_by_hand(config, github, store):
    """Gives each open by-hand task of a registered board the `by-hand` label, once, and returns their keys.

    A task with `agent`, a spec, a task done or closed, and one that has the label already are left alone,
    so a second run changes nothing.
    """
    specs_made, labelled, boards = specs(store), [], {}
    for project in config.projects:
        if project.board is not None:
            boards.setdefault((project.board.owner, project.board.number), (project.board, set()))[1].update(
                project.repos)
    for board, repos in boards.values():
        for item in github.board_items(board):
            key = f"{item.repo}#{item.number}"
            if (item.repo in repos and item.waits_by_hand and key not in specs_made
                    and BY_HAND_LABEL not in item.labels):
                github.add_label(item.repo, item.number, BY_HAND_LABEL, BY_HAND_DESCRIPTION)
                labelled.append(key)
    return labelled


def label_existing(args):
    """`--label-by-hand`: the one-time backfill of the `by-hand` label."""
    try:
        config = load_config(args.local)
        github = open_github(config, refresh=True)
        labelled = label_by_hand(config, github, StateStore())
    except ConfigError as e:
        print(f"tasks: {len(e.problems)} problem(s) in config; run: python -m kandu.config", file=sys.stderr)
        return 1
    except GitHubError as e:
        print(f"tasks: {e}", file=sys.stderr)
        return 1
    print(", ".join(labelled) or "every by-hand task has the label")
    if labelled:
        try:
            github.send()
        except GitHubError as e:
            print(f"tasks: the labels wait in the queue, and kandu.sync sends them: {e}", file=sys.stderr)
    return 0


def send_now(github):
    """The card's fields wait in the queue; the user ordered this task, so they go to GitHub now, not at the next sync."""
    try:
        _, kept, dropped = github.send()
    except GitHubError as e:
        return f"tasks: the fields wait in the queue, and kandu.sync sends them: {e}"
    if kept or dropped:
        return f"tasks: {kept} change(s) still wait and {dropped} were dropped; see: python -m kandu.sync --queue"
    return None


def tell_rewrite(old, new):
    print(f"tasks: {old} was split, so this task waits for its last slice {new} instead", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Put a task on a board.")
    parser.add_argument("--project")
    parser.add_argument("--repo", help="needed when the project has several repos")
    parser.add_argument("--title")
    parser.add_argument("--body-file")
    parser.add_argument("--place", metavar="URL", help="put an existing issue on the board, with these fields")
    parser.add_argument("--spec-issue", action="store_true",
                        help="make the spec issue of a request of several tasks: no fields, no card")
    parser.add_argument("--spec", metavar="ISSUE", help="the spec issue this task belongs to, as #12 or owner/repo#12")
    parser.add_argument("--priority", choices=BOARD_FIELDS["Priority"])
    parser.add_argument("--kind", choices=BOARD_FIELDS["Kind"])
    parser.add_argument("--size", choices=BOARD_FIELDS["Size"])
    parser.add_argument("--deadline", help="the date it must be done by, YYYY-MM-DD")
    parser.add_argument("--by-hand", action="store_true", help="a person does this: no `agent` label")
    parser.add_argument("--tokens", action="store_true", help="its worker gets the project's task_tokens")
    parser.add_argument("--label-by-hand", action="store_true",
                        help="give each open by-hand task the `by-hand` label; a second run changes nothing")
    parser.add_argument("--local", default=DEFAULT_LOCAL_DIR, help="config directory (default: local/)")
    args = parser.parse_args()
    if args.label_by_hand:
        return label_existing(args)
    if not args.project:
        parser.error("the following arguments are required: --project")
    if not args.place and not (args.title and args.body_file):
        parser.error("a new task needs --title and --body-file")
    if args.spec_issue:
        given = [name for name in ("place", "spec", "priority", "kind", "size", "deadline", "by_hand", "tokens")
                 if getattr(args, name)]
        if given:
            print(f"tasks: a spec is not sized and is not work, so it takes no --{given[0].replace('_', '-')}",
                  file=sys.stderr)
            return 1
        return make_spec(args)
    missing = [name for name in ("priority", "kind", "size") if getattr(args, name) is None]
    if missing:
        parser.error(f"a task needs --{', --'.join(missing)}")
    created = []
    try:
        config = load_config(args.local)
        github = open_github(config, refresh=True)
        if args.place:
            url = place_existing(config, github, args.project, args.place, args.priority, args.kind, args.size,
                                 args.deadline)
        else:
            body = Path(args.body_file).read_text(encoding="utf-8")
            url = create_task(config, github, args.project, args.repo, args.title, body, args.local, args.priority,
                              args.kind, args.size, args.deadline, args.by_hand, args.tokens, created=created.append,
                              store=StateStore(), rewritten=tell_rewrite, spec=args.spec)
    except ConfigError as e:
        print(f"tasks: {len(e.problems)} problem(s) in config; run: python -m kandu.config", file=sys.stderr)
        return 1
    except (TaskError, GitHubError) as e:
        print(f"tasks: {e}", file=sys.stderr)
        if created:
            print(f"tasks: the issue exists, but is not on the board yet: {created[0]}\n"
                  f"tasks: finish it with the same command and --place {created[0]} instead of --title and "
                  "--body-file; without --place a second issue is made", file=sys.stderr)
        return 1
    print(url)
    waiting = send_now(github)
    if waiting:
        print(waiting, file=sys.stderr)
    return 0


def make_spec(args):
    try:
        config = load_config(args.local)
        github = open_github(config, refresh=True)
        body = Path(args.body_file).read_text(encoding="utf-8")
        url = create_spec(config, github, args.project, args.repo, args.title, body, args.local, StateStore())
    except ConfigError as e:
        print(f"tasks: {len(e.problems)} problem(s) in config; run: python -m kandu.config", file=sys.stderr)
        return 1
    except (TaskError, GitHubError) as e:
        print(f"tasks: {e}", file=sys.stderr)
        return 1
    print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
