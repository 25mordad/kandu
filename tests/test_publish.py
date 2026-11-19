"""`python -m kandu.publish`: tags a release and publishes it on the public upstream, after the deploy made it live.

Git and GitHub are both faked: no test pushes a tag or creates a release anywhere.
"""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from kandu import publish
from kandu.github import GitHub, GitHubError

RELEASE = "c0ffee"   # the merged release commit: the last one that changed VERSION
OLDER = "beef01"
ENTRY = "## 1.1.0 - 2026-11-20\n\n### Features\n- Add a release command (#105)\n"
CHANGELOG = f"# Changelog\n\n{ENTRY}\n## 1.0.1 - 2026-11-01\n\n### Fixes\n- Fix a typo (#90)\n"


class FakeGit:
    """The install's checkout and its upstream remote, as `publish` sees them through `publish.Git`."""

    def __init__(self, version="1.1.0\n", changelog=CHANGELOG, version_commit=RELEASE, head=RELEASE,
                 upstream_main=RELEASE, tags=None, repo="acme/kandu"):
        self.files = {"VERSION": version, "CHANGELOG.md": changelog}
        self.version_commit = version_commit
        self.head_sha = head
        self.upstream_main = upstream_main
        self.tags = dict(tags or {})
        self.repo = repo
        self.pushed = []
        self.history = {RELEASE: {RELEASE}, "later": {"later", RELEASE}, OLDER: {OLDER}}

    def read(self, path):
        return self.files.get(path)

    def last_commit_to(self, path):
        return self.version_commit

    def head(self):
        return self.head_sha

    def contains(self, ancestor, commit):
        return ancestor in self.history.get(commit, {commit})

    def remote_branch(self, remote, branch):
        return self.upstream_main

    def remote_tag(self, remote, tag):
        return self.tags.get(tag)

    def push_tag(self, remote, tag, commit):
        self.pushed.append((remote, tag, commit))
        self.tags[tag] = commit

    def remote_repo(self, remote):
        return self.repo


class FakeGitHub:
    def __init__(self, releases=None):
        self.releases = dict(releases or {})
        self.created = []

    def release(self, repo, tag):
        return self.releases.get((repo, tag))

    def create_release(self, repo, tag, title, notes):
        self.created.append((repo, tag, title, notes))
        self.releases[(repo, tag)] = {"tag_name": tag}
        return f"https://github.com/{repo}/releases/tag/{tag}"


def run(git, github, terms=()):
    lines = []
    result = publish.publish(git, github, lambda: list(terms), out=lines.append)
    return result, lines


