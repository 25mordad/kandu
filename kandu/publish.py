"""Tags a merged release and publishes it on the public upstream, once the deploy made it live (DESIGN §12).

Kandu's own deploy runs this as its last step, in the live install, after it pulled the merge and pushed `main`
to the upstream:

    <kandu>/.venv/bin/python -m kandu.publish --install <kandu> [--remote upstream]

A merge is a release when the install's `VERSION` has an entry in its `CHANGELOG.md`: only a release PR adds one.
Then the version's tag, `vX.Y.Z`, goes on the last commit that changed `VERSION`, once that commit is in the
install's checkout and in the upstream's `main`. Before anything goes public, the private guard's terms are
checked against the tag name and the entry. The tag is pushed to the upstream, then a release with the entry is
created there on GitHub. A tag or a release that already exists is not made again, so running it twice is safe.
Any failure exits non-zero, so the deploy fails and the user is told.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

from .github import GitHub, GitHubError
from .privateterms import all_terms

INSTALL_DIR = Path(__file__).resolve().parent.parent
REMOTE = "upstream"
BRANCH = "main"
VERSION_PATH = "VERSION"
CHANGELOG_PATH = "CHANGELOG.md"
TAG_PREFIX = "v"


class PublishError(Exception):
    pass


def tag_name(version):
    return f"{TAG_PREFIX}{version}"


def changelog_entry(changelog, version):
    """The entry of `version` in the changelog, from its `## X.Y.Z - date` line to the next entry, or None."""
    lines, found = [], False
    for line in (changelog or "").splitlines(keepends=True):
        if line.startswith("## "):
            if found:
                break
            found = re.match(rf"## {re.escape(version)}(\s|$)", line) is not None
        if found:
            lines.append(line)
    return "".join(lines).rstrip("\n") + "\n" if lines else None


class Git:
    """The install's checkout and its remotes, through `git`. Only `push_tag` writes."""

    def __init__(self, repo_dir, run=subprocess.run):
        self.repo_dir = str(repo_dir)
        self._run = run

    def _git(self, *args):
        return self._run(["git", "-C", self.repo_dir, *args], capture_output=True, text=True)

    def _out(self, *args):
        done = self._git(*args)
        if done.returncode != 0:
            raise PublishError(f"git {args[0]}: {(done.stderr or done.stdout).strip()}")
        return done.stdout

    def read(self, path):
        """A file of the checkout as it is on disk, or None when it is missing."""
        try:
            return (Path(self.repo_dir) / path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def last_commit_to(self, path):
        return self._out("log", "-1", "--format=%H", "--", path).strip() or None

    def head(self):
        return self._out("rev-parse", "HEAD").strip()

    def contains(self, ancestor, commit):
        """Whether `commit` holds `ancestor`. A commit git does not have holds nothing it can vouch for."""
        return self._git("merge-base", "--is-ancestor", ancestor, commit).returncode == 0

    def _ls_remote(self, remote, *refs):
        return [line.split("\t") for line in self._out("ls-remote", remote, *refs).splitlines() if "\t" in line]

    def remote_branch(self, remote, branch):
        found = self._ls_remote(remote, f"refs/heads/{branch}")
        return found[0][0] if found else None

    def remote_tag(self, remote, tag):
        """The commit a tag on the remote points at (an annotated tag's own commit), or None."""
        found = dict((ref, sha) for sha, ref in self._ls_remote(remote, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"))
        return found.get(f"refs/tags/{tag}^{{}}") or found.get(f"refs/tags/{tag}")

    def push_tag(self, remote, tag, commit):
        """Creates the tag on the remote only: no local tag is made, so nothing is left to push by mistake."""
        self._out("push", remote, f"{commit}:refs/tags/{tag}")

    def remote_repo(self, remote):
        """The `owner/name` a remote points at on GitHub, or None."""
        done = self._git("remote", "get-url", remote)
        if done.returncode != 0:
            return None
        match = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", done.stdout.strip())
        return f"{match.group(1)}/{match.group(2)}" if match else None


def check_private(texts, terms):
    """Raises when a private term is in any of `texts`. The terms found are counted, never named."""
    try:
        words = list(terms())
    except Exception as e:  # the guard never passes what it could not read
        raise PublishError(f"could not read the private terms ({type(e).__name__}): nothing was published") from e
    text = "\n".join(texts).lower()
    found = [word for word in words if word and word.lower() in text]
    if found:
        raise PublishError(f"the tag or the changelog entry holds {len(found)} private term(s): nothing was published")


def publish(git, github, terms, remote=REMOTE, branch=BRANCH, out=print):
    """Tags and publishes the live release, if the merge is one. Returns what it did:
    `not_a_release`, `already_published` or `published`. Raises PublishError before anything goes public."""
    version = (git.read(VERSION_PATH) or "").strip()
    entry = changelog_entry(git.read(CHANGELOG_PATH), version) if version else None
    if entry is None:
        out(f"publish: {version or 'no version'} has no entry in {CHANGELOG_PATH}, so this is not a release: "
            "nothing to tag")
        return "not_a_release"
    tag = tag_name(version)
    commit = git.last_commit_to(VERSION_PATH)
    if not commit or not git.contains(commit, git.head()):
        raise PublishError(f"{version} is not live in the install yet: nothing was tagged")
    pushed = git.remote_branch(remote, branch)
    if not pushed or not git.contains(commit, pushed):
        raise PublishError(f"{version} is not in {remote}/{branch} yet: push {branch} first; nothing was tagged")
    check_private([tag, entry], terms)
    repo = git.remote_repo(remote)
    if not repo:
        raise PublishError(f"cannot tell the GitHub repo of the {remote} remote: nothing was tagged")
    existing = git.remote_tag(remote, tag)
    if existing is not None and existing != commit:
        raise PublishError(f"{tag} already exists on {remote} at another commit: nothing was published")
    if existing is None:
        git.push_tag(remote, tag, commit)
        out(f"publish: tagged {commit[:12]} as {tag} on {remote}")
    if github.release(repo, tag) is not None:
        if existing is not None:
            out(f"publish: {tag} is already tagged and released on {repo}")
            return "already_published"
        return "published"
    url = github.create_release(repo, tag, f"Kandu {version}", entry)
    out(f"publish: released {tag}: {url}")
    return "published"


def main(argv=None, git=None, github=None, terms=None):
    parser = argparse.ArgumentParser(description="Tag the live release and publish it on the public upstream, "
                                                 "after the deploy pushed main there.")
    parser.add_argument("--install", default=str(INSTALL_DIR), help="the live install's checkout")
    parser.add_argument("--remote", default=REMOTE, help=f"the git remote of the public repo (default: {REMOTE})")
    args = parser.parse_args(argv)
    install = Path(args.install).expanduser()
    try:
        publish(git or Git(install), github or GitHub(), terms or (lambda: all_terms(install / "local")),
                remote=args.remote)
    except (PublishError, GitHubError) as e:
        print(f"publish: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
