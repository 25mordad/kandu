import contextlib
import io
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from kandu import bazras, brief, config, dashboard, didban, timer
from kandu.config import ConfigError, DidbanBrain, DidbanConfig, load_config
from kandu.runner import AgentResult, RunnerError
from kandu.state import StateStore
from kandu.usage import Usage

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 1, 2, 9, 0, tzinfo=timezone.utc)
F, R = bazras.FIELD, bazras.RECORD


def log_entry(sha, when, subject, body="", files=(), parents="p1"):
    return f"{R}{sha}{F}{parents}{F}{when.isoformat()}{F}{subject}{F}{body}{F}\n" + "".join(f"{f}\n" for f in files)


def done(stdout="", code=0):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr="")


class FakeHost:
    """Answers `systemctl show`, `journalctl` and `crontab -l` from a table."""

    def __init__(self, shows=None, journal="", crontab="", crontab_code=0):
        self.shows, self.journal, self.crontab, self.crontab_code = shows or {}, journal, crontab, crontab_code
        self.commands = []

    def check(self, command, cwd=None):
        self.commands.append(command)
        return self.shows.get(command[command.index("show") + 1], "LoadState=not-found\n")

    def run(self, command, cwd=None):
        self.commands.append(command)
        if command[0] == "journalctl":
            return done(self.journal)
        if command[0] == "crontab":
            return done(self.crontab, self.crontab_code)
        return done()


class CommitTest(unittest.TestCase):
    def test_parse_log_reads_each_commit_and_its_files(self):
        text = log_entry("a" * 40, NOW, "Add it", "Kandu-Run: run-1", ["x.py", "y.py"]) + log_entry("b" * 40, NOW, "Merge", parents="p1 p2")
        first, second = bazras.parse_log(text)
        self.assertEqual((first.kandu_run, first.files, first.parents), ("run-1", ["x.py", "y.py"], 1))
        self.assertEqual((second.kandu_run, second.parents), (None, 2))

    def commits(self, *entries):
        return bazras.parse_log("".join(entries))

    def test_a_revert_of_a_kandu_commit_is_a_finding(self):
        commits = self.commits(log_entry("b" * 40, NOW - timedelta(hours=1), 'Revert "Fix"', f"This reverts commit {'a' * 40}."),
                               log_entry("a" * 40, NOW - timedelta(days=2), "Fix", "Kandu-Run: run-1", ["x.py"]))
        findings, mine = bazras.commit_findings(commits, NOW)
        self.assertEqual(len(findings), 1)
        self.assertIn("was reverted", findings[0].summary)
        self.assertIn("(Fix)", findings[0].summary)
        self.assertEqual(mine, [])

    def test_a_revert_of_an_older_kandu_commit_is_asked_about(self):
        commits = self.commits(log_entry("b" * 40, NOW, "Revert", f"This reverts commit {'c' * 40}."))
        self.assertEqual(len(bazras.commit_findings(commits, NOW, is_kandu=lambda sha: True)[0]), 1)
        self.assertEqual(bazras.commit_findings(commits, NOW, is_kandu=lambda sha: False)[0], [])

    def test_rework_of_kandus_files_is_a_finding_minus_ignored_files_and_merges(self):
        commits = self.commits(
            log_entry("c" * 40, NOW - timedelta(hours=1), "Hand fix", files=["app.py", "CHANGELOG.md", "docs/a.md"]),
            log_entry("d" * 40, NOW - timedelta(hours=1), "Merge", files=["app.py"], parents="p1 p2"),
            log_entry("a" * 40, NOW - timedelta(days=3), "Feature", "Kandu-Run: run-1", ["app.py", "CHANGELOG.md", "docs/a.md"]),
        )
        findings, _ = bazras.commit_findings(commits, NOW, ignore=["CHANGELOG.md", "docs/*"])
        self.assertEqual(len(findings), 1)
        self.assertIn("app.py", findings[0].summary)
        self.assertNotIn("CHANGELOG", findings[0].summary)

    def test_old_rework_and_kandus_own_commits_are_no_finding_and_todays_kandu_commits_are_listed(self):
        commits = self.commits(
            log_entry("e" * 40, NOW - timedelta(hours=2), "Next", "Kandu-Run: run-2", ["app.py"]),
            log_entry("c" * 40, NOW - timedelta(days=2), "Hand fix", files=["app.py"]),
            log_entry("a" * 40, NOW - timedelta(days=3), "Feature", "Kandu-Run: run-1", ["app.py"]),
        )
        findings, mine = bazras.commit_findings(commits, NOW)
        self.assertEqual(findings, [])
        self.assertEqual(mine, [f"{'e' * 9} Next (Kandu-Run run-2)"])


