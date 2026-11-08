"""Prints an issue, or a PR, from Kandu's copy of GitHub (DESIGN §3). It changes nothing.

    .venv/bin/python -m kandu.show OWNER/REPO#NUMBER          # an issue with its comments, or a PR with
    .venv/bin/python -m kandu.show OWNER/REPO#NUMBER --json   # its comments, review threads and checks
    .venv/bin/python -m kandu.show https://github.com/OWNER/REPO/pull/NUMBER

What the copy holds costs GitHub nothing. An item it does not hold yet is read from GitHub once and kept,
so the next call reads nothing. The text keeps its shape, so other tools can read it.
"""
import argparse
import json
import re
import sys

from .config import DEFAULT_LOCAL_DIR, ConfigError, load_config
from .github import GitHubError
from .mirror import open_github

NAMED = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")
URL = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)/?$")


def parse_item(text):
    """(repo, number) from `owner/repo#12` or an issue's or PR's URL, or None."""
    found = NAMED.match(text) or URL.match(text)
    return (found.group(1), int(found.group(2))) if found else None


def _label_names(labels):
    return ", ".join(label["name"] if isinstance(label, dict) else label for label in labels) or "none"


def _comment(comment, indent=""):
    author = (comment.get("author") or {}).get("login") or "ghost"
    lines = [f"--- {author}, {comment.get('createdAt') or 'queued'}", *(comment.get("body") or "").splitlines()]
    return [indent + line if line else line for line in lines]


def _check(check):
    name = check.get("name") or check.get("context") or "?"
    result = check.get("conclusion") or check.get("state") or check.get("status") or "?"
    return f"- {name}: {result}"


def render(item):
    """The item as text: a head of `key: value` lines, the body, then each list under its count."""
    lines = [f"{item['kind']} {item['repo']}#{item['number']}: {item['title']}", f"state: {item['state']}"]
    if item["kind"] == "pr":
        lines += [f"draft: {'yes' if item.get('isDraft') else 'no'}", f"mergeable: {item.get('mergeable')}",
                  f"branch: {item.get('headRefName')} @ {item.get('headRefOid')}",
                  f"files changed: {item.get('changedFiles')}"]
    lines += [f"labels: {_label_names(item.get('labels') or [])}", f"url: {item['url']}", "", item.get("body") or "", ""]
    if item["kind"] == "pr":
        checks, threads = item.get("statusCheckRollup") or [], item.get("reviewThreads") or []
        lines += [f"checks: {len(checks)}", *map(_check, checks), ""]
        lines.append(f"review threads: {len(threads)}")
        for thread in threads:
            place = thread.get("path") or "?"
            if thread.get("line"):
                place += f":{thread['line']}"
            lines.append(f"- {place}, {'resolved' if thread['resolved'] else 'open'}")
            for comment in thread.get("comments") or []:
                lines += _comment(comment, "  ")
        lines.append("")
    comments = item.get("comments") or []
    lines.append(f"comments: {len(comments)}")
    for comment in comments:
        lines += _comment(comment)
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Print an issue or a PR from Kandu's copy of GitHub.")
    parser.add_argument("item", help="OWNER/REPO#NUMBER, or the issue's or PR's URL")
    parser.add_argument("--json", action="store_true", help="print the item as the copy holds it")
    parser.add_argument("--local", default=DEFAULT_LOCAL_DIR, help="config directory (default: local/)")
    args = parser.parse_args(argv)
    named = parse_item(args.item)
    if named is None:
        print(f"show: {args.item!r} is not OWNER/REPO#NUMBER or a GitHub URL", file=sys.stderr)
        return 2
    try:
        config = load_config(args.local)
    except ConfigError as e:
        print(f"show: {len(e.problems)} problem(s) in config; run: python -m kandu.config", file=sys.stderr)
        return 1
    try:
        item = open_github(config).item(*named)
    except GitHubError as e:
        print(f"show: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(item, ensure_ascii=False, indent=2))
    else:
        print(render(item), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
