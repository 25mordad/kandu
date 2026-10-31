import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kandu import doctor, notify, queen, supervisor
from kandu.config import NOTIFY_EVENTS, ClaudeLimits
from kandu.notify import MESSAGES, Message, Notifier, NotifyError, Telegram, message_for, notifier_for
from kandu.state import StateStore
from kandu.usage import Usage, over_limit

TASK = "o/r#7"
ON_TASK = {"task": TASK, "project": "site"}


class Adapter:
    def __init__(self):
        self.sent = []
        self.fails = None

    def send(self, text):
        if self.fails is not None:
            raise self.fails
        self.sent.append(text)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = StateStore(Path(tmp.name) / "state")
        self.adapter = Adapter()
        self.use(list(NOTIFY_EVENTS), "fa")

    def use(self, events, language):
        self.store.listeners[:] = [Notifier(self.store, self.adapter, events, language).on_event]

    def slept(self, reason):
        self.store.record("supervisor_slept", reason=reason, next_wake=None)

    def finished(self, outcome):
        self.store.record("task_finished", task=TASK, project="site", outcome=outcome)

    def logged(self, kind):
        return [e for e in self.store.events(self.store.today()) if e["type"] == kind]


class WhatIsImportantTest(unittest.TestCase):
    def kind_of(self, event_type, **fields):
        message = message_for({"type": event_type, "ts": "2026-10-31T10:00:00+01:00"} | fields)
        return message and message.kind

    def test_a_task_that_ended(self):
        for outcome, kind in (("failed", "failure"), ("deploy_failed", "failure"), ("live_check_failed", "failure"),
                              ("sizing_failed", "failure"), ("blocked", "needs_decision"),
                              ("needs_info", "needs_decision"), ("pr_closed", "needs_decision"),
                              ("merged", None), ("stopped_by_user", None)):
            self.assertEqual(self.kind_of("task_finished", outcome=outcome, **ON_TASK), kind, outcome)

    def test_every_way_a_cycle_ends(self):
        limits = ClaudeLimits(session_5h_percent=70, weekly_percent=60)
        for reason, kind in (("nothing ready", None), ("tasks ran out", None), ("waiting for a machine", None),
                             ("cycle time is up", None), ("stopped while an agent was running", None),
                             ("stopped by the user", "supervisor_stopped"),
                             (over_limit(Usage(90, 10), limits), "usage_limit"),
                             (over_limit(Usage(10, 90), limits), "usage_limit"),
                             ("the queen reached her daily token limit", "usage_limit"),
                             ("usage unknown, not starting: could not run claude", "claude_login"),
                             ("could not read the boards: gh failed", "failure"),
                             ("tasks ran out; the queen could not look: GitHubError", "failure"),
                             ("something new", "supervisor_stopped")):
            self.assertEqual(self.kind_of("supervisor_slept", reason=reason), kind, reason)

    def test_a_pr_waiting_for_the_user(self):
        for decision in ("approved", "approved_with_notes"):
            self.assertEqual(self.kind_of("decision", decision=decision, reason="", **ON_TASK), "pr_ready")

    def test_a_merge_that_waits_is_for_the_queen_to_judge(self):
        self.assertIsNone(self.kind_of("decision", decision="merge_waits", reason="CI is running", **ON_TASK))

    def test_what_needs_the_user(self):
        self.assertEqual(self.kind_of("task_sized", slices=3, **ON_TASK), "needs_decision")
        self.assertIsNone(self.kind_of("task_sized", slices=0, **ON_TASK))
        self.assertEqual(self.kind_of("queen_decided", action="ask_user", note="?", **ON_TASK), "needs_decision")
        self.assertIsNone(self.kind_of("queen_decided", action="release", note="", **ON_TASK))

    def test_project_events(self):
        self.assertEqual(self.kind_of("project_checked", project="site", passed=False, detail=""), "failure")
        self.assertIsNone(self.kind_of("project_checked", project="site", passed=True, detail=""))
        self.assertEqual(self.kind_of("decision", decision="over_budget", reason="", **ON_TASK), "usage_limit")

    def test_the_queen_failing_uses_the_words_the_supervisor_writes(self):
        brief = f"{queen.COULD_NOT_FINISH}: no JSON."
        self.assertEqual(self.kind_of("queen_finished", run_id="r", brief=brief, backlog=[]), "failure")
        self.assertIsNone(self.kind_of("queen_finished", run_id="r", brief="All fine.", backlog=[]))

    def test_every_language_has_every_message(self):
        self.assertEqual(set(MESSAGES["fa"]), set(MESSAGES["en"]))