class PublishTest(unittest.TestCase):
    def test_a_live_release_is_tagged_then_published_with_its_changelog_entry(self):
        git, github = FakeGit(), FakeGitHub()
        result, _ = run(git, github)
        self.assertEqual(result, "published")
        self.assertEqual(git.pushed, [("upstream", "v1.1.0", RELEASE)])
        self.assertEqual(github.created, [("acme/kandu", "v1.1.0", "Kandu 1.1.0", ENTRY)])

    def test_the_release_commit_is_tagged_even_when_later_merges_are_live_too(self):
        git, github = FakeGit(head="later", upstream_main="later"), FakeGitHub()
        self.assertEqual(run(git, github)[0], "published")
        self.assertEqual(git.pushed, [("upstream", "v1.1.0", RELEASE)])

    def test_nothing_is_tagged_before_the_release_is_live(self):
        git, github = FakeGit(head=OLDER), FakeGitHub()
        with self.assertRaises(publish.PublishError):
            run(git, github)
        self.assertEqual((git.pushed, github.created), ([], []))

    def test_nothing_is_tagged_before_main_is_on_the_upstream(self):
        for upstream_main in (OLDER, None):
            with self.subTest(upstream_main=upstream_main):
                git, github = FakeGit(upstream_main=upstream_main), FakeGitHub()
                with self.assertRaises(publish.PublishError):
                    run(git, github)
                self.assertEqual((git.pushed, github.created), ([], []))

    def test_a_private_term_in_the_changelog_entry_stops_the_tag_and_the_release(self):
        git, github = FakeGit(), FakeGitHub()
        with self.assertRaises(publish.PublishError) as caught:
            run(git, github, terms=["release COMMAND"])
        self.assertEqual((git.pushed, github.created), ([], []))
        self.assertNotIn("release COMMAND", str(caught.exception))

    def test_a_private_term_in_the_tag_name_stops_the_tag_and_the_release(self):
        git, github = FakeGit(), FakeGitHub()
        with self.assertRaises(publish.PublishError):
            run(git, github, terms=["v1.1.0"])
        self.assertEqual((git.pushed, github.created), ([], []))

    def test_unreadable_private_terms_stop_the_tag_and_the_release(self):
        def broken():
            raise OSError("cannot read local/")
        git, github = FakeGit(), FakeGitHub()
        with self.assertRaises(publish.PublishError):
            publish.publish(git, github, broken, out=lambda line: None)
        self.assertEqual((git.pushed, github.created), ([], []))

    def test_an_ordinary_merge_creates_no_tag_and_no_release(self):
        # The version has no changelog entry: only a release PR adds one, so this merge is not a release.
        for changelog in (None, "# Changelog\n\n## 1.0.1 - 2026-11-01\n\n### Fixes\n- Fix a typo (#90)\n"):
            with self.subTest(changelog=changelog):
                git, github = FakeGit(version="1.0.0\n", changelog=changelog), FakeGitHub()
                result, lines = run(git, github)
                self.assertEqual(result, "not_a_release")
                self.assertEqual((git.pushed, github.created), ([], []))
                self.assertTrue(lines)

    def test_an_ordinary_merge_after_a_published_release_creates_nothing_new(self):
        git = FakeGit(head="later", upstream_main="later", tags={"v1.1.0": RELEASE})
        github = FakeGitHub({("acme/kandu", "v1.1.0"): {"tag_name": "v1.1.0"}})
        self.assertEqual(run(git, github)[0], "already_published")
        self.assertEqual((git.pushed, github.created), ([], []))

    def test_running_again_for_the_same_release_makes_no_duplicate(self):
        git, github = FakeGit(), FakeGitHub()
        run(git, github)
        self.assertEqual(run(git, github)[0], "already_published")
        self.assertEqual(len(git.pushed), 1)
        self.assertEqual(len(github.created), 1)

    def test_a_tag_pushed_by_a_run_that_stopped_gets_its_release_without_a_second_tag(self):
        git, github = FakeGit(tags={"v1.1.0": RELEASE}), FakeGitHub()
        self.assertEqual(run(git, github)[0], "published")
        self.assertEqual(git.pushed, [])
        self.assertEqual(len(github.created), 1)

    def test_a_tag_of_the_same_name_on_another_commit_stops_the_release(self):
        git, github = FakeGit(tags={"v1.1.0": OLDER}), FakeGitHub()
        with self.assertRaises(publish.PublishError):
            run(git, github)
        self.assertEqual((git.pushed, github.created), ([], []))

    def test_an_upstream_that_is_not_on_github_stops_before_the_tag(self):
        git, github = FakeGit(repo=None), FakeGitHub()
        with self.assertRaises(publish.PublishError):
            run(git, github)
        self.assertEqual((git.pushed, github.created), ([], []))


class ChangelogEntryTest(unittest.TestCase):
    def test_the_entry_of_a_version_runs_to_the_next_entry(self):
        self.assertEqual(publish.changelog_entry(CHANGELOG, "1.1.0"), ENTRY)
        self.assertEqual(publish.changelog_entry(CHANGELOG, "1.0.1"),
                         "## 1.0.1 - 2026-11-01\n\n### Fixes\n- Fix a typo (#90)\n")

    def test_no_entry(self):
        self.assertIsNone(publish.changelog_entry(CHANGELOG, "1.1"))
        self.assertIsNone(publish.changelog_entry(CHANGELOG, "2.0.0"))
        self.assertIsNone(publish.changelog_entry("", "1.0.0"))


class Completed:
    def __init__(self, code=0, out=""):
        self.returncode, self.stdout, self.stderr = code, out, "fatal: no" if code else ""