class UnitTest(unittest.TestCase):
    def test_a_unit_not_loaded_is_one_finding(self):
        findings = bazras.unit_findings(FakeHost(), "site.service")
        self.assertEqual(len(findings), 1)
        self.assertIn("not loaded", findings[0].summary)

    def test_a_failed_service_and_its_errors_are_counted_without_quoting_them(self):
        host = FakeHost({"site.service": "LoadState=loaded\nActiveState=failed\nResult=exit-code\n"},
                        journal="boom: password=hunter2\nagain\n")
        findings = bazras.unit_findings(host, "site.service")
        self.assertEqual(len(findings), 2)
        self.assertIn("2 error line(s)", findings[1].summary)
        self.assertNotIn("hunter2", findings[1].summary)
        self.assertIn("hunter2", findings[1].detail)

    def test_a_user_timer_checks_itself_and_the_service_it_starts(self):
        host = FakeHost({"backup.timer": "LoadState=loaded\nActiveState=inactive\nResult=success\nTriggers=backup.service\n",
                         "backup.service": "Result=exit-code\n"})
        findings = bazras.unit_findings(host, "user:backup.timer")
        self.assertEqual(len(findings), 2)
        self.assertIn("inactive", findings[0].summary)
        self.assertIn("backup.service", findings[1].summary)
        self.assertTrue(all("--user" in c for c in host.commands))
        self.assertIn("backup.service", host.commands[-1])  # its log is the service's

    def test_a_healthy_unit_is_no_finding(self):
        host = FakeHost({"site.service": "LoadState=loaded\nActiveState=active\nResult=success\n"})
        self.assertEqual(bazras.unit_findings(host, "site.service"), [])

    def test_a_cron_line_missing_or_commented_out_is_a_finding(self):
        host = FakeHost(crontab="# 0 3 * * * report\n0 4 * * * backup\n")
        findings = bazras.cron_findings(host, ["report", "backup"])
        self.assertEqual([f.summary for f in findings], ["No crontab line has `report`."])
        self.assertEqual(len(bazras.cron_findings(FakeHost(crontab_code=1), ["backup"])), 1)
        self.assertEqual(bazras.cron_findings(FakeHost(), []), [])


class LogHost:
    """A log file as `stat` and `tail` see it: its inode, and its whole text."""

    def __init__(self, text, inode="7"):
        self.text, self.inode, self.reads = text, inode, []

    def absolute(self, path):
        return path

    def run(self, command, cwd=None):
        data = self.text.encode()
        if command[0] == "stat":
            return done(f"{self.inode} {len(data)}\n")
        self.reads.append(command)
        if command[0] == "sh":
            since, limit = int(command[-2]), int(command[-1])
            return done("\n".join(data[since - 1:].decode().splitlines()[-limit:]))
        return done("\n".join(self.text.splitlines()[-int(command[2]):]))


