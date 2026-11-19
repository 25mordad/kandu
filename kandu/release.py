"""Proposes Kandu's next release and, on the user's yes, opens it as a PR on the private copy (DESIGN §12).

`python -m kandu.release` reads `main` on GitHub: the last release is the newest commit there that changed
`VERSION` (the one that brought in `1.0.0` counts as the start), and every commit since is a change. It proposes the
next version, MAJOR when a change carries the breaking mark, else MINOR when one is a `Feature`, else PATCH, and
prints the changelog entry. Only with the user's yes does it make a branch on GitHub with the new `VERSION` and
`CHANGELOG.md` and open a PR from it into `main`. It never touches the install's files, pushes nothing to `main`, and
never writes to the public upstream; tagging and publishing are not done here.
"""
import argparse
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

from .github import GitHub, GitHubError

INSTALL_DIR = Path(__file__).resolve().parent.parent
BRANCH = "main"
ORIGIN = "origin"      # the private copy
UPSTREAM = "upstream"  # the public Kandu, never written to
VERSION_PATH = "VERSION"
CHANGELOG_PATH = "CHANGELOG.md"
STARTING_VERSION = "1.0.0"
CHANGELOG_HEAD = "# Changelog\n"
PR_NUMBER = re.compile(r"\s*\(#(\d+)\)\s*$")
# The changelog's groups, in the order they are printed.
GROUPS = (("breaking", "Breaking"), ("Feature", "Features"), ("Bug", "Fixes"), ("other", "Other"))


class ReleaseError(Exception):
    pass


def remote_repo(remote, repo_dir=INSTALL_DIR):
    """The `owner/name` a git remote of the install points at on GitHub, or None. Reads only."""
    done = subprocess.run(["git", "remote", "get-url", remote], cwd=repo_dir, capture_output=True, text=True)
    if done.returncode != 0:
        return None
    match = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", done.stdout.strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


def bump(version, changes):
    """The next version after `version`: MAJOR if any change is breaking, else MINOR if any is a Feature, else PATCH."""
    try:
        major, minor, patch = (int(part) for part in version.split("."))
    except ValueError:
        raise ReleaseError(f"{VERSION_PATH} holds {version!r}, not MAJOR.MINOR.PATCH") from None
    if any(change["breaking"] for change in changes):
        return f"{major + 1}.0.0"
    if any(change["kind"] == "Feature" for change in changes):
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def group(change):
    if change["breaking"]:
        return "breaking"
    return change["kind"] if change["kind"] in ("Feature", "Bug") else "other"


def entry(version, changes, day):
    """The changelog entry of a release: its version and date, then each change under its group."""
    lines = [f"## {version} - {day.isoformat()}"]
    for key, heading in GROUPS:
        listed = [change for change in changes if group(change) == key]
        if listed:
            lines += ["", f"### {heading}"]
            lines += [f"- {c['title']} (#{c['number']})" if c["number"] else f"- {c['title']}" for c in listed]
    return "\n".join(lines) + "\n"


def with_entry(changelog, text):
    """The changelog with `text` as its newest entry, under the heading; a new one when there is none yet."""
    if not changelog:
        return f"{CHANGELOG_HEAD}\n{text}"
    if changelog.startswith(CHANGELOG_HEAD):
        rest = changelog[len(CHANGELOG_HEAD):].lstrip("\n")
        return f"{CHANGELOG_HEAD}\n{text}" + (f"\n{rest}" if rest else "")
    return f"{CHANGELOG_HEAD}\n{text}\n{changelog}"


