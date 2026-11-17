"""Codex's usage, read on request from `codex app-server` at no token cost, with a faked reply (#79).

The fake is a real `codex` program on PATH, so the read is bounded as it is on a machine: by processes and pipes.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from kandu import usage
from kandu.config import UsageLimits
from kandu.usage import UsageError, codex_login, over_limit, read_codex_usage

# The shape of a real `account/rateLimits/read` answer, read from `codex app-server` itself; the account left out.
FIVE_HOURS_RESET, WEEK_RESET = 1794922747, 1795104018
REAL_REPLY = {"id": 2, "result": {
    "rateLimits": {"limitId": "codex", "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": FIVE_HOURS_RESET},
                   "secondary": {"usedPercent": 41, "windowDurationMins": 10080, "resetsAt": WEEK_RESET}},
    "rateLimitsByLimitId": {"codex": {"limitId": "codex",
                                      "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": FIVE_HOURS_RESET},
                                      "secondary": {"usedPercent": 41, "windowDurationMins": 10080, "resetsAt": WEEK_RESET}}},
    "ordinaryUsageAllowed": True}}
SLEEP = "/usr/bin/sleep" if os.path.exists("/usr/bin/sleep") else "/bin/sleep"
# A `codex` that is a wrapper, as the npm package's node launcher is: the real program is its child, holds its
# stdout, and never answers.
HUNG_WRAPPER = f"#!/bin/sh\n{SLEEP} 30 &\necho $! > \"${{0%/*}}/child\"\nwait\n"
SERVER = """#!{python}
import os, sys
here = os.path.dirname(os.path.abspath(__file__))
open(os.path.join(here, "pid"), "w").write(str(os.getpid()))
asked = [sys.stdin.readline() for _ in range(3)]
open(os.path.join(here, "requests"), "w").write("".join(asked))
sys.stdout.write({lines!r})
sys.stdout.flush()
if {stays!r}:
    sys.stdin.read()  # as the real server, it answers and stays while its stdin is open
