"""`python -m kandu.release`: the next version and changelog entry from what was merged, and a release PR only on a
yes, on the private copy (issue #105). git and GitHub are faked."""
import base64
import contextlib
import io
import json
import unittest
import unittest.mock
from datetime import date

from kandu import release
from kandu.github import GitHub, GitHubError

REPO = "acme-org/kandu-copy"
PUBLIC = "acme-org/kandu"
HEAD = "c0ffee"
START = "5eed00"


class FakeGh:
    """What `gh` would answer about one repo's `main`, and every call that would change something."""

    def __init__(self, commits, prs, version="1.0.0", changelog=None):
        self.commits, self.prs = commits, prs
        self.files = {"VERSION": version, "CHANGELOG.md": changelog}
        self.calls, self.writes = [], []

    def __call__(self, args):
        self.calls.append(args)
        if "-X" in args:
            self.writes.append(args)
            path = args[1]
            if path.endswith("/pulls"):
                return json.dumps({"html_url": f"https://github.com/{REPO}/pull/200"})
            return "{}"
        path = args[1]
        if args[:2] == ["api", "graphql"]:
            number = int(next(a for a in args if a.startswith("number=")).split("=")[1])
            return json.dumps({"data": {"repository": {"pullRequest": self.prs.get(number)}}})
        if path == f"repos/{REPO}/git/ref/heads/main":
            return json.dumps({"object": {"sha": HEAD}})
        if path.startswith(f"repos/{REPO}/commits?") and "path=VERSION" in path:
            return json.dumps([{"sha": START}] if self.files["VERSION"] is not None else [])
        if path.startswith(f"repos/{REPO}/compare/{START}...{HEAD}"):
            # GitHub's compare pages its commits: `per_page` of them per `page`, counting from 1.
            query = dict(part.split("=") for part in path.split("?", 1)[1].split("&"))
            size, page = int(query["per_page"]), int(query["page"])
            listed = list(enumerate(self.commits))[(page - 1) * size:page * size]
            return json.dumps({"total_commits": len(self.commits), "commits": [
                {"sha": f"s{i}", "commit": {"message": subject + "\n\nbody"}} for i, subject in listed]})
        if path.startswith(f"repos/{REPO}/contents/"):
            name, ref = path.split("/contents/")[1].split("?ref=")
            text = self.files.get(name) if ref == HEAD else None
            if text is None:
                raise GitHubError("gh api: HTTP 404: Not Found")
            return json.dumps({"content": base64.b64encode(text.encode()).decode(), "sha": "old"})
        raise AssertionError(f"unexpected gh call: {args}")

    def put(self, name):
        put = next(w for w in self.writes if w[1] == f"repos/{REPO}/contents/{name}")
        content = next(a for a in put if a.startswith("content=")).split("=", 1)[1]
        return base64.b64decode(content).decode()


def pr(number, title, kind=None, breaking=False, issue_breaking=False):
    labels = [{"name": "kandu"}] + ([{"name": "breaking"}] if breaking else [])
    issue_labels = [{"name": "agent"}] + ([{"name": "breaking"}] if issue_breaking else [])
    items = [{"kind": {"name": kind} if kind else None}]
    return {"number": number, "title": title, "labels": {"nodes": labels},
            "closingIssuesReferences": {"nodes": [{"number": number - 1, "labels": {"nodes": issue_labels},
                                                   "projectItems": {"nodes": items}}]}}


def remotes(name):
    return {"origin": REPO, "upstream": PUBLIC}.get(name)


def run_release(gh, *argv, answer=False):
    out, err = io.StringIO(), io.StringIO()
    asked = []

    def confirm(question):
        asked.append(question)
        return answer

    github = GitHub(run=gh)
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        if "--yes" in argv:
            code = release.main(list(argv), github=github, remotes=remotes)
        else:
            code = 0
            try:
                release.run(github, REPO, confirm, day=date(2026, 3, 4), upstream=PUBLIC)
            except release.ReleaseError as e:
                print(f"release: {e}", file=err)
                code = 1
    return code, out.getvalue(), err.getvalue(), asked


MIX = ["Fix a typo in the dashboard (#11)", "Add a calm switch (#12)", "Tidy the tests (#13)"]
MIX_PRS = {11: pr(11, "Fix a typo in the dashboard", "Bug"), 12: pr(12, "Add a calm switch", "Feature"),
           13: pr(13, "Tidy the tests", "Chore")}