class LogTest(unittest.TestCase):
    project = SimpleNamespace(path="/srv/site")

    def test_the_first_look_reads_the_end_and_later_looks_only_what_was_added(self):
        host, offsets = LogHost("ok\n2026-01-01 10:00:01 ERROR db timeout after 30s (id 4f9a2b)\n"), {}
        first = bazras.log_findings(host, self.project, "logs/app.log", offsets)
        self.assertEqual(len(first), 1)
        self.assertIn("1 error line(s) in logs/app.log in its newest", first[0].summary)
        self.assertEqual(first[0].kinds, [("ERROR db timeout after Ns (id N)", 1)])
        self.assertEqual(bazras.log_findings(host, self.project, "logs/app.log", offsets), [])
        host.text += "".join(f"2026-01-02 09:0{i}:00 ERROR db timeout after {i}s\n" for i in range(3)) + "Traceback: boom\nfine\n"
        later = bazras.log_findings(host, self.project, "logs/app.log", offsets)
        self.assertIn("4 error line(s) in logs/app.log since the last look", later[0].summary)
        self.assertEqual(later[0].kinds[0], ("ERROR db timeout after Ns", 3))

    def test_a_rotated_log_is_read_from_its_start(self):
        host, offsets = LogHost("x\n" * 50), {}
        bazras.log_findings(host, self.project, "/var/log/app.log", offsets)
        host.text, host.inode = "FATAL out of memory\n", "8"
        found = bazras.log_findings(host, self.project, "/var/log/app.log", offsets)
        self.assertIn("since it was rotated", found[0].summary)

    def test_kinds_drop_times_numbers_and_quoted_values(self):
        lines = ['Jan  2 10:00:00 host app: user "a@b.c" not found', "Jan  2 10:00:05 host app: user 'x' not found"]
        self.assertEqual(bazras.kinds(lines), [('host app: user "…" not found', 2)])

    def test_the_report_shows_the_kinds_scrubbed(self):
        finding = bazras.Finding("log", "2 error line(s) in logs/app.log since the last look.", "raw",
                                 [("login failed for a.b@example.com", 2)])
        text = bazras.render(bazras.Look("website", findings=[finding]), "2026-01-02")
        self.assertIn("   - Seen 2×: `login failed for [address]`", text)
        self.assertNotIn("raw", text)
        self.assertEqual(bazras.summary(text), (False, 1))


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.local = self.root / "local"
        self.local.mkdir()
        shutil.copy(ROOT / "config.example" / "kandu.example.yaml", self.local / "kandu.yaml")
        shutil.copy(ROOT / "config.example" / "projects.example.yaml", self.local / "projects.yaml")
        self.config = load_config(self.local)
        self.project = self.config.projects[0]
        self.project.inspect.enabled = True  # Off in the example, as everywhere until the user turns it on.
        self.store = StateStore(self.root / "state")
        self.logs = self.root / "logs"
        self.logs.mkdir()

    def edit_projects(self, change):
        path = self.local / "projects.yaml"
        data = yaml.safe_load(path.read_text())
        change(data["projects"])
        path.write_text(yaml.safe_dump(data))
        return load_config(self.local)


class ConfigTest(Base):
    def test_the_example_holds_an_inspect_block_and_it_is_off(self):
        self.assertFalse(load_config(self.local).projects[0].inspect.enabled)
        self.assertEqual(bazras.inspected(load_config(self.local)), [])
        self.assertEqual(self.project.inspect.at, "07:00")
        self.assertIn("user:website-backup.timer", self.project.inspect.units)
        self.assertEqual(self.config.settings.bazras.at, "06:00")
        self.assertIsNone(self.config.projects[1].inspect)

    def test_bad_hours_and_units_are_refused(self):
        for key, value in (("at", "25:00"), ("units", ["no-suffix"]), ("checks", [""])):
            with self.subTest(key=key), self.assertRaises(ConfigError):
                self.edit_projects(lambda projects: projects[0]["inspect"].__setitem__(key, value))