class NotifierTest(Base):
    def test_a_task_message_is_in_the_language_of_the_config_with_a_link(self):
        self.finished("blocked")
        self.assertIn("تصمیم تو", self.adapter.sent[0])
        self.assertIn(TASK, self.adapter.sent[0])
        self.assertTrue(self.adapter.sent[0].endswith("https://github.com/o/r/issues/7"))
        self.assertEqual(self.logged("notification_sent")[0]["kind"], "needs_decision")

    def test_changing_the_language_changes_only_the_message(self):
        self.use(list(NOTIFY_EVENTS), "en")
        self.finished("blocked")
        self.assertIn("needs your decision", self.adapter.sent[0])

    def test_an_unknown_language_falls_back_to_english(self):
        self.use(list(NOTIFY_EVENTS), "de")
        self.finished("blocked")
        self.assertIn("needs your decision", self.adapter.sent[0])

    def test_only_the_listed_events_are_sent(self):
        self.use(["failure"], "fa")
        self.finished("blocked")
        self.assertEqual(self.adapter.sent, [])
        self.finished("failed")
        self.assertEqual(len(self.adapter.sent), 1)

    def test_a_state_that_lasts_is_told_once_until_it_clears(self):
        for _ in range(3):
            self.slept("weekly usage at 90%, limit 60%")
        self.assertEqual(len(self.adapter.sent), 1)
        self.slept("tasks ran out")
        self.slept("weekly usage at 91%, limit 60%")
        self.assertEqual(len(self.adapter.sent), 2)

    def test_another_state_is_news(self):
        self.slept("weekly usage at 90%, limit 60%")
        self.slept("stopped by the user")
        self.assertEqual(len(self.adapter.sent), 2)

    def test_a_project_limit_is_told_once_a_day(self):
        for _ in range(2):
            self.store.record("decision", decision="over_budget", reason="limit", **ON_TASK)
        self.assertEqual(len(self.adapter.sent), 1)

    def test_a_failed_send_never_reaches_the_caller_and_is_logged(self):
        self.adapter.fails = NotifyError("Telegram answered 500")
        self.finished("failed")
        self.assertEqual(self.logged("notification_failed")[0]["reason"], "Telegram answered 500")

    def test_a_surprise_logs_only_its_type(self):
        self.adapter.fails = RuntimeError("secret-token")
        self.finished("failed")
        self.assertEqual(self.logged("notification_failed")[0]["reason"], "RuntimeError")

    def test_a_state_is_told_after_a_failed_send(self):
        self.adapter.fails = NotifyError("down")
        self.slept("stopped by the user")
        self.adapter.fails = None
        self.slept("stopped by the user")
        self.assertEqual(len(self.adapter.sent), 1)

    def test_a_crash_is_told(self):
        self.store.listeners[0].__self__.crashed()
        self.assertIn("از کار افتاد", self.adapter.sent[0])


class NotifierForTest(Base):
    def settings(self, adapter="telegram", language=None):
        telegram = SimpleNamespace(bot_token_env="TOKEN", chat_id_env="CHAT")
        notify_ = SimpleNamespace(adapter=adapter, telegram=telegram, events=["failure"], language=language)
        return SimpleNamespace(notify=notify_, language="fa")

    def test_off(self):
        self.assertIsNone(notifier_for(self.settings("none"), self.store))

    def test_its_own_language_comes_before_the_shared_one(self):
        self.assertEqual(notifier_for(self.settings(language="en"), self.store).language, "en")
        self.assertEqual(notifier_for(self.settings(), self.store).language, "fa")