class Version(unittest.TestCase):
    def proposed(self, commits, prs, version="1.0.0"):
        _, out, _, _ = run_release(FakeGh(commits, prs, version=version))
        return next(line.split(": ")[1] for line in out.splitlines() if line.startswith("Proposed version"))

    def test_a_feature_among_fixes_and_chores_is_minor(self):
        self.assertEqual(self.proposed(MIX, MIX_PRS), "1.1.0")

    def test_only_fixes_and_chores_is_patch(self):
        self.assertEqual(self.proposed(MIX[:1] + MIX[2:], MIX_PRS), "1.0.1")

    def test_a_breaking_pr_is_major_even_beside_a_feature(self):
        prs = MIX_PRS | {13: pr(13, "Rename the config", "Chore", breaking=True)}
        self.assertEqual(self.proposed(MIX, prs, version="1.4.2"), "2.0.0")

    def test_a_breaking_mark_on_the_closed_issue_is_major(self):
        prs = {11: pr(11, "Drop an old flag", "Chore", issue_breaking=True)}
        self.assertEqual(self.proposed(MIX[:1], prs), "2.0.0")

    def test_minor_and_patch_count_from_the_current_version(self):
        self.assertEqual(self.proposed(MIX, MIX_PRS, version="1.4.2"), "1.5.0")
        self.assertEqual(self.proposed(MIX[:1], MIX_PRS, version="1.4.2"), "1.4.3")

    def test_the_first_release_counts_from_1_0_0(self):
        self.assertEqual(self.proposed(MIX, MIX_PRS), "1.1.0")
        self.assertEqual(release.STARTING_VERSION, "1.0.0")

    def test_a_commit_without_a_pr_is_a_patch_change(self):
        self.assertEqual(self.proposed(["Edit the readme by hand"], {}), "1.0.1")


class Proposal(unittest.TestCase):
    def test_a_release_of_more_than_one_page_lists_every_commit_once_in_order(self):
        commits = [f"Fix thing {n} (#{n})" for n in range(1, 151)]
        prs = {n: pr(n, f"Fix thing {n}", "Bug") for n in range(1, 151)}
        prs[150] = pr(150, "Fix thing 150", "Feature")
        gh = FakeGh(commits, prs)
        code, out, _, _ = run_release(gh)
        self.assertEqual(code, 0)
        listed = [line.strip() for line in out.splitlines() if line.startswith("  #")]
        self.assertEqual(listed, [f"#{n} Fix thing {n} (Feature)" if n == 150 else f"#{n} Fix thing {n} (Bug)"
                                  for n in range(1, 151)])
        self.assertIn("Proposed version: 1.1.0", out)

    def test_a_pr_number_with_no_pr_found_keeps_the_number_and_counts_as_other(self):
        code, out, _, _ = run_release(FakeGh(["Edit the readme (#99)"], {}))
        self.assertEqual(code, 0)
        self.assertIn("  #99 Edit the readme\n", out)
        self.assertIn("Proposed version: 1.0.1", out)
        self.assertIn("### Other\n- Edit the readme (#99)\n", out)

    def test_it_lists_the_changes_and_prints_the_entry_grouped_by_change(self):
        prs = MIX_PRS | {14: pr(14, "Rename the config", "Feature", breaking=True)}
        code, out, _, _ = run_release(FakeGh(MIX + ["Rename the config (#14)"], prs))
        self.assertEqual(code, 0)
        self.assertIn("#11 Fix a typo in the dashboard (Bug)", out)
        self.assertIn("#14 Rename the config (Feature, breaking)", out)
        self.assertIn("## 2.0.0 - 2026-03-04\n\n"
                      "### Breaking\n- Rename the config (#14)\n\n"
                      "### Features\n- Add a calm switch (#12)\n\n"
                      "### Fixes\n- Fix a typo in the dashboard (#11)\n\n"
                      "### Other\n- Tidy the tests (#13)\n", out)

    def test_without_a_yes_it_changes_nothing(self):
        gh = FakeGh(MIX, MIX_PRS)
        code, out, _, asked = run_release(gh, answer=False)
        self.assertEqual(code, 0)
        self.assertEqual(len(asked), 1)
        self.assertEqual(gh.writes, [])
        self.assertIn("Nothing changed.", out)

    def test_with_no_change_it_says_so_and_proposes_nothing(self):
        gh = FakeGh([], {})
        _, out, _, asked = run_release(gh)
        self.assertIn("No change since the last release (1.0.0)", out)
        self.assertNotIn("Proposed version", out)
        self.assertEqual(asked, [])
        self.assertEqual(gh.writes, [])

    def test_no_terminal_is_a_no(self):
        with unittest.mock.patch("sys.stdin", io.StringIO("y\n")):
            self.assertFalse(release.ask("Open it?"))


