import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kandu import didban, sessions
from kandu.config import DidbanBrain
from kandu.state import StateStore

SESSION = "11111111-2222-3333-4444-555555555555"
BRAIN = DidbanBrain(reach="ssh server", install_dir="~/kandu")


def said(message_id, command=None, **usage):
    content = [{"type": "tool_use", "name": "Bash", "input": {"command": command}}] if command else [{"type": "text"}]
    return {"type": "assistant", "timestamp": "2026-10-31T10:05:00Z",
            "message": {"id": message_id, "usage": usage, "content": content}}


class Files(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        folder = self.root / "-some-repo"
        folder.mkdir()
        self.path = folder / f"{SESSION}.jsonl"

    def write(self, *entries, tail=""):
        self.path.write_text("\n".join(json.dumps(e) for e in entries) + "\n" + tail, encoding="utf-8")


class SummaryTest(Files):
    def summary(self):
        return sessions.summarize(SESSION, self.path)

    def test_one_message_on_several_lines_counts_once(self):
        self.write(said("m1", input_tokens=2, output_tokens=10, cache_read_input_tokens=100),
                   said("m1", input_tokens=2, output_tokens=10, cache_read_input_tokens=100),
                   said("m2", input_tokens=1, output_tokens=5, cache_creation_input_tokens=7))
        self.assertEqual(self.summary()["tokens"], {"input": 3, "output": 15, "cache_read": 100, "cache_creation": 7})

    def test_it_says_when_and_what(self):
        self.write({"type": "user", "timestamp": "2026-10-31T10:00:00Z"},
                   {"type": "ai-title", "aiTitle": "Fix the login"},
                   said("m1", ".venv/bin/python -m kandu.intake add <<'END'"),
                   said("m2", ".venv/bin/python -m kandu.tasks --project a"),
                   said("m3", ".venv/bin/python -m kandu.debt promote o/r#1"),
                   said("m4", "ls"))
        summary = self.summary()
        self.assertEqual((summary["started"], summary["ended"], summary["title"]),
                         ("2026-10-31T10:00:00Z", "2026-10-31T10:05:00Z", "Fix the login"))
        self.assertEqual(summary["actions"], {"requests": 1, "tasks": 2, "answers": 0})

    def test_a_line_cut_off_is_skipped(self):
        self.write(said("m1", output_tokens=4), tail='{"type": "assist')
        self.assertEqual(self.summary()["tokens"]["output"], 4)

    def test_the_transcript_is_found_in_any_folder(self):
        self.write({"type": "user"})
        self.assertEqual(sessions.transcript(SESSION, self.root), self.path)
        self.assertIsNone(sessions.transcript("other", self.root))


class SaveTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = StateStore(Path(tmp.name) / "state")

    def test_a_later_report_replaces_the_earlier_one(self):
        sessions.save(self.store, {"id": SESSION, "title": "first"})
        sessions.save(self.store, {"id": SESSION, "title": "second"})
        self.assertEqual([s["title"] for s in sessions.recent(self.store)], ["second"])

    def test_an_id_cannot_name_another_path(self):
        for summary in ({"id": "../../local/kandu"}, {"id": None}, []):
            with self.assertRaises(sessions.SessionError):
                sessions.save(self.store, summary)

    def test_only_the_last_are_kept_on_show(self):
        for number in range(sessions.KEPT + 2):
            sessions.save(self.store, {"id": f"{number:08d}-2222-3333-4444-555555555555", "started": f"{number:04d}"})
        found = sessions.recent(self.store)
        self.assertEqual((len(found), found[0]["started"]), (sessions.KEPT, f"{sessions.KEPT + 1:04d}"))


class ReportTest(Files):
    def report(self, **brain):
        self.list = self.root / "unreported"
        with mock.patch("kandu.didban.UNREPORTED", self.list), \
             mock.patch("kandu.didban.sessions.transcript", return_value=self.path), \
             mock.patch("kandu.didban.on_brain", **brain) as send:
            didban.remember(SESSION)
            didban.report_sessions(BRAIN)
            return send, didban.unreported()

    def test_a_report_sent_leaves_the_list(self):
        self.write(said("m1", output_tokens=4))
        send, left = self.report(return_value=subprocess.CompletedProcess([], 0))
        self.assertEqual(left, [])
        self.assertEqual(send.call_args.args[1], "kandu.sessions save")
        self.assertEqual(json.loads(send.call_args.args[2])["tokens"]["output"], 4)

    def test_a_report_not_sent_waits_for_the_next_time(self):
        self.write(said("m1", output_tokens=4))
        for brain in ({"return_value": subprocess.CompletedProcess([], 1)},
                      {"side_effect": subprocess.TimeoutExpired("ssh", 20)}):
            self.assertEqual(self.report(**brain)[1], [SESSION])

    def test_a_session_that_said_nothing_leaves_the_list(self):
        with mock.patch("kandu.didban.UNREPORTED", self.root / "unreported"), \
             mock.patch("kandu.didban.sessions.transcript", return_value=None):
            didban.remember(SESSION)
            didban.report_sessions(BRAIN)
            self.assertEqual(didban.unreported(), [])


if __name__ == "__main__":
    unittest.main()
