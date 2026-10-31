"""The splitter's report, and what happens when the user approves its proposal (DESIGN §2).

The splitter proposes; only the user's approval creates issues. They form a group:
each slice is blocked by the one before it, and the parent closes with the last.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

from . import sizing
from .config import DEFAULT_LOCAL_DIR, ConfigError, load_config
from .github import BLOCKED_BY, GitHub, GitHubError, blockers_in
from .queen import all_events
from .review import CODE_FENCE
from .state import StateStore
from .tasks import TaskError, check_body, check_english, check_fields, check_private, create_task, private_words

MIN_SLICES = 2
BACKLOG = "Backlog"
NEEDS_SPLIT = "Needs split"


class ReportError(Exception):
    pass


def parse_report(text):
    """Takes the splitter's last message; returns its report."""
    text = text.strip()
    fence = CODE_FENCE.search(text)
    try:
        report = json.loads(fence.group(1) if fence else text)
    except json.JSONDecodeError as e:
        raise ReportError("splitter reply is not JSON") from e
    if not isinstance(report, dict):
        raise ReportError("splitter reply is not an object")
    return report


def _facts(record, where):
    files = record.get("files")
    if not isinstance(files, int) or isinstance(files, bool) or files < 0:
        raise ReportError(f"{where}: files must be a count")
    if record.get("door") not in sizing.DOORS:
        raise ReportError(f"{where}: door must be one of {', '.join(sizing.DOORS)}")
    if record.get("size") not in sizing.ORDER:
        raise ReportError(f"{where}: size must be one of {', '.join(sizing.ORDER)}")
    return files


def check_slices(slices):
    """Every slice must be ready to become an issue that may run."""
    if not isinstance(slices, list) or len(slices) < MIN_SLICES:
        raise ReportError(f"a split needs at least {MIN_SLICES} slices")
    for number, part in enumerate(slices, 1):
        where = f"slice {number}"
        if not isinstance(part, dict) or not isinstance(part.get("title"), str) or not part["title"].strip() \
                or not isinstance(part.get("body"), str):
            raise ReportError(f"{where}: needs a title and a body")
        files = _facts(part, where)
        if part["door"] == "mixed" or part["size"] == sizing.LARGE:
            raise ReportError(f"{where}: a slice is one door and is not Large")
        if not sizing.fits(part["size"], sizing.criteria_in(part["body"]), files):
            raise ReportError(f"{where}: too much for {part['size']}")
        try:
            check_body(part["body"])
            check_english(f"{part['title']}\n{part['body']}")
        except TaskError as e:
            raise ReportError(f"{where}: {e}") from e


def validate_report(report, body):
    """The size comes from the facts, not from the splitter's opinion of them."""
    files = _facts(report, "report")
    if not isinstance(report.get("reason"), str) or not report["reason"].strip():
        raise ReportError("report: needs a reason")
    needed = sizing.smallest(sizing.criteria_in(body), files, report["door"])
    if sizing.ORDER.index(report["size"]) < sizing.ORDER.index(needed):
        raise ReportError(f"report: says {report['size']}, the facts say {needed}")
    if report["size"] == sizing.LARGE:
        check_slices(report.get("slices"))
    elif report.get("slices"):
        raise ReportError("report: only a Large task is split")


def user_text(report):
    """The proposal in the user's language, or None. It is only read, so a bad one costs no more than the English."""
    text = report.get("user_text")
    parts = text.get("slices") if isinstance(text, dict) else None
    if not isinstance(text, dict) or not isinstance(text.get("reason"), str) or not isinstance(parts, list) \
            or len(parts) != len(report["slices"]) \
            or not all(isinstance(p, dict) and isinstance(p.get("title"), str) and isinstance(p.get("body"), str)
                       for p in parts):
        return None
    return text


def with_blockers(body, references):
    text = "\n".join(references) or "none"
    return BLOCKED_BY.sub(lambda _: f"## Blocked by\n{text}\n\n", body, count=1)


def current_size(store, key):
    """Returns the task's `task_sized` event, unless the user declined that proposal: then it is sized again."""
    sized = store.last_event("task_sized", task=key)
    declined = store.last_event("split_declined", task=key)
    return None if sized and declined and declined["ts"] >= sized["ts"] else sized


def pending(store):
    """Returns the proposals that wait for the user, oldest first."""
    return [s for s in store.splits() if store.last_event("task_split", task=s["task"]) is None]