class GitTest(unittest.TestCase):
    """`publish.Git` asks git the right questions; `run` stands in for git itself."""

    def git(self, *answers):
        calls, answers = [], list(answers)

        def fake(args, **kwargs):
            calls.append(args)
            return answers.pop(0)
        return publish.Git("/install", run=fake), calls

    def test_a_tag_is_pushed_to_the_remote_from_the_commit_without_a_local_tag(self):
        git, calls = self.git(Completed())
        git.push_tag("upstream", "v1.1.0", RELEASE)
        self.assertEqual(calls, [["git", "-C", "/install", "push", "upstream", f"{RELEASE}:refs/tags/v1.1.0"]])

    def test_a_refused_push_raises(self):
        git, _ = self.git(Completed(1))
        with self.assertRaises(publish.PublishError):
            git.push_tag("upstream", "v1.1.0", RELEASE)

    def test_a_remote_tag_or_none(self):
        git, calls = self.git(Completed(0, f"{OLDER}\trefs/tags/v1.1.0\n{RELEASE}\trefs/tags/v1.1.0^{{}}\n"),
                              Completed(0, ""))
        self.assertEqual(git.remote_tag("upstream", "v1.1.0"), RELEASE)
        self.assertIsNone(git.remote_tag("upstream", "v1.1.0"))
        self.assertEqual(calls[0], ["git", "-C", "/install", "ls-remote", "upstream",
                                    "refs/tags/v1.1.0", "refs/tags/v1.1.0^{}"])

    def test_a_remote_branch_or_none(self):
        git, _ = self.git(Completed(0, f"{RELEASE}\trefs/heads/main\n"), Completed(0, ""))
        self.assertEqual(git.remote_branch("upstream", "main"), RELEASE)
        self.assertIsNone(git.remote_branch("upstream", "main"))

    def test_contains(self):
        git, calls = self.git(Completed(0), Completed(1), Completed(128))
        self.assertTrue(git.contains(RELEASE, "later"))
        self.assertFalse(git.contains(RELEASE, OLDER))
        self.assertFalse(git.contains(RELEASE, "unknown"))
        self.assertEqual(calls[0], ["git", "-C", "/install", "merge-base", "--is-ancestor", RELEASE, "later"])

    def test_the_remote_repo_on_github(self):
        git, _ = self.git(Completed(0, "git@github.com:acme/kandu.git\n"), Completed(0, "https://example.com/x\n"))
        self.assertEqual(git.remote_repo("upstream"), "acme/kandu")
        self.assertIsNone(git.remote_repo("upstream"))

    def test_read_a_file_of_the_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "VERSION").write_text("1.1.0\n", encoding="utf-8")
            git = publish.Git(tmp, run=lambda *a, **k: self.fail("git is not run to read a file"))
            self.assertEqual(git.read("VERSION"), "1.1.0\n")
            self.assertIsNone(git.read("CHANGELOG.md"))


class GitHubReleaseTest(unittest.TestCase):
    def test_a_release_or_none(self):
        calls = []

        def gh(args):
            calls.append(args)
            return '{"tag_name": "v1.1.0"}'
        self.assertEqual(GitHub(gh).release("acme/kandu", "v1.1.0"), {"tag_name": "v1.1.0"})
        self.assertEqual(calls, [["api", "repos/acme/kandu/releases/tags/v1.1.0"]])

        def missing(args):
            raise GitHubError("gh api: gh: Not Found (HTTP 404)")
        self.assertIsNone(GitHub(missing).release("acme/kandu", "v1.1.0"))

        def busy(args):
            raise GitHubError("gh api: HTTP 502")
        with self.assertRaises(GitHubError):
            GitHub(busy).release("acme/kandu", "v1.1.0")

    def test_create_a_release_of_an_existing_tag(self):
        calls = []

        def gh(args):
            calls.append(args)
            return '{"html_url": "https://github.com/acme/kandu/releases/tag/v1.1.0"}'
        url = GitHub(gh).create_release("acme/kandu", "v1.1.0", "Kandu 1.1.0", ENTRY)
        self.assertEqual(url, "https://github.com/acme/kandu/releases/tag/v1.1.0")
        self.assertEqual(calls, [["api", "repos/acme/kandu/releases", "-X", "POST", "-f", "tag_name=v1.1.0",
                                  "-f", "name=Kandu 1.1.0", "-f", f"body={ENTRY}"]])


class MainTest(unittest.TestCase):
    def setUp(self):
        quiet = contextlib.ExitStack()
        quiet.enter_context(contextlib.redirect_stdout(io.StringIO()))
        quiet.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.addCleanup(quiet.close)

    def test_a_failure_exits_non_zero_so_the_deploy_fails(self):
        git, github = FakeGit(head=OLDER), FakeGitHub()
        self.assertEqual(publish.main([], git=git, github=github, terms=lambda: []), 1)

    def test_success_and_nothing_to_do_exit_zero(self):
        self.assertEqual(publish.main([], git=FakeGit(), github=FakeGitHub(), terms=lambda: []), 0)
        self.assertEqual(publish.main([], git=FakeGit(changelog=None), github=FakeGitHub(), terms=lambda: []), 0)


if __name__ == "__main__":
    unittest.main()
