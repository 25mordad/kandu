import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from kandu.config import ClaudeLimits
from kandu.usage import Usage, UsageError, over_limit, parse_usage, read_usage

# Shape of the real `/usage` text, read from `claude` itself.
REAL_TEXT = """Current session: 28% used · resets Oct 30, 6:20pm
Current week (all models): 93% used · resets Oct 30, 8pm
Current week (Fable): 0% used · resets Oct 30, 8pm
  53%  Trim a file · 6k tokens per request"""
CET = timezone(timedelta(hours=1))
NOW = datetime(2026, 10, 29, 12, tzinfo=CET)  # `/usage` names its times on this machine's clock.
LIMITS = ClaudeLimits(session_5h_percent=70, weekly_percent=60)


def claude_returns(stdout, returncode=0):
    return mock.patch("kandu.usage.subprocess.run",
                      return_value=SimpleNamespace(stdout=stdout, returncode=returncode))


class UsageTest(unittest.TestCase):
    def test_real_text(self):
        usage = parse_usage(REAL_TEXT, now=NOW)
        self.assertEqual((usage.session, usage.week), (28, 93))

    def test_when_each_limit_resets_is_read_from_the_text(self):
        usage = parse_usage(REAL_TEXT, now=NOW)
        self.assertEqual(usage.session_resets, "2026-10-30T18:20:00+01:00")
        self.assertEqual(usage.week_resets, "2026-10-30T20:00:00+01:00")

    def test_a_reset_early_next_year_is_next_year(self):
        text = "Current week (all models): 4% used · resets Jan 2, 9am"
        late = datetime(2026, 12, 30, 12, tzinfo=CET)
        self.assertEqual(parse_usage(text, now=late).week_resets, "2027-01-02T09:00:00+01:00")

    def test_a_reset_with_no_day_is_the_next_time_it_comes(self):
        text = "Current session: 5% used · resets 6:20pm\nCurrent week (all models): 4% used · resets 10am"
        usage = parse_usage(text, now=NOW)
        self.assertEqual(usage.session_resets, "2026-10-29T18:20:00+01:00")
        self.assertEqual(usage.week_resets, "2026-10-30T10:00:00+01:00")

    def test_a_reset_names_its_time_zone(self):
        text = "Current week (all models): 4% used · resets Oct 30, 8pm (America/New_York)"
        self.assertEqual(parse_usage(text, now=NOW).week_resets, "2026-10-30T20:00:00-04:00")

    def test_a_reset_it_cannot_read_is_unknown_but_the_usage_is_still_read(self):
        text = "Current session: 5% used\nCurrent week (all models): 4% used · resets soon"
        usage = parse_usage(text, now=NOW)
        self.assertEqual((usage.session, usage.week, usage.session_resets, usage.week_resets), (5, 4, None, None))

    def test_a_plan_with_no_5_hour_window(self):
        text = "You are currently using your subscription\n\nCurrent week (all models): 4% used · resets Nov 6, 8pm"
        usage = parse_usage(text, now=NOW)
        self.assertEqual((usage.session, usage.week, usage.session_resets), (0, 4, None))
        self.assertEqual(usage.week_resets, "2026-11-06T20:00:00+01:00")

    def test_unknown_text_fails_closed(self):
        with self.assertRaises(UsageError):
            parse_usage("Current session: 28% used")

    def test_read_from_the_json_result(self):
        with claude_returns(json.dumps({"result": REAL_TEXT})):
            usage = read_usage()
        self.assertEqual((usage.session, usage.week), (28, 93))

    def test_every_way_of_not_reading_it_fails_closed(self):
        for stdout, code in (("", 0), ("[]", 0), ('{"result": null}', 0), (json.dumps({"result": REAL_TEXT}), 1)):
            with claude_returns(stdout, code), self.assertRaises(UsageError):
                read_usage()

    def test_missing_claude_fails_closed(self):
        with mock.patch("kandu.usage.subprocess.run", side_effect=FileNotFoundError), self.assertRaises(UsageError):
            read_usage()

    def test_limits_are_reached_at_the_exact_percent(self):
        self.assertIsNone(over_limit(Usage(69, 59), LIMITS))
        self.assertIn("5-hour", over_limit(Usage(70, 0), LIMITS))
        self.assertIn("weekly", over_limit(Usage(0, 60), LIMITS))


if __name__ == "__main__":
    unittest.main()
