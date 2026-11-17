import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from kandu import ghmeter
from kandu.state import StateStore


class MeterScriptTest(unittest.TestCase):
    """The meter counts a call, keeps no argument past the second word, and passes the real result on."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        real = self.tmp / "bin"
        real.mkdir()
        (real / "gh").write_text('#!/bin/sh\necho "real $*"\nexit 3\n')
        (real / "gh").chmod(0o755)
        self.log = self.tmp / "log"
        self.env = {"PATH": f"{ghmeter.METER_DIR}:{real}:/usr/bin:/bin", "GH_METER_LOG": str(self.log),
                    ghmeter.CALLER_ENV: "supervisor", ghmeter.RUN_ENV: "r-1"}

    def call(self, *args):
        return subprocess.run([str(ghmeter.METER), *args], capture_output=True, text=True, env=self.env)

    def lines(self):
        [day] = self.log.iterdir()
        return [json.loads(line) for line in day.read_text().splitlines()]

    def test_counts_and_passes_on(self):
        result = self.call("pr", "view", "12", "--body", "secret text")
        self.assertEqual((result.stdout, result.returncode), ("real pr view 12 --body secret text\n", 3))
        [line] = self.lines()
        self.assertEqual({k: line[k] for k in ("type", "caller", "run", "command", "kind", "exit")},
                         {"type": "call", "caller": "supervisor", "run": "r-1", "command": "pr view",
                          "kind": "graphql", "exit": 3})
        self.assertNotIn("secret", json.dumps(line))

    def test_api_is_split_into_graphql_and_rest(self):
        self.call("api", "graphql", "-f", "query={ viewer { login } }")
        self.call("api", "repos/a/b/pulls")
        self.assertEqual([(l["command"], l["kind"]) for l in self.lines()],
                         [("api graphql", "graphql"), ("api rest", "rest")])

    def test_a_symlink_to_the_meter_is_not_the_real_gh(self):
        loop = self.tmp / "loop"
        loop.mkdir()
        (loop / "gh").symlink_to(ghmeter.METER)
        self.env["PATH"] = f"{loop}:{self.env['PATH']}"
        self.assertEqual(self.call("issue", "list").returncode, 3)


class MeterBackoffTest(unittest.TestCase):
    """Every `gh` through the meter, an agent's too, honors GitHub's back-off and starts one when refused."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        real = self.tmp / "bin"
        real.mkdir()
        self.ran = self.tmp / "ran"
        # The fake gh notes when it ran; `pr view` is refused with GitHub's secondary-limit answer.
        (real / "gh").write_text(
            '#!/bin/sh\n'
            '[ "$1 $2" = "api rate_limit" ] && echo "not this time" >&2 && exit 1\n'
            f'date +%s.%N >> "{self.ran}"\n'
            '[ "$1 $2" = "pr view" ] && echo "gh: You have exceeded a secondary rate limit. Please wait a few minutes." >&2'
            ' && exit 1\n'
            'echo ok\n')
        (real / "gh").chmod(0o755)
        self.log = self.tmp / "log"
        self.log.mkdir()
        self.backoff = self.log / "backoff.json"
        self.env = {"PATH": f"{ghmeter.METER_DIR}:{real}:/usr/bin:/bin", "GH_METER_LOG": str(self.log),
                    ghmeter.CALLER_ENV: "worker"}

    def call(self, *args):
        return subprocess.run([str(ghmeter.METER), *args], capture_output=True, text=True, env=self.env)

    def ran_at(self):
        return [float(line) for line in self.ran.read_text().split()]

    def test_a_call_during_a_backoff_waits_until_it_ends(self):
        until = int(time.time()) + 2
        self.backoff.write_text(json.dumps({"until": until, "refusals": 1, "why": "secondary rate limit"}))
        result = self.call("issue", "list")
        self.assertEqual((result.stdout, result.returncode), ("ok\n", 0))
        [ran] = self.ran_at()
        self.assertGreaterEqual(ran, until)
        self.assertFalse(self.backoff.exists())  # the call went through, so the next refusal starts from a minute

    def test_a_rate_limit_answer_writes_the_backoff(self):
        before = time.time()
        result = self.call("pr", "view", "12")
        self.assertEqual(result.returncode, 1)
        self.assertIn("secondary rate limit", result.stderr)
        data = json.loads(self.backoff.read_text())
        self.assertEqual(data["refusals"], 1)
        self.assertIn("secondary rate limit", data["why"])
        self.assertGreaterEqual(data["until"], int(before) + 60)

    def test_an_other_failure_writes_no_backoff(self):
        bin2 = self.tmp / "bin2"
        bin2.mkdir()
        (bin2 / "gh").write_text('#!/bin/sh\necho "could not resolve to a PullRequest" >&2\nexit 1\n')
        (bin2 / "gh").chmod(0o755)
        self.env["PATH"] = f"{ghmeter.METER_DIR}:{bin2}:/usr/bin:/bin"
        self.assertEqual(self.call("pr", "view", "12").returncode, 1)
        self.assertFalse(self.backoff.exists())

    def test_without_a_backoff_there_is_no_delay(self):
        start = time.time()
        self.assertEqual(self.call("issue", "list").returncode, 0)
        self.assertLess(time.time() - start, 1)

    def test_a_past_backoff_does_not_delay(self):
        self.backoff.write_text(json.dumps({"until": int(time.time()) - 5, "refusals": 1, "why": "x"}))
        start = time.time()
        self.assertEqual(self.call("issue", "list").returncode, 0)
        self.assertLess(time.time() - start, 1)

    def test_kandus_own_calls_are_left_to_kandu(self):
        until = int(time.time()) + 30
        self.backoff.write_text(json.dumps({"until": until, "refusals": 1, "why": "x"}))
        self.env[ghmeter.GATE_ENV] = "kandu"
        start = time.time()
        self.assertEqual(self.call("issue", "list").returncode, 0)
        self.assertLess(time.time() - start, 1)
        self.assertEqual(json.loads(self.backoff.read_text())["until"], until)

    def test_the_rate_limit_lookup_never_waits(self):
        self.backoff.write_text(json.dumps({"until": int(time.time()) + 30, "refusals": 1, "why": "x"}))
        start = time.time()
        self.call("api", "rate_limit")
        self.assertLess(time.time() - start, 1)


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.log = self.tmp / "gh"
        self.log.mkdir()
        self.store = StateStore(self.tmp / "state")
        self.store.record("run_finished", task="a/b#1", project="b", run_id="r-1", role="worker", models={},
                          duration_ms=1, is_error=False, now=datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc))
        day = self.store.days()[-1]
        self.day = day
        lines = [
            {"type": "limit", "ts": f"{day}T10:00:00+01:00", "graphql_used": 100, "graphql_reset": 1, "rest_used": 0, "rest_reset": 1},
            {"type": "call", "ts": f"{day}T10:01:00+01:00", "caller": "supervisor", "run": "r-1", "command": "pr view", "kind": "graphql", "exit": 0},
            {"type": "call", "ts": f"{day}T10:02:00+01:00", "caller": "dashboard", "run": "", "command": "api graphql", "kind": "graphql", "exit": 0},
            {"type": "call", "ts": f"{day}T10:03:00+01:00", "caller": "supervisor", "run": "", "command": "api rest", "kind": "rest", "exit": 1},
            {"type": "limit", "ts": f"{day}T10:05:00+01:00", "graphql_used": 150, "graphql_reset": 1, "rest_used": 1, "rest_reset": 1},
            {"type": "limit", "ts": f"{day}T11:05:00+01:00", "graphql_used": 7, "graphql_reset": 2, "rest_used": 1, "rest_reset": 2},
        ]
        (self.log / f"{day}.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n{cut")

    def test_report(self):
        text = ghmeter.report(self.day, self.log, self.store)
        self.assertIn("2 GraphQL calls and 1 REST calls counted from Kandu, 1 failed", text)
        self.assertIn("10:00                   50              2              48", text)
        self.assertIn("11:00                    7              0               7", text)
        self.assertIn("1  worker a/b#1", text)
        self.assertIn("1  dashboard", text)

    def test_each_call_counts_the_points_it_really_cost(self):
        lines = [
            {"type": "limit", "ts": f"{self.day}T12:00:00+01:00", "graphql_used": 100, "graphql_reset": 3, "rest_used": 0, "rest_reset": 3},
            {"type": "call", "ts": f"{self.day}T12:01:00+01:00", "caller": "sync", "run": "", "call": "c1", "command": "api graphql", "kind": "graphql", "exit": 0},
            {"type": "cost", "ts": f"{self.day}T12:01:00+01:00", "call": "c1", "points": 30},
            {"type": "call", "ts": f"{self.day}T12:02:00+01:00", "caller": "doctor", "run": "", "call": "c2", "command": "api graphql", "kind": "graphql", "exit": 0},
            {"type": "cost", "ts": f"{self.day}T12:02:00+01:00", "call": "c2", "points": 19},
            {"type": "call", "ts": f"{self.day}T12:03:00+01:00", "caller": "worker", "run": "", "command": "pr view", "kind": "graphql", "exit": 0},
            {"type": "call", "ts": f"{self.day}T12:04:00+01:00", "caller": "sync", "run": "", "call": "c3", "command": "api rest", "kind": "rest", "exit": 0},
            {"type": "limit", "ts": f"{self.day}T12:05:00+01:00", "graphql_used": 150, "graphql_reset": 3, "rest_used": 1, "rest_reset": 3},
        ]
        (self.log / f"{self.day}.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n")
        text = ghmeter.report(self.day, self.log, self.store)
        # 50 points spent: 30 + 19 by Kandu's costed calls, 1 by a call that said nothing of its cost.
        self.assertIn("12:00                   50             50               0", text)
        self.assertIn("    30  sync", text)
        self.assertIn("    19  doctor", text)
        self.assertIn("     1  worker", text)

    def test_no_log(self):
        self.assertIn("0 GraphQL calls", ghmeter.report("2001-01-01", self.log, self.store))


class SampleTest(unittest.TestCase):
    def test_writes_the_limit(self):
        log = Path(tempfile.mkdtemp())
        body = {"resources": {"graphql": {"used": 9, "reset": 5}, "core": {"used": 2, "reset": 6}}}
        run = lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(body), "")
        line = ghmeter.sample(log, run, datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc))
        self.assertEqual((line["graphql_used"], line["rest_used"]), (9, 2))
        self.assertEqual(ghmeter.read("2026-11-03", log), [line])

    def test_a_failed_read_writes_nothing(self):
        log = Path(tempfile.mkdtemp()) / "gh"
        run = lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "no network")
        self.assertIsNone(ghmeter.sample(log, run))
        self.assertFalse(log.exists())


if __name__ == "__main__":
    unittest.main()