class SetInspectTest(Base):
    def test_an_existing_block_is_turned_on_and_its_hour_changed_and_comments_stay(self):
        config.set_inspect("website", True, "08:30", self.local)
        text = (self.local / "projects.yaml").read_text()
        self.assertIn("# optional; Bazras's daily read-only look", text)
        project = load_config(self.local).projects[0]
        self.assertEqual((project.inspect.enabled, project.inspect.at), (True, "08:30"))
        self.assertIn("user:website-backup.timer", project.inspect.units)
        config.set_inspect("website", False, None, self.local)
        project = load_config(self.local).projects[0]
        self.assertEqual((project.inspect.enabled, project.inspect.at), (False, "08:30"))

    def test_a_project_without_a_block_gets_one_with_only_these_lines(self):
        before = load_config(self.local).projects[2:]
        config.set_inspect("internal-tool", True, "05:00", self.local)
        after = load_config(self.local)
        tool = next(p for p in after.projects if p.name == "internal-tool")
        self.assertEqual((tool.inspect.enabled, tool.inspect.at, tool.inspect.tests, tool.inspect.units), (True, "05:00", True, []))
        self.assertEqual(tool.paused, False)
        self.assertEqual([p.name for p in after.projects[2:]], [p.name for p in before])

    def test_a_block_without_enabled_gets_the_line(self):
        path = self.local / "projects.yaml"
        path.write_text(path.read_text().replace("      enabled: false", "", 1))
        self.assertFalse(load_config(self.local).projects[0].inspect.enabled)
        config.set_inspect("website", True, None, self.local)
        self.assertTrue(load_config(self.local).projects[0].inspect.enabled)

    def test_bad_input_changes_nothing(self):
        before = (self.local / "projects.yaml").read_text()
        for name, at in (("website", "7am"), ("nope", None)):
            with self.subTest(name=name), self.assertRaises(ConfigError):
                config.set_inspect(name, True, at, self.local)
        path = self.local / "projects.yaml"
        path.write_text(before.replace("    inspect:  ", '    inspect: {enabled: false}  ', 1).split("      enabled: false")[0] + "\n")
        with self.assertRaises(ConfigError):
            config.set_inspect("website", True, None, self.local)


class FakeRunner:
    def __init__(self, test_code=0, check_code=0, reachable=True, log=""):
        self.test_code, self.check_code, self._reachable, self.log = test_code, check_code, reachable, log
        self.agents = []
        self.host = mock.Mock()
        self.host.absolute.side_effect = lambda path: path
        self.host.run.side_effect = self.run
        self.host.succeeds.return_value = False
        self.host.check.side_effect = self.check
        self.hosts = {"server": self.host, "laptop": self.host}
        self.reply = AgentResult(text='{"findings": []}', is_error=False)

    def check(self, command, cwd=None):
        if command[:2] == ["git", "log"]:
            return self.log
        if "show" in command:
            return "LoadState=loaded\nActiveState=active\nResult=success\n"
        return ""

    def run(self, command, cwd=None):
        if command[0] == "crontab":
            return done("0 3 * * * website-report\n")
        if command[:2] == ["git", "symbolic-ref"]:
            return done("origin/main\n")
        if command[0] == "stat":
            return done("7 100\n")
        return done()

    def reachable(self, machine):
        return self._reachable

    def prepare_review(self, project, repo, branch, head):
        return "/checkout"

    def run_command(self, project, cwd, command, log_name, secrets=False):
        Path(self.logs, f"{log_name}.log").write_text("FAILED test_x\n")
        return self.test_code if command == project.test else self.check_code

    def run_agent(self, machine, cwd, prompt, allowed, denied, run_id, should_stop, model=None, schema=None, **_):
        self.agents.append(SimpleNamespace(cwd=cwd, prompt=prompt, allowed=allowed, denied=denied, schema=schema))
        return self.reply