def find_item(config, github, key):
    repo, _, number = key.partition("#")
    project = next((p for p in config.projects if repo in p.repos), None)
    if project is None or project.board is None or not number.isdigit():
        raise TaskError(f"unknown task: {key}")
    for item in github.board_items(project.board):
        if item.repo == repo and item.number == int(number):
            return project, item
    raise TaskError(f"{key} is not on its board")


def fingerprint(proposal):
    """Names one version of a proposal, so a yes reaches only the version the user read."""
    return hashlib.sha256(json.dumps(proposal, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def approve(config, github, store, key, local_dir, slices=None, seen=None):
    """Creates the group, once however many doors say yes.

    `slices` is the user's edited version of the proposal, if any. `seen` is the
    fingerprint of the proposal the user read.
    """
    with store.split_lock():
        return _approve(config, github, store, key, local_dir, slices, seen)


def _approve(config, github, store, key, local_dir, slices, seen):
    project, item = find_item(config, github, key)
    if item.status != NEEDS_SPLIT or store.last_event("task_split", task=key) is not None:
        raise TaskError(f"{key} is not waiting for a split")
    proposal = store.read_split(key)
    if proposal is None and slices is None:
        raise TaskError(f"{key} has no proposal")
    if seen is not None and (proposal is None or fingerprint(proposal) != seen):
        raise TaskError("the proposal has changed since you saw it")
    slices = slices if slices is not None else proposal["slices"]
    check_slices(slices)
    check_fields(item.priority, item.kind, sizing.MEDIUM, item.deadline or None)
    # A retry after a failure carries on after the slices that exist, and copies none of them.
    children = [e["task"] for e in all_events(store) if e["type"] == "slice_created" and e["parent"] == key]
    references = children[-1:] or [f"{repo}#{number}" for repo, number in blockers_in(item.body, item.repo)]
    if github.repo_is_public(item.repo):  # before the first issue, not on the one that holds the word
        check_private("\n".join(f"{part['title']}\n{part['body']}" for part in slices), private_words(local_dir))
    for part in slices[len(children):]:
        # At the top: under `## Blocked by` the parent would block its own slices.
        body = f"Part of #{item.number}\n\n{with_blockers(part['body'], references).rstrip()}\n"
        url = create_task(config, github, project.name, item.repo, part["title"], body, local_dir,
                          item.priority, item.kind, part["size"], item.deadline or None)
        child = f"{item.repo}#{int(url.rstrip().rsplit('/', 1)[1])}"
        store.record("slice_created", task=child, project=project.name, parent=key)
        store.record("task_sized", task=child, project=project.name, run_id=None, size=part["size"],
                     files=part["files"], door=part["door"], criteria=sizing.criteria_in(body), slices=0)
        children.append(child)
        references = [child]
    store.record("task_split", task=key, project=project.name, children=children)
    return children


def decline(config, github, store, key):
    """The user's no: the proposal goes, the task returns to Backlog and is sized again as it then reads."""
    project, item = find_item(config, github, key)
    if item.status != NEEDS_SPLIT or store.last_event("task_split", task=key) is not None:
        raise TaskError(f"{key} is not waiting for a split")
    github.set_status(project.board, item.url, BACKLOG)
    store.record("split_declined", task=key, project=project.name)
    store.drop_split(key)


def main():
    parser = argparse.ArgumentParser(description="See, approve and decline split proposals.")
    parser.add_argument("command", choices=("list", "show", "approve", "decline"))
    parser.add_argument("task", nargs="?", help="owner/repo#number")
    parser.add_argument("--file", help="approve this list of slices instead, as JSON; - reads stdin")
    parser.add_argument("--local", default=DEFAULT_LOCAL_DIR, help="config directory (default: local/)")
    args = parser.parse_args()
    store = StateStore()
    if args.command == "list":
        for split in pending(store):
            print(f"{split['task']}: {split['title']} ({len(split['slices'])} slices)")
        return 0
    if args.task is None:
        parser.error("name a task")
    if args.command == "show":
        split = store.read_split(args.task)
        if split is None:
            print(f"split: {args.task} has no proposal", file=sys.stderr)
            return 1
        print(json.dumps(split, ensure_ascii=False, indent=2))
        return 0
    try:
        config = load_config(args.local)
        if args.command == "decline":
            decline(config, GitHub(), store, args.task)
            return 0
        slices = None
        if args.file:
            text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
            slices = json.loads(text)
        print("\n".join(approve(config, GitHub(), store, args.task, args.local, slices)))
    except ConfigError as e:
        print(f"split: {len(e.problems)} problem(s) in config; run: python -m kandu.config", file=sys.stderr)
        return 1
    except (TaskError, ReportError, GitHubError, json.JSONDecodeError) as e:
        print(f"split: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