class TelegramTest(unittest.TestCase):
    ENV = {"TOKEN": "t0ken", "CHAT": "42"}

    def send(self, opener):
        with mock.patch.dict("os.environ", self.ENV), mock.patch("kandu.notify.urllib.request.urlopen", opener):
            Telegram("TOKEN", "CHAT").send("سلام")

    def test_posts_to_the_chat(self):
        reply = mock.MagicMock()
        reply.__enter__.return_value.read.return_value = json.dumps({"ok": True})
        opener = mock.Mock(return_value=reply)
        self.send(opener)
        request = opener.call_args.args[0]
        self.assertIn("/bott0ken/sendMessage", request.full_url)
        self.assertIn(b"chat_id=42", request.data)

    def test_missing_settings_are_named_without_a_network_call(self):
        with mock.patch.dict("os.environ", {}, clear=True), self.assertRaises(NotifyError) as caught:
            Telegram("TOKEN", "CHAT").send("x")
        self.assertIn("TOKEN", str(caught.exception))

    def test_no_failure_carries_the_token(self):
        error = notify.urllib.error.HTTPError("https://api.telegram.org/bott0ken/x", 401, "no", {}, None)
        for failure in (error, TimeoutError("t0ken"), OSError("t0ken")):
            with self.assertRaises(NotifyError) as caught:
                self.send(mock.Mock(side_effect=failure))
            self.assertNotIn("t0ken", str(caught.exception))

    def test_a_refusal_is_an_error(self):
        reply = mock.MagicMock()
        reply.__enter__.return_value.read.return_value = json.dumps({"ok": False})
        with self.assertRaises(NotifyError):
            self.send(mock.Mock(return_value=reply))


class DoctorTest(unittest.TestCase):
    def test_reports_what_is_missing(self):
        with tempfile.TemporaryDirectory() as local, mock.patch.dict("os.environ", {}, clear=True):
            on = SimpleNamespace(adapter="telegram", telegram=SimpleNamespace(bot_token_env="TOKEN", chat_id_env="CHAT"))
            level, text = doctor.check_notify(on, local)
            self.assertEqual(level, doctor.WARN)
            self.assertIn("TOKEN", text)
            (Path(local) / ".env").write_text("TOKEN=x\nCHAT=1\n", encoding="utf-8")
            self.assertEqual(doctor.check_notify(on, local)[0], doctor.OK)
            self.assertEqual(doctor.check_notify(SimpleNamespace(adapter="none"), local)[0], doctor.OK)


class SupervisorMainTest(Base):
    def run_main(self, cycle):
        notifier = mock.Mock()
        with mock.patch("sys.argv", ["supervisor"]), mock.patch.multiple(
                "kandu.supervisor", StateStore=lambda: self.store, load_config=mock.Mock(), Supervisor=mock.Mock(),
                GitHub=mock.Mock(), BoardCache=mock.Mock(), Runner=mock.Mock(), _locked_cycle=cycle,
                notifier_for=lambda settings, store: notifier):
            supervisor.main()
        return notifier

    def test_a_real_cycle_listens_to_the_log(self):
        self.store.listeners.clear()
        notifier = self.run_main(mock.Mock())
        self.assertEqual(self.store.listeners, [notifier.on_event])
        notifier.crashed.assert_not_called()

    def test_a_crash_is_told_and_still_raised(self):
        notifier = mock.Mock()
        with mock.patch("sys.argv", ["supervisor"]), mock.patch.multiple(
                "kandu.supervisor", StateStore=lambda: self.store, load_config=mock.Mock(), Supervisor=mock.Mock(),
                GitHub=mock.Mock(), BoardCache=mock.Mock(), Runner=mock.Mock(),
                _locked_cycle=mock.Mock(side_effect=RuntimeError), notifier_for=lambda settings, store: notifier):
            with self.assertRaises(RuntimeError):
                supervisor.main()
        notifier.crashed.assert_called_once()


class EventLogTest(Base):
    def test_a_new_kind_of_event_does_not_wake_the_queen(self):
        self.finished("merged")
        self.store.record("queen_finished", run_id="r", brief="b", backlog=[])
        self.store.record("notification_sent", kind="failure", key=None)
        before, news = queen.since_last_look(self.store)
        self.assertFalse(queen.worth_a_look(before, news, [], self.store.today()))


if __name__ == "__main__":
    unittest.main()