class LookTest(Base):
    def runner(self, **kwargs):
        runner = FakeRunner(**kwargs)
        runner.logs = self.logs
        return runner

    def inspect(self, runner, usage=lambda: Usage(1, 1)):
        with mock.patch.object(bazras, "read_usage", usage):
            return bazras.inspect_project(self.config, self.store, runner, self.project, self.local, self.logs, NOW,
                                          usage_reader=usage)

    def test_nothing_found_is_the_one_line_and_no_agent_runs(self):
        runner = self.runner()
        result = self.inspect(runner)
        self.assertEqual(runner.agents, [])
        text = bazras.reports(self.project.name, self.local)[-1][1]
        self.assertEqual(text.splitlines()[-1], bazras.ALL_WELL)
        event = [e for e in self.store.events(self.store.today()) if e["type"] == "project_inspected"]
        self.assertEqual((event[0]["passed"], event[0]["findings"], event[0]["judged"]), (True, 0, False))
        self.assertIsNone(self.store.read_bazras())
        self.assertEqual(result.findings, [])

    def test_failing_tests_and_checks_are_judged_with_fixed_read_only_tools(self):
        runner = self.runner(test_code=1, check_code=2)
        runner.reply = AgentResult(is_error=False, text='{"findings": [{"n": 1, "what": "Tests fail.", "cause": "Commit abc.",'
                                   ' "task": {"title": "Fix the tests", "body": "## Goal\\nGreen."}}, {"n": 2, "what": "Noise.", "cause": "None.", "task": null}]}')
        result = self.inspect(runner)
        self.assertEqual([f.check for f in result.findings], ["tests", "check"])
        agent = runner.agents[0]
        self.assertEqual(agent.allowed, bazras.TOOLS)
        for tool in ("Edit", "Write", "Bash(gh *)", "Bash(git push *)"):
            self.assertIn(tool, agent.denied)
        self.assertEqual(agent.schema, bazras.SCHEMA)
        self.assertIn("FAILED test_x", agent.prompt)
        text = bazras.reports(self.project.name, self.local)[-1][1]
        self.assertIn("1. The tests fail on main", text)
        self.assertIn("Proposed task: **Fix the tests**", text)
        self.assertNotIn("FAILED test_x", text)
        runs = [e for e in self.store.events(self.store.today()) if e["type"] == "run_finished"]
        self.assertEqual((runs[0]["role"], runs[0]["task"], runs[0]["project"]), ("bazras", None, self.project.name))

    def test_the_judge_is_skipped_by_the_stop_file_the_daily_limit_and_the_usage_limit(self):
        def stopped():
            self.store.root.mkdir(parents=True, exist_ok=True)
            self.store.stop_file.touch()

        def spent():
            self.store.stop_file.unlink(missing_ok=True)
            self.project.daily_token_limit = 0

        cases = [(stopped, lambda: Usage(1, 1), "Kandu is stopped"),
                 (spent, lambda: Usage(1, 1), "the project reached its daily limit")]
        for prepare, usage, why in cases:
            prepare()
            runner = self.runner(test_code=1)
            self.inspect(runner, usage)
            self.assertEqual(runner.agents, [])
            self.assertIn(f"Not judged: {why}", bazras.reports(self.project.name, self.local)[-1][1])
        self.project.daily_token_limit = 10 ** 9
        runner = self.runner(test_code=1)
        self.inspect(runner, lambda: Usage(99, 99))
        self.assertEqual(runner.agents, [])
        self.assertIn("Not judged:", bazras.reports(self.project.name, self.local)[-1][1])

    def test_an_unreachable_machine_is_not_a_finding_but_is_said(self):
        result = self.inspect(self.runner(reachable=False))
        self.assertEqual(result.findings, [])
        text = bazras.reports(self.project.name, self.local)[-1][1]
        self.assertIn("Bazras could not check:", text)
        self.assertNotIn(bazras.ALL_WELL, text)

    def test_a_failing_git_command_is_trouble_not_a_crash(self):
        runner = self.runner()
        runner.host.check.side_effect = RunnerError("no repo")
        result = self.inspect(runner)
        self.assertTrue(result.trouble)


class ReportTest(unittest.TestCase):
    def test_scrub_hides_addresses_keys_and_tokens_but_keeps_shas_and_paths(self):
        sha = "0123456789abcdef0123456789abcdef01234567"
        text = bazras.scrub(f"mail a.b@example.com API_KEY=abc123 tok ghp_{'a1' * 20} {sha} /usr/lib/x/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1",
                            names=["SHOP_TOKEN"])
        self.assertNotIn("example.com", text)
        self.assertIn("API_KEY=[hidden]", text)
        self.assertNotIn("a1a1a1", text)
        self.assertIn(sha, text)
        self.assertIn("/usr/lib/x/", text)
        self.assertIn("SHOP_TOKEN=[hidden]", bazras.scrub("SHOP_TOKEN: xyz", names=["SHOP_TOKEN"]))

    def test_render_scrubs_what_the_judge_wrote(self):
        result = bazras.Look("website", findings=[bazras.Finding("tests", "The tests fail.")],
                             judged=[{"what": "Ask a.b@example.com", "cause": "x", "task": None}])
        self.assertIn("[address]", bazras.render(result, "2026-01-02"))

    def test_parse_judgment_keeps_only_findings_it_knows(self):
        judged = bazras.parse_judgment('{"findings": [{"n": 2, "what": "w", "cause": "c", "task": null}, {"n": 9}]}', 2)
        self.assertEqual(judged[0], None)
        self.assertEqual(judged[1]["what"], "w")


