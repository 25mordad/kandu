import json
import os
import subprocess
import tempfile
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
