import tempfile
import unittest
from pathlib import Path

from kandu import didban, intake
from kandu.config import DidbanBrain, DidbanConfig
from kandu.state import StateStore


class IntakeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = StateStore(Path(tmp.name) / "state")

    def test_a_request_stays_until_it_is_closed(self):
        request_id = intake.add(self.store, "  Fix the  login\nfor new users ")
        self.assertEqual(self.store.requests(), [{"id": request_id, "text": "Fix the login for new users"}])
        intake.close(self.store, request_id)
        self.assertEqual(self.store.requests(), [])

    def test_saving_twice_keeps_one_request(self):
        self.assertEqual(intake.add(self.store, "Same words"), intake.add(self.store, "Same words"))
        self.assertEqual(len(self.store.requests()), 1)

    def test_a_request_is_kept_in_the_users_language(self):
        intake.add(self.store, "صدا را درست کن")
        self.assertEqual(self.store.requests()[0]["text"], "صدا را درست کن")

    def test_a_request_needs_text(self):
        with self.assertRaises(intake.IntakeError):
            intake.add(self.store, " \n ")

    def test_closing_an_unknown_request_says_so(self):
        with self.assertRaises(intake.IntakeError):
            intake.close(self.store, "abc123")

    def test_a_request_is_not_in_the_event_log(self):
        intake.add(self.store, "Private words")
        self.assertEqual(self.store.days(), [])

    def test_only_its_user_can_read_a_request(self):
        request_id = intake.add(self.store, "Private words")
        self.assertEqual((self.store.requests_dir / f"{request_id}.json").stat().st_mode & 0o777, 0o600)

    def test_didban_opens_with_what_is_waiting(self):
        config = DidbanConfig(brain=DidbanBrain(reach="ssh server", install_dir="~/kandu"))
        self.assertIn("in the user's words: abc123: Fix it", didban.system_prompt(config, "ok", requests="abc123: Fix it"))


if __name__ == "__main__":
    unittest.main()