class ScheduleTest(Base):
    def test_due_after_its_own_hour_once_a_day(self):
        self.assertFalse(bazras.due(self.config, self.project, self.local, NOW.replace(hour=6, minute=59)))
        self.assertTrue(bazras.due(self.config, self.project, self.local, NOW.replace(hour=7)))
        bazras.write_report(self.project.name, NOW.date().isoformat(), "x", self.local)
        self.assertFalse(bazras.due(self.config, self.project, self.local, NOW.replace(hour=23)))

    def test_without_its_own_hour_it_takes_bazras_at(self):
        self.project.inspect.at = None
        self.assertTrue(bazras.due(self.config, self.project, self.local, NOW.replace(hour=6)))

    def test_paused_projects_and_those_off_or_without_inspect_are_not_looked_at(self):
        runner = FakeRunner()
        runner.logs = self.logs
        self.store.pause_project(self.project.name)
        self.assertEqual(bazras.run(self.config, self.store, runner, self.local, self.logs, now=NOW.replace(hour=8)), [])
        self.assertEqual(bazras.inspected(self.config), [self.project])
        self.project.inspect.enabled = False
        self.assertEqual(bazras.inspected(self.config), [])
        self.project.inspect.enabled = True
        self.project.paused = True
        self.assertEqual(bazras.inspected(self.config), [])


class ReadingTest(Base):
    def test_unread_list_and_brief_show_marks_them_read(self):
        bazras.write_report(self.project.name, "2026-01-01", f"# Bazras\n\n{bazras.ALL_WELL}\n", self.local)
        bazras.write_report(self.project.name, "2026-01-02", "# Bazras\n\n1. The tests fail.\n2. x\n", self.local)
        self.assertEqual(bazras.waiting(self.config, self.local), ["website: 2 finding(s) in 1 report(s), newest 2026-01-02"])
        with mock.patch.object(brief.Host, "absolute", side_effect=lambda path: path), \
                mock.patch.object(brief, "read_report", return_value="r"):
            text = brief.show(self.config, self.project.name, self.local)
        self.assertIn("New from Bazras: 2 report(s)", text)
        self.assertIn("1. The tests fail.", text)
        self.assertEqual(bazras.unread(self.project.name, self.local), [])
        self.assertEqual(bazras.waiting(self.config, self.local), [])
        with mock.patch.object(brief, "read_report", return_value="r"):
            again = brief.show(self.config, self.project.name, self.local)
        self.assertIn("nothing new since its look of 2026-01-02", again)

    def run_cli(self, *args):
        out = io.StringIO()
        with mock.patch("sys.argv", ["bazras", *args, "--local", str(self.local)]), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = bazras.main()
        return code, out.getvalue()

    def test_list_and_show_commands(self):
        bazras.write_report(self.project.name, "2026-01-02", "# Bazras\n\n1. The tests fail.\n", self.local)
        self.assertEqual(self.run_cli("list"), (0, "website: 1 finding(s) in 1 report(s), newest 2026-01-02\n"))
        code, out = self.run_cli("show", "--project", "website")
        self.assertEqual(code, 0)
        self.assertIn("1. The tests fail.", out)
        self.assertEqual(bazras.unread(self.project.name, self.local)[0][0], "2026-01-02")  # show marks nothing
        self.assertEqual(self.run_cli("show", "--project", "../x")[0], 1)