"""


def at(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).astimezone().isoformat(timespec="seconds")


def gone(pid, within=3.0):
    """True when the process has ended, or is only left to be reaped, within `within` seconds."""
    end = time.monotonic() + within
    while time.monotonic() < end:
        try:
            if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return True
        except (FileNotFoundError, ProcessLookupError):
            return True
        time.sleep(0.05)
    return False


class Codex:
    """A faked `codex` program, alone on PATH."""
    def __init__(self, script=None):
        self.dir = Path(tempfile.mkdtemp())
        if script is not None:
            path = self.dir / "codex"
            path.write_text(script)
            path.chmod(0o755)

    def __enter__(self):
        self.path = mock.patch.dict(os.environ, {"PATH": str(self.dir)})
        self.path.start()
        return self

    def __exit__(self, *exc):
        self.path.stop()

    def read(self, name):
        return (self.dir / name).read_text()


class Server(Codex):
    """A faked `codex app-server`: its lines in answer, and what Kandu wrote to it."""
    def __init__(self, *replies, missing=False, stays=True, before=""):
        lines = before + "".join(json.dumps(r) + "\n" for r in ({"id": 1, "result": {}},) + replies)
        super().__init__(None if missing else SERVER.format(python=sys.executable, lines=lines, stays=stays))

    def requests(self):
        return [json.loads(line) for line in self.read("requests").splitlines()]


def read(server):
    with server:
        return read_codex_usage()


class ReadCodexUsageTest(unittest.TestCase):
    def test_it_reads_the_5_hour_and_weekly_usage_with_when_each_resets(self):
        usage = read(Server(REAL_REPLY))
        self.assertEqual((usage.session, usage.week), (12, 41))
        self.assertEqual((usage.session_resets, usage.week_resets), (at(FIVE_HOURS_RESET), at(WEEK_RESET)))

    def test_it_asks_the_app_server_for_the_limits_and_costs_no_tokens(self):
        server = Server(REAL_REPLY)
        read(server)
        self.assertEqual([r["method"] for r in server.requests()], ["initialize", "initialized", "account/rateLimits/read"])
        self.assertTrue(gone(int(server.read("pid"))))  # the server does not outlive the read

    def test_each_window_is_known_by_its_length_not_its_place(self):
        reply = {"id": 2, "result": {"rateLimits": {
            "primary": {"usedPercent": 41, "windowDurationMins": 10080, "resetsAt": WEEK_RESET},
            "secondary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": FIVE_HOURS_RESET}}}}
        usage = read(Server(reply))
        self.assertEqual((usage.session, usage.week), (12, 41))

    def test_a_plan_with_only_a_weekly_window_counts_the_5_hour_one_as_0(self):
        reply = {"id": 2, "result": {"rateLimits": {
            "primary": {"usedPercent": 7, "windowDurationMins": 10080, "resetsAt": None}, "secondary": None}}}
        usage = read(Server(reply))
        self.assertEqual((usage.session, usage.week, usage.session_resets, usage.week_resets), (0, 7, None, None))

    def test_no_weekly_window_is_a_shape_it_does_not_know(self):
        reply = {"id": 2, "result": {"rateLimits": {
            "primary": {"usedPercent": 7, "windowDurationMins": 300, "resetsAt": None}, "secondary": None}}}
        with self.assertRaises(UsageError):
            read(Server(reply))

    def test_a_refused_read_such_as_a_logged_out_codex_fails_closed(self):
        refused = {"id": 2, "error": {"code": -32600, "message": "auth required for someone@example.com"}}
        with self.assertRaises(UsageError) as caught:
            read(Server(refused))
        self.assertNotIn("example.com", str(caught.exception))  # what Codex says never reaches a log or a message

    def test_codex_missing_fails_closed(self):
        with self.assertRaises(UsageError):
            read(Server(missing=True))

    def test_a_server_that_ends_without_an_answer_fails_closed(self):
        with self.assertRaises(UsageError):
            read(Server(stays=False))

    def test_lines_that_are_not_json_or_not_the_answer_are_passed_over(self):
        server = Server({"method": "account/updated", "params": {}}, REAL_REPLY, before="starting\n")
        self.assertEqual(read(server).week, 41)

    def test_codex_reasons_name_codex(self):
        usage = read(Server(REAL_REPLY))
        self.assertEqual(over_limit(usage, UsageLimits(10, 90), "Codex "), "Codex 5-hour usage at 12%, limit 10%")
        self.assertEqual(over_limit(usage, UsageLimits(90, 40), "Codex "), "Codex weekly usage at 41%, limit 40%")
        self.assertIsNone(over_limit(usage, UsageLimits(90, 90), "Codex "))


class CodexLoginTest(unittest.TestCase):
    def status(self, code=0, missing=False):
        script = None if missing else f'#!/bin/sh\n[ "$*" = "login status" ] || exit 9\necho "Logged in"\nexit {code}\n'
        with Codex(script):
            return codex_login()

    def test_logged_in_is_none(self):
        self.assertIsNone(self.status())

    def test_logged_out_says_so(self):
        self.assertEqual(self.status(code=1), "not logged in; run: codex login")

    def test_missing_says_so(self):
        self.assertEqual(self.status(missing=True), "not installed")


class AHungCodexIsBoundedTest(unittest.TestCase):
    """A `codex` that never answers is stopped at the time limit, with the program its wrapper started."""
    def setUp(self):
        limit = mock.patch.object(usage, "TIMEOUT_SECONDS", 1)
        limit.start()
        self.addCleanup(limit.stop)

    def test_a_usage_read_that_never_answers_times_out(self):
        codex = Codex(HUNG_WRAPPER)
        began = time.monotonic()
        with self.assertRaises(UsageError) as caught:
            read(codex)
        self.assertLess(time.monotonic() - began, 10)
        self.assertEqual(str(caught.exception), "codex timed out")
        self.assertTrue(gone(int(codex.read("child"))))

    def test_a_login_status_that_never_answers_times_out(self):
        codex = Codex(HUNG_WRAPPER)
        began = time.monotonic()
        with codex:
            why = codex_login()
        self.assertLess(time.monotonic() - began, 10)
        self.assertEqual(why, "codex login status did not answer")
        self.assertTrue(gone(int(codex.read("child"))))

    def test_a_server_that_ends_before_it_is_asked_fails_closed(self):
        popen = usage.subprocess.Popen

        def ended(*args, **kwargs):
            server = popen(*args, **kwargs)
            server.wait()  # its end of the pipe is closed before Kandu writes
            return server

        with Codex("#!/bin/sh\nexit 0\n"), mock.patch("kandu.usage.subprocess.Popen", ended):
            with self.assertRaises(UsageError) as caught:
                read_codex_usage()
        self.assertEqual(str(caught.exception), "codex could not be asked: BrokenPipeError")


if __name__ == "__main__":
    unittest.main()
