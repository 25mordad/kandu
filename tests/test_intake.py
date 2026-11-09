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

    MAP = "✅ The login works by email\n❓ Should a new user get a welcome mail?\n\n⏳ Which mail service\n✂️ Password reset\n"

    def test_a_map_is_saved_for_an_open_request(self):
        request_id = intake.add(self.store, "Fix the login")
        intake.save_map(self.store, request_id, self.MAP)
        self.assertEqual(self.store.requests()[0]["map"], {
            "decided": ["The login works by email"], "asked": ["Should a new user get a welcome mail?"],
            "later": ["Which mail service"], "out": ["Password reset"]})

    def test_a_new_map_replaces_the_earlier_one(self):
        request_id = intake.add(self.store, "Fix the login")
        intake.save_map(self.store, request_id, self.MAP)
        intake.save_map(self.store, request_id, "✅ Welcome mail: yes")
        self.assertEqual(self.store.requests()[0]["map"],
                         {"decided": ["Welcome mail: yes"], "asked": [], "later": [], "out": []})

    def test_saving_the_request_again_keeps_its_map(self):
        request_id = intake.add(self.store, "Fix the login")
        intake.save_map(self.store, request_id, self.MAP)
        intake.add(self.store, "Fix the login")
        self.assertIn("map", self.store.requests()[0])

    def test_a_mark_is_read_with_or_without_its_emoji_form(self):
        request_id = intake.add(self.store, "Fix the login")
        intake.save_map(self.store, request_id, "\u2702 Password reset\n\u2705\ufe0f By email")
        self.assertEqual(self.store.requests()[0]["map"],
                         {"decided": ["By email"], "asked": [], "later": [], "out": ["Password reset"]})

    def test_a_map_can_name_its_project_and_a_later_map_keeps_it(self):
        request_id = intake.add(self.store, "Fix the login")
        intake.save_map(self.store, request_id, self.MAP, project="website")
        intake.save_map(self.store, request_id, "✅ Welcome mail: yes")
        self.assertEqual(self.store.requests()[0]["project"], "website")

    def test_the_command_saves_the_maps_project(self):
        request_id = intake.add(self.store, "Fix the login")
        self.assertEqual(self.run_main(["map", request_id, "--project", "website"], "✅ By email\n"), (0, ""))
        self.assertEqual(self.store.requests()[0]["project"], "website")

    def test_a_map_needs_an_open_request(self):
        with self.assertRaisesRegex(intake.IntakeError, "no open request abc123"):
            intake.save_map(self.store, "abc123", self.MAP)

    def test_a_map_not_in_the_expected_shape_is_refused_with_the_reason(self):
        request_id = intake.add(self.store, "Fix the login")
        for text, reason in (("", "empty"), ("  \n", "empty"), ("✅ ok\nno mark here", "line 2"),
                             ("❓", "line 1"), ("- ✅ ok", "line 1")):
            with self.subTest(text=text), self.assertRaisesRegex(intake.IntakeError, reason):
                intake.save_map(self.store, request_id, text)
        self.assertNotIn("map", self.store.requests()[0])

    def test_closing_a_request_drops_its_map(self):
        request_id = intake.add(self.store, "Fix the login")
        intake.save_map(self.store, request_id, self.MAP)
        intake.close(self.store, request_id)
        intake.add(self.store, "Fix the login")
        self.assertNotIn("map", self.store.requests()[0])

    def test_the_list_shows_each_requests_map_on_its_line(self):
        first = intake.add(self.store, "Fix the login")
        second = intake.add(self.store, "Add dark mode")
        intake.save_map(self.store, first, "✅ By email\n❓ Welcome mail?")
        self.assertEqual(intake.listing(self.store), [
            f"{first}: Fix the login [map: ✅ By email | ❓ Welcome mail?]",
            f"{second}: Add dark mode"])

    def test_the_command_saves_a_map_from_stdin(self):
        request_id = intake.add(self.store, "Fix the login")
        out = self.run_main(["map", request_id], "✅ By email\n")
        self.assertEqual(out, (0, ""))
        self.assertEqual(self.store.requests()[0]["map"]["decided"], ["By email"])

    def test_the_command_refuses_a_bad_map_with_the_reason(self):
        request_id = intake.add(self.store, "Fix the login")
        code, err = self.run_main(["map", request_id], "nothing marked\n")
        self.assertEqual(code, 1)
        self.assertIn("line 1", err)

    def run_main(self, argv, stdin):
        import io
        from unittest import mock
        err = io.StringIO()
        with mock.patch("sys.argv", ["intake", *argv]), mock.patch("sys.stdin", io.StringIO(stdin)), \
                mock.patch("sys.stderr", err), mock.patch.object(intake, "StateStore", lambda: self.store):
            code = intake.main()
        return code, err.getvalue()

    def test_didban_opens_with_what_is_waiting(self):
        config = DidbanConfig(brain=DidbanBrain(reach="ssh server", install_dir="~/kandu"))
        self.assertIn("in the user's words: abc123: Fix it", didban.system_prompt(config, "ok", requests="abc123: Fix it"))


if __name__ == "__main__":
    unittest.main()