class ReleasePr(unittest.TestCase):
    def test_a_yes_opens_a_pr_on_the_private_copy_with_the_version_and_the_entry(self):
        gh = FakeGh(MIX, MIX_PRS, changelog="# Changelog\n\n## 1.0.0 - 2026-01-01\n\n### Other\n- Start (#1)\n")
        code, out, _, _ = run_release(gh, answer=True)
        self.assertEqual(code, 0)
        self.assertIn(f"Release PR: https://github.com/{REPO}/pull/200", out)
        self.assertEqual(gh.put("VERSION"), "1.1.0\n")
        changelog = gh.put("CHANGELOG.md")
        self.assertTrue(changelog.startswith("# Changelog\n\n## 1.1.0 - 2026-03-04\n"))
        self.assertIn("### Features\n- Add a calm switch (#12)\n", changelog)
        self.assertTrue(changelog.endswith("## 1.0.0 - 2026-01-01\n\n### Other\n- Start (#1)\n"))
        pull = next(w for w in gh.writes if w[1].endswith("/pulls"))
        self.assertIn("head=release/1.1.0", pull)
        self.assertIn("base=main", pull)
        self.assertIn("title=Release 1.1.0", pull)

    def test_the_first_changelog_is_made_when_there_is_none(self):
        gh = FakeGh(MIX, MIX_PRS)
        run_release(gh, answer=True)
        self.assertTrue(gh.put("CHANGELOG.md").startswith("# Changelog\n\n## 1.1.0 - 2026-03-04\n"))

    def test_nothing_is_written_to_main_or_to_the_public_upstream(self):
        gh = FakeGh(MIX, MIX_PRS)
        run_release(gh, answer=True)
        self.assertTrue(gh.writes)
        for write in gh.writes:
            self.assertTrue(write[1].startswith(f"repos/{REPO}/"), write)
            self.assertNotIn("ref=refs/heads/main", write)
            self.assertNotIn("branch=main", write)
        ref = next(w for w in gh.writes if w[1].endswith("/git/refs"))
        self.assertIn("ref=refs/heads/release/1.1.0", ref)
        self.assertIn(f"sha={HEAD}", ref)

    def test_it_refuses_the_public_upstream_as_the_repo(self):
        gh = FakeGh(MIX, MIX_PRS)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = release.main(["--repo", PUBLIC, "--yes"], github=GitHub(run=gh), remotes=remotes)
        self.assertEqual(code, 1)
        self.assertIn("public upstream", err.getvalue())
        self.assertEqual(gh.calls, [])

    def test_yes_on_the_command_line_opens_it_on_the_origin_copy(self):
        gh = FakeGh(MIX, MIX_PRS)
        code, out, _, _ = run_release(gh, "--yes")
        self.assertEqual(code, 0)
        self.assertIn("Release PR:", out)

    def test_a_github_failure_is_a_clear_error(self):
        def broken(args):
            raise GitHubError("gh api: HTTP 502")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = release.main(["--yes"], github=GitHub(run=broken), remotes=remotes)
        self.assertEqual(code, 1)
        self.assertIn("release: gh api: HTTP 502", err.getvalue())


class Remotes(unittest.TestCase):
    def test_it_reads_owner_and_name_from_the_remote_url(self):
        for url in ("https://github.com/acme-org/kandu-copy.git", "git@github.com:acme-org/kandu-copy.git",
                    "https://x:<token>@github.com/acme-org/kandu-copy"):
            done = unittest.mock.Mock(returncode=0, stdout=url + "\n")
            with unittest.mock.patch("subprocess.run", return_value=done):
                self.assertEqual(release.remote_repo("origin"), REPO)

    def test_no_remote_is_none(self):
        with unittest.mock.patch("subprocess.run", return_value=unittest.mock.Mock(returncode=2, stdout="")):
            self.assertIsNone(release.remote_repo("origin"))


if __name__ == "__main__":
    unittest.main()