class DidbanTest(unittest.TestCase):
    def test_the_prompt_lists_bazras_and_the_project_opened_from_it(self):
        config = DidbanConfig(brain=DidbanBrain(reach="ssh server", install_dir="~/k"))
        text = didban.system_prompt(config, "ok", bazras="website: 1 finding(s)", project="website")
        self.assertIn("Bazras's findings not read yet, written by an agent: website: 1 finding(s)", text)
        self.assertIn("Bazras's reports on `website`", text)
        self.assertNotIn("Bazras's reports on `", didban.system_prompt(config, "ok").split("## This setup")[1])

    def test_didban_decides_and_creates_a_task_only_on_a_yes(self):
        text = (ROOT / "prompts" / "didban.md").read_text(encoding="utf-8")
        section = text.split("## Bazras's findings")[1].split("\n## ")[0]
        for words in ("first reply", "worth a task", "user's yes", "data", "kandu.bazras show"):
            self.assertIn(words, section)


class DashboardTest(Base):
    def setUp(self):
        super().setUp()
        config.set_inspect("website", True, None, self.local)
        self.config = load_config(self.local)
        self.api = dashboard.Api(self.config, self.store, mock.Mock(), self.logs, "t", timer_on=lambda: True,
                                 local_dir=self.local, flights_=mock.Mock())

    def test_reports_and_the_state_line(self):
        bazras.write_report(self.project.name, "2026-01-02", "# Bazras\n\n1. The tests fail.\n", self.local)
        status, body = dashboard.dispatch(self.api, "GET", "/api/v1/bazras")
        self.assertEqual(status, 200)
        self.assertEqual(body["projects"][0]["name"], "website")
        self.assertEqual([p["enabled"] for p in body["projects"]][:2], [True, False])
        self.assertEqual(body["projects"][0]["at"], "07:00")
        self.assertEqual(body["projects"][0]["reports"][0]["findings"], 1)
        state = dashboard.dispatch(self.api, "GET", "/api/v1/state")[1]["bazras"]
        self.assertEqual(state["last"]["website"]["date"], "2026-01-02")
        self.assertIsNone(state["running"])

    def test_the_switch_writes_the_config_and_is_logged(self):
        status, body = dashboard.dispatch(self.api, "POST", "/api/v1/projects/internal-tool/inspect?enabled=true&at=05:30", "t")
        self.assertEqual((status, body), (200, {"enabled": True, "at": "05:30"}))
        self.assertTrue(next(p for p in load_config(self.local).projects if p.name == "internal-tool").inspect.enabled)
        event = [e for e in self.store.events(self.store.today()) if e["type"] == "inspect_changed"][0]
        self.assertEqual((event["project"], event["enabled"], event["at"]), ("internal-tool", True, "05:30"))
        for target in ("/api/v1/projects/website/inspect?enabled=maybe", "/api/v1/projects/website/inspect?enabled=true&at=99:00"):
            self.assertEqual(dashboard.dispatch(self.api, "POST", target, "t")[0], 400)
        self.assertEqual(dashboard.dispatch(self.api, "POST", "/api/v1/projects/website/inspect?enabled=true")[0], 403)

    def test_didban_opens_on_a_known_project_only(self):
        self.assertEqual(self.api.open_didban({"project": ["website"]}), {"opening": "didban"})
        self.api.flights.open_didban.assert_called_with(mock.ANY, "website")
        with self.assertRaises(dashboard.ApiError):
            self.api.open_didban({"project": ["nope"]})


def load_config_from_examples():
    folder = Path(tempfile.mkdtemp())
    shutil.copy(ROOT / "config.example" / "kandu.example.yaml", folder / "kandu.yaml")
    shutil.copy(ROOT / "config.example" / "projects.example.yaml", folder / "projects.yaml")
    try:
        return load_config(folder)
    finally:
        shutil.rmtree(folder)


class TimerTest(unittest.TestCase):
    def test_the_timer_wakes_every_fifteen_minutes_and_runs_bazras(self):
        config = load_config_from_examples()
        self.assertIn("-m kandu.bazras", timer.render_bazras_service(config))
        self.assertIn("OnCalendar=*:0/15", timer.render_bazras_timer(config))


if __name__ == "__main__":
    unittest.main()
