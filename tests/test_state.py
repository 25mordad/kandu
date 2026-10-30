import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kandu.state import EVENT_FIELDS, StateError, StateStore, usage_from_result

DAY = "2026-10-30"
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone(timedelta(hours=1)))


def tokens(input=0, output=0, cache_read=0, cache_creation=0):
    return {"input": input, "output": output, "cache_read": cache_read, "cache_creation": cache_creation}


class StateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = StateStore(Path(tmp.name))

    def run_finished(self, task="o/r#1", role="worker", models=None, project="site"):
        self.store.record(
            "run_finished", now=NOW, task=task, project=project, run_id="r1", role=role,
            models=models or {"model-a": tokens(input=10, output=5)}, duration_ms=100, is_error=False,
        )

    def test_record_and_read(self):
        self.store.record("task_picked", now=NOW, task="o/r#1", project="site")
        event = self.store.events(DAY)[0]
        self.assertEqual(event["type"], "task_picked")
        self.assertEqual(event["v"], 1)
        self.assertEqual(event["ts"], "2026-10-30T12:00:00+01:00")

    def test_unknown_event_type_is_rejected(self):
        with self.assertRaises(StateError):
            self.store.record("anything", now=NOW)

    def test_extra_field_is_rejected(self):
        with self.assertRaises(StateError):
            self.store.record("task_picked", now=NOW, task="o/r#1", project="site", prompt="private text")

    def test_missing_field_is_rejected(self):
        with self.assertRaises(StateError):
            self.store.record("phase_changed", now=NOW, task="o/r#1", project="site")

    def test_nothing_is_written_when_rejected(self):
        with self.assertRaises(StateError):
            self.store.record("task_picked", now=NOW)
        self.assertEqual(self.store.days(), [])

    def test_no_event_type_carries_free_output(self):
        for fields in EVENT_FIELDS.values():
            self.assertFalse({"prompt", "output", "text", "command", "secret"} & set(fields))

    def test_review_round_is_logged_even_with_no_findings(self):
        self.store.record("review_finished", now=NOW, task="o/r#1", project="site", run_id="r2", round=1,
                          head_sha="abc123", findings=0, blockers=0, door="two-way")
        event = self.store.events(DAY)[0]
        self.assertEqual((event["head_sha"], event["findings"]), ("abc123", 0))

    def test_days_are_sorted(self):
        self.store.record("supervisor_woke", now=NOW)
        self.store.record("supervisor_woke", now=NOW - timedelta(days=1))
        self.assertEqual(self.store.days(), ["2026-10-29", DAY])

    def test_day_without_events(self):
        self.assertEqual(self.store.events(DAY), [])
        self.assertEqual(self.store.tokens_for_day(DAY)["total"]["total"], 0)

    def test_cut_off_line_is_skipped(self):
        self.store.record("supervisor_woke", now=NOW)
        with open(self.store.events_dir / f"{DAY}.jsonl", "a") as f:
            f.write('{"v": 1, "type": "sup')
        self.assertEqual(len(self.store.events(DAY)), 1)

    def test_line_cut_inside_a_character_is_skipped(self):
        self.store.record("supervisor_woke", now=NOW)
        with open(self.store.events_dir / f"{DAY}.jsonl", "ab") as f:
            f.write('{"reason": "ک'.encode("utf-8")[:-1])
        self.assertEqual(len(self.store.events(DAY)), 1)

    def test_event_after_a_cut_off_line_is_kept(self):
        self.store.record("supervisor_woke", now=NOW)
        with open(self.store.events_dir / f"{DAY}.jsonl", "a") as f:
            f.write('{"v": 1, "type": "sup')
        self.run_finished()
        self.assertEqual([e["type"] for e in self.store.events(DAY)], ["supervisor_woke", "run_finished"])
        self.assertEqual(self.store.tokens_for_day(DAY)["total"]["total"], 15)

    def test_text_with_a_unicode_line_separator_survives(self):
        reason = "first second"
        self.store.record("supervisor_slept", now=NOW, reason=reason, next_wake="later")
        self.assertEqual(self.store.events(DAY)[0]["reason"], reason)

    def test_log_files_and_folders_are_private(self):
        self.store.record("supervisor_woke", now=NOW)
        self.assertEqual((self.store.events_dir / f"{DAY}.jsonl").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.events_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.root.stat().st_mode & 0o777, 0o700)

    def test_supervisor_run_without_a_task(self):
        self.run_finished(task=None, role="supervisor", project=None)
        self.run_finished()
        report = self.store.tokens_for_day(DAY)
        self.assertEqual(report["no_task"]["total"], 15)
        self.assertEqual(list(report["tasks"]), ["o/r#1"])
        self.assertEqual(report["total"]["total"], 30)

    def test_only_a_supervisor_run_may_have_no_task(self):
        with self.assertRaises(StateError):
            self.run_finished(task=None, role="worker", project=None)
        with self.assertRaises(StateError):
            self.store.record("task_picked", now=NOW, task=None, project=None)

    def test_tokens_are_split_by_task_model_and_role(self):
        self.run_finished(task="o/r#1", role="worker", models={"model-a": tokens(10, 5, 100, 20)})
        self.run_finished(task="o/r#1", role="reviewer", models={"model-b": tokens(1, 2)})
        self.run_finished(task="o/r#2", role="worker", models={"model-a": tokens(3, 4)})
        report = self.store.tokens_for_day(DAY)
        self.assertEqual(report["total"]["total"], 145)
        self.assertEqual(report["total"]["cache_read"], 100)
        self.assertEqual(report["tasks"]["o/r#1"]["total"], 138)
        self.assertEqual(report["tasks"]["o/r#2"]["total"], 7)
        self.assertEqual(report["models"]["model-b"]["total"], 3)
        self.assertEqual(report["roles"]["worker"]["total"], 142)

    def test_one_run_with_two_models(self):
        self.run_finished(models={"model-a": tokens(1, 1), "model-b": tokens(2, 2)})
        report = self.store.tokens_for_day(DAY)
        self.assertEqual(report["tasks"]["o/r#1"]["total"], 6)
        self.assertEqual(sorted(report["models"]), ["model-a", "model-b"])

    def test_current_snapshot(self):
        self.assertEqual(self.store.read_current(), {})
        self.store.write_current({"supervisor": "asleep"})
        self.assertEqual(self.store.read_current(), {"v": 1, "supervisor": "asleep"})
        self.assertEqual(list(self.store.root.glob("*.tmp")), [])
        self.assertEqual(self.store.current_file.stat().st_mode & 0o777, 0o600)

    def test_snapshot_file_is_private_from_the_start(self):
        created = []
        real_open = os.open

        def spy(path, flags, mode=0o777, **kwargs):
            created.append(mode)
            return real_open(path, flags, mode, **kwargs)

        with mock.patch("kandu.state.os.open", spy):
            self.store.write_current({"supervisor": "asleep"})
        self.assertEqual(created, [0o600])


class UsageFromResultTest(unittest.TestCase):
    def test_real_result_shape(self):
        result = json.loads("""{
            "type": "result", "duration_ms": 994, "is_error": false,
            "modelUsage": {"model-a": {"inputTokens": 2, "outputTokens": 4,
                "cacheReadInputTokens": 649, "cacheCreationInputTokens": 4784,
                "webSearchRequests": 0, "costUSD": 0.001}}
        }""")
        self.assertEqual(usage_from_result(result), {
            "models": {"model-a": {"input": 2, "output": 4, "cache_read": 649, "cache_creation": 4784}},
            "duration_ms": 994,
            "is_error": False,
        })

    def test_missing_usage(self):
        self.assertEqual(usage_from_result({})["models"], {})


if __name__ == "__main__":
    unittest.main()