def changes_since_release(github, repo):
    """The current version, the SHA `main` is at, and the changes merged since the last release, oldest first."""
    head = github.branch_head(repo, BRANCH)
    base = github.last_commit_to(repo, VERSION_PATH, head)
    if base is None:
        raise ReleaseError(f"{repo} has no {VERSION_PATH} on {BRANCH}, so there is no version to start from")
    version = (github.file_at(repo, VERSION_PATH, head) or STARTING_VERSION).strip() or STARTING_VERSION
    changes = []
    for commit in github.commits_between(repo, base, head):
        found = PR_NUMBER.search(commit["subject"])
        change = github.merged_change(repo, int(found.group(1))) if found else None
        if change is None:
            title = PR_NUMBER.sub("", commit["subject"]).strip() if found else commit["subject"]
            change = {"number": int(found.group(1)) if found else None, "title": title, "breaking": False,
                      "kind": None}
        changes.append(change)
    return version, head, changes


def propose(github, repo, day):
    """What the next release would be, changing nothing: None when nothing changed since the last one."""
    version, head, changes = changes_since_release(github, repo)
    if not changes:
        return {"version": version, "changes": []}
    nxt = bump(version, changes)
    return {"version": version, "next": nxt, "head": head, "changes": changes, "entry": entry(nxt, changes, day)}


def open_release(github, repo, proposal):
    """Opens the release PR on `repo`, from a new branch at the head that was proposed. Returns its URL."""
    nxt = proposal["next"]
    changelog = with_entry(github.file_at(repo, CHANGELOG_PATH, proposal["head"]), proposal["entry"])
    body = (f"## Summary\nKandu {proposal['version']} → {nxt}. Merging sets `{VERSION_PATH}` to `{nxt}` and adds this "
            f"entry to `{CHANGELOG_PATH}`:\n\n{proposal['entry']}\n## Blast radius\nOnly `{VERSION_PATH}` and "
            f"`{CHANGELOG_PATH}` change. Nothing is tagged or published, and nothing goes to the public upstream.\n")
    return github.files_pr(repo, f"release/{nxt}", BRANCH, proposal["head"],
                           {VERSION_PATH: f"{nxt}\n", CHANGELOG_PATH: changelog}, f"Release {nxt}", body)


def run(github, repo, confirm, out=print, day=None, upstream=None):
    """Prints the proposal, then opens the release PR only when `confirm()` says yes. Returns the PR's URL or None."""
    if upstream and upstream.lower() == repo.lower():
        raise ReleaseError(f"{repo} is the public upstream; a release PR goes only to the private copy")
    proposal = propose(github, repo, day or date.today())
    if not proposal["changes"]:
        out(f"No change since the last release ({proposal['version']}): nothing to propose.")
        return None
    out(f"Changes since {proposal['version']}:")
    for change in proposal["changes"]:
        marks = ", ".join(filter(None, [change["kind"], "breaking" if change["breaking"] else None]))
        number = f"#{change['number']} " if change["number"] else ""
        out(f"  {number}{change['title']}" + (f" ({marks})" if marks else ""))
    out(f"\nProposed version: {proposal['next']}\n\n{proposal['entry']}")
    if not confirm(f"Open the release PR for {proposal['next']} on {repo}?"):
        out("Nothing changed.")
        return None
    url = open_release(github, repo, proposal)
    out(f"Release PR: {url}")
    return url


def ask(question):
    """The user's yes at the terminal; no terminal is a no."""
    if not sys.stdin.isatty():
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def main(argv=None, github=None, remotes=remote_repo):
    parser = argparse.ArgumentParser(description="Propose Kandu's next version and changelog entry from what was "
                                                 "merged, and on a yes open the release PR on the private copy.")
    parser.add_argument("--repo", help=f"owner/name of the private copy (default: the {ORIGIN} remote)")
    parser.add_argument("--yes", action="store_true", help="open the release PR without asking")
    args = parser.parse_args(argv)
    repo = args.repo or remotes(ORIGIN)
    if not repo:
        print(f"release: cannot tell the private copy from the {ORIGIN} remote; give --repo", file=sys.stderr)
        return 1
    try:
        run(github or GitHub(), repo, (lambda _: True) if args.yes else ask, upstream=remotes(UPSTREAM))
    except (ReleaseError, GitHubError) as e:
        print(f"release: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
