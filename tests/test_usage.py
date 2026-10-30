import json
import unittest
from types import SimpleNamespace
from unittest import mock

from kandu.config import ClaudeLimits
from kandu.usage import Usage, UsageError, over_limit, parse_usage, read_usage

# Shape of the real `/usage` text, read from `claude` itself.
REAL_TEXT = """Current session: 28% used · resets Oct 30, 6:20pm
Current week (all models): 93% used · resets Oct 30, 8pm
Current week (Fable): 0% used · resets Oct 30, 8pm
  53%  Trim a file · 6k tokens per request"""
LIMITS = ClaudeLimits(session_5h_percent=70, weekly_percent=60)


def claude_returns(stdout, returncode=0):
    return mock.patch("kandu.usage.subprocess.run",
                      return_value=SimpleNamespace(stdout=stdout, returncode=returncode))


class UsageTest(unittest.TestCase):
    def test_real_text(self):
        self.assertEqual(parse_usage(REAL_TEXT), Usage(session=28, week=93))

    def test_unknown_text_fails_closed(self):
        with self.assertRaises(UsageError):
            parse_usage("Current session: 28% used")

    def test_read_from_the_json_result(self):
        with claude_returns(json.dumps({"result": REAL_TEXT})):
            self.assertEqual(read_usage(), Usage(session=28, week=93))

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
