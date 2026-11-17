"""Telegram both ways (DESIGN §8): the token and the chat set from the dashboard, and approving a merge from Telegram."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kandu import telegram_bot
from kandu.config import load_config, set_telegram_buttons
from kandu.dashboard import dispatch
from kandu.github import BoardItem
from kandu.notify import Message, Notifier
from tests.test_dashboard import Base, REPO, TOKEN

BOT_TOKEN = "123456789:" + "A" * 35
CHAT = "4242"
TASK = f"{REPO}#1"
HEAD = "abc123def456789"
H = HEAD[:12]  # what a button carries of its head
REVIEW = dict(task=TASK, project="website", run_id="r", round=1, findings=0, blockers=0, door="one-way",
              criteria_met=True, wait_for_user=["Merge?"], user_brief=None)


class Telegram:
    """Telegram as the bot sees it: every call is kept, and the answers are set by the test."""

    def __init__(self):
        self.calls = []
        self.fails = set()

    def __call__(self, token, method, params=None, timeout=10):
        self.calls.append((token, method, dict(params or {})))
        if method in self.fails:
            raise telegram_bot.TelegramError("Telegram answered 401: Unauthorized")
        return {"getMe": {"username": "kandu_test_bot"}, "getUpdates": []}.get(method, True)

    def made(self, method):
        return [params for _, name, params in self.calls if name == method]


class EnvTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)

    def test_it_keeps_every_other_line_and_only_its_owner_reads_it(self):
        (self.local / ".env").write_text("# secrets\nKANDU_DASHBOARD_TOKEN=keep\nKANDU_TELEGRAM_BOT_TOKEN=old\n", encoding="utf-8")
        telegram_bot.write_env(self.local, {"KANDU_TELEGRAM_BOT_TOKEN": "new", "KANDU_TELEGRAM_CHAT_ID": "7"})
        self.assertEqual((self.local / ".env").read_text(encoding="utf-8"),
                         "# secrets\nKANDU_DASHBOARD_TOKEN=keep\nKANDU_TELEGRAM_BOT_TOKEN=new\nKANDU_TELEGRAM_CHAT_ID=7\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.local / ".env").st_mode), 0o600)
        self.assertEqual(telegram_bot.read_env(self.local),
                         {"KANDU_DASHBOARD_TOKEN": "keep", "KANDU_TELEGRAM_BOT_TOKEN": "new", "KANDU_TELEGRAM_CHAT_ID": "7"})

    def test_a_missing_file_is_made_and_quotes_are_read_off(self):
        self.assertEqual(telegram_bot.read_env(self.local), {})
        telegram_bot.write_env(self.local, {"A": "1"})
        (self.local / ".env").write_text('A="1"\nB=\'2\'\n', encoding="utf-8")
        self.assertEqual(telegram_bot.read_env(self.local), {"A": "1", "B": "2"})

    def test_a_button_that_would_not_fit_is_left_out(self):
        self.assertEqual(telegram_bot.approve_markup(TASK, HEAD, "en")["inline_keyboard"][0][0]["callback_data"],
                         f"a:{TASK}:{H}")
        self.assertIsNone(telegram_bot.approve_markup("o/" + "r" * 80 + "#1", HEAD, "en"))

    def test_no_button_without_the_head_it_is_about(self):
        self.assertIsNone(telegram_bot.approve_markup(TASK, None, "en"))
        self.assertIsNone(telegram_bot.approve_markup(TASK, "abc", "en"))


class BotBase(Base):
    def setUp(self):
        super().setUp()
        self.local = self.root / "local"
        self.telegram = Telegram()
        patch = mock.patch.object(telegram_bot, "call", self.telegram)
        patch.start()
        self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        self.api.start_cycle = mock.Mock(return_value=True)

    def post(self, target, body="", token=TOKEN):
        return dispatch(self.api, "POST", target, token, body)

    def connect(self, buttons=True):
        telegram_bot.write_env(self.local, {"KANDU_TELEGRAM_BOT_TOKEN": BOT_TOKEN, "KANDU_TELEGRAM_CHAT_ID": CHAT})
        if buttons:
            set_telegram_buttons(True, self.local)
            self.api.refresh_config()

    def reviewed(self, head=HEAD):
        self.store.record("review_finished", task=TASK, project="website", run_id="r", round=1, head_sha=head,
                          findings=0, blockers=0, door="one-way", criteria_met=True, wait_for_user=["Merge?"], user_brief=None)
        self.github.find_pr.return_value = {"number": 7, "title": "T", "url": "u", "state": "OPEN", "headRefOid": head, "comments": []}
        self.github.board_items.return_value = [BoardItem(REPO, 1, "t", "u", "In review", ["agent"], "")]

    def press(self, data, chat=CHAT):
        update = {"update_id": 1, "callback_query": {"id": "q", "data": data,
                                                     "message": {"message_id": 9, "chat": {"id": int(chat)}}}}
        self.api.bot.take(update, BOT_TOKEN, CHAT)
        return self.store.last_event("telegram_pressed")


class ConnectTest(BotBase):
    def test_the_token_goes_in_the_body_is_checked_and_the_state_says_only_that_it_is_set(self):
        status, body = self.post("/api/v1/telegram/token", f"token={BOT_TOKEN}")
        self.assertEqual(status, 200)
        self.assertEqual(self.telegram.calls[0][:2], (BOT_TOKEN, "getMe"))
        self.assertEqual(telegram_bot.read_env(self.local)["KANDU_TELEGRAM_BOT_TOKEN"], BOT_TOKEN)
        state = json.dumps(self.get("/api/v1/state")[1])
        self.assertNotIn(BOT_TOKEN, state)
        self.assertNotIn(BOT_TOKEN, json.dumps(body))
        self.assertNotIn(BOT_TOKEN, json.dumps(self.store.events(self.store.today())))
        telegram = self.get("/api/v1/state")[1]["notify"]["telegram"]
        self.assertEqual((telegram["token_set"], telegram["chat_set"], telegram["bot"]), (True, False, "kandu_test_bot"))
        self.assertEqual(self.store.last_event("telegram_changed")["what"], "token")

    def test_a_secret_in_the_url_is_refused_before_anything_is_written(self):
        for target in (f"/api/v1/telegram/token?token={BOT_TOKEN}", "/api/v1/telegram/chat?chat_id=1"):
            status, body = self.post(target)
            self.assertEqual(status, 400)
            self.assertIn("body", body["error"])
        self.assertEqual(self.telegram.calls, [])
        self.assertEqual(telegram_bot.read_env(self.local), {})

    def test_a_wrong_token_writes_nothing(self):
        self.assertEqual(self.post("/api/v1/telegram/token", "token=nonsense")[0], 400)
        self.telegram.fails.add("getMe")
        status, body = self.post("/api/v1/telegram/token", f"token={BOT_TOKEN}")
        self.assertEqual(status, 400)
        self.assertIn("401", body["error"])
        self.assertEqual(telegram_bot.read_env(self.local), {})
        self.assertEqual(self.post("/api/v1/telegram/token", f"token={BOT_TOKEN}", token="wrong")[0], 403)

    def test_the_chat_is_a_number_and_the_chats_that_wrote_are_offered(self):
        self.assertEqual(self.post("/api/v1/telegram/chat", "chat_id=me")[0], 400)
        self.assertEqual(self.post("/api/v1/telegram/chat", "chat_id=-100123")[0], 200)
        self.assertEqual(telegram_bot.read_env(self.local)["KANDU_TELEGRAM_CHAT_ID"], "-100123")
        for number, name in ((5, "Ana"), (6, "Ben"), (5, "Ana")):
            self.api.bot.take({"update_id": number, "message": {"chat": {"id": number, "first_name": name}}}, BOT_TOKEN, "")
        self.assertEqual(self.post("/api/v1/telegram/chats")[1]["chats"], [{"id": "5", "name": "Ana"}, {"id": "6", "name": "Ben"}])

    def test_a_test_message_needs_both(self):
        self.assertEqual(self.post("/api/v1/telegram/test")[0], 400)
        self.connect(buttons=False)
        self.assertEqual(self.post("/api/v1/telegram/test")[1], {"sent": True})
        self.assertEqual(self.telegram.made("sendMessage")[0]["chat_id"], CHAT)

    def test_the_buttons_are_off_by_default_and_turned_on_in_the_config(self):
        self.assertFalse(self.get("/api/v1/state")[1]["notify"]["telegram"]["buttons"])
        self.assertEqual(self.post("/api/v1/telegram/buttons?enabled=maybe")[0], 400)
        self.assertTrue(self.post("/api/v1/telegram/buttons?enabled=true")[1]["notify"]["telegram"]["buttons"])
        self.assertTrue(load_config(self.local).settings.notify.telegram.buttons)
        self.assertIn("bot_token_env: KANDU_TELEGRAM_BOT_TOKEN", (self.local / "kandu.yaml").read_text(encoding="utf-8"))
        self.post("/api/v1/telegram/buttons?enabled=false")
        self.assertFalse(load_config(self.local).settings.notify.telegram.buttons)

    def test_the_bot_reads_its_updates_from_where_it_stopped(self):
        self.assertFalse(self.api.bot.poll_once(wait=0))  # no token: it does not ask Telegram
        self.connect()
        self.assertTrue(self.api.bot.poll_once(wait=0))
        self.assertTrue(self.api.bot.status()["listening"])
        self.assertEqual(self.telegram.made("getUpdates")[0]["offset"], 0)
        telegram_bot._write_offset(self.api, 8)
        self.api.bot.poll_once(wait=0)
        self.assertEqual(self.telegram.made("getUpdates")[1]["offset"], 8)


class ApproveTest(BotBase):
    def setUp(self):
        super().setUp()
        self.connect()
        self.reviewed()

    def test_two_presses_approve_the_head_the_first_one_showed(self):
        self.assertEqual(self.press(f"a:{TASK}:{H}")["result"], "asked")
        sure = json.loads(self.telegram.made("editMessageReplyMarkup")[-1]["reply_markup"])["inline_keyboard"][0]
        self.assertEqual(sure[0]["callback_data"], f"y:{TASK}:{HEAD[:12]}")
        self.assertIn(HEAD[:7], sure[0]["text"])
        self.assertEqual(sure[1]["callback_data"], f"n:{TASK}:{H}")
        self.assertIsNone(self.store.last_event("user_approved"))
        self.assertEqual(self.press(sure[0]["callback_data"])["result"], "approved")
        self.assertEqual(self.store.last_event("user_approved")["head_sha"], HEAD)
        self.api.start_cycle.assert_called_once()

    def test_a_head_pushed_after_the_message_is_not_approved_by_its_button(self):
        self.reviewed(head="fff000fff000fff")
        self.assertEqual(self.press(f"a:{TASK}:{H}")["result"], "changed")
        self.assertEqual(self.press(f"y:{TASK}:{H}")["result"], "changed")
        self.assertIsNone(self.store.last_event("user_approved"))

    def test_a_head_shorter_than_a_button_carries_is_refused(self):
        for data in (f"y:{TASK}:a", f"y:{TASK}:{HEAD[:11]}", f"y:{TASK}", f"a:{TASK}"):
            self.assertEqual(self.press(data)["result"], "changed")
        self.assertIsNone(self.store.last_event("user_approved"))

    def test_a_head_that_changed_after_the_first_press_is_not_approved(self):
        self.press(f"a:{TASK}:{H}")
        self.reviewed(head="fff000fff000fff")
        self.assertEqual(self.press(f"y:{TASK}:{HEAD[:12]}")["result"], "changed")
        self.assertIsNone(self.store.last_event("user_approved"))

    def test_a_press_from_another_chat_does_nothing(self):
        self.assertEqual(self.press(f"y:{TASK}:{HEAD[:12]}", chat="999")["result"], "not_your_chat")
        self.assertIsNone(self.store.last_event("user_approved"))
        self.assertEqual(self.telegram.made("editMessageReplyMarkup"), [])

    def test_buttons_turned_off_approve_nothing(self):
        set_telegram_buttons(False, self.local)
        self.api.refresh_config()
        self.assertEqual(self.press(f"y:{TASK}:{HEAD[:12]}")["result"], "off")
        self.assertIsNone(self.store.last_event("user_approved"))

    def test_what_the_dashboard_would_refuse_is_refused_and_said(self):
        self.store.pause_project("website")
        self.assertEqual(self.press(f"y:{TASK}:{HEAD[:12]}")["result"], "refused")
        self.assertIn("paused", self.telegram.made("answerCallbackQuery")[-1]["text"])
        self.assertIsNone(self.store.last_event("user_approved"))

    def test_cancel_puts_the_first_button_back_and_a_closed_pr_takes_it_away(self):
        self.assertEqual(self.press(f"n:{TASK}:{H}")["result"], "cancelled")
        self.assertEqual(json.loads(self.telegram.made("editMessageReplyMarkup")[-1]["reply_markup"])["inline_keyboard"][0][0]["callback_data"], f"a:{TASK}:{H}")
        self.github.find_pr.return_value |= {"state": "MERGED"}
        self.assertEqual(self.press(f"a:{TASK}:{H}")["result"], "no_pr")
        self.assertEqual(json.loads(self.telegram.made("editMessageReplyMarkup")[-1]["reply_markup"]), {"inline_keyboard": []})

    def test_nonsense_data_does_nothing(self):
        for data in ("", "x:y", "a:not-a-task", f"z:{TASK}"):
            self.assertEqual(self.press(data)["result"], "unknown")
        self.assertIsNone(self.store.last_event("user_approved"))


class MessageButtonTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        from kandu.state import StateStore
        self.store = StateStore(Path(tmp.name) / "state")
        self.sent = []

    def tell(self, buttons, text):
        adapter = mock.Mock()
        adapter.send = lambda message, markup=None: self.sent.append(markup)
        Notifier(self.store, adapter, ["pr_ready", "failure"], "en", buttons=buttons).tell(
            Message("pr_ready" if text == "pr_ready" else "failure", text, values={"task": TASK}))
        return self.sent[-1]

    def test_only_a_pr_that_waits_for_the_yes_carries_the_button_and_only_when_on(self):
        self.assertIsNone(self.tell(True, "pr_ready"))  # no review yet: no head to carry
        self.store.record("review_finished", **REVIEW, head_sha=HEAD)
        self.assertEqual(self.tell(True, "pr_ready")["inline_keyboard"][0][0]["callback_data"], f"a:{TASK}:{H}")
        self.assertIsNone(self.tell(False, "pr_ready"))
        self.assertIsNone(self.tell(True, "pr_held"))


if __name__ == "__main__":
    unittest.main()
