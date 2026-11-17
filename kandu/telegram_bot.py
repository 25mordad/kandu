"""Telegram both ways: the bot's token and the chat kept in local/.env, and the buttons under a message (DESIGN §8).

The token and the chat stay in local/.env and in this process: the dashboard's state says only whether each is set,
and never puts one in a URL or a log. A button press counts only from the configured chat. Approving takes two presses:
the first names the PR's head as it is then, and the second approves only that head through the dashboard's own
approval, so a PR that changed in between is not approved. The texts are Kandu's own (notify.MESSAGES).
"""
import fcntl
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .notify import MESSAGES

TOKEN_SHAPE = re.compile(r"\d{5,15}:[A-Za-z0-9_-]{30,60}")
CHAT_SHAPE = re.compile(r"-?\d{1,20}")
CALLBACK_LIMIT = 64          # the bytes Telegram keeps for a button's data
POLL_SECONDS = 50            # how long one getUpdates call waits for a press
IDLE_SECONDS = 5             # how often a bot without its token looks again
TROUBLE_SECONDS = 30         # how long it waits after Telegram failed
HEAD_CHARS = 12              # how much of the head a confirm button carries
KEPT_CHATS = 10              # the newest chats that wrote to the bot, offered as the chat to use
APPROVE, SURE, CANCEL = "a", "y", "n"
OFFSET_FILE = "telegram-offset"


class TelegramError(Exception):
    """What Telegram answered, in words that never hold the token."""


class Refused(Exception):
    """The dashboard's approval said no; `str()` is its reason."""


def read_env(local_dir):
    """The variables in local/.env, as written there; quotes around a value are dropped."""
    path = Path(local_dir) / ".env"
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() and not name.lstrip().startswith("#"):
            values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def write_env(local_dir, values):
    """Sets each variable in local/.env, keeping every other line, readable by its owner only.

    One write at a time, as two saves at once would each keep only their own variable; the new file is its
    owner's only from the moment it exists, as it holds the bot's token."""
    path = Path(local_dir) / ".env"
    lock = os.open(path.with_name(".env.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        left = dict(values)
        for i, line in enumerate(lines):
            name = line.partition("=")[0].strip()
            if name in left and "=" in line and not line.lstrip().startswith("#"):
                lines[i] = f"{name}={left.pop(name)}"
        lines += [f"{name}={value}" for name, value in left.items()]
        fd, temp = tempfile.mkstemp(prefix=".env.", dir=path.parent)  # made 0600, with a name no one else uses
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write("\n".join(lines) + "\n")
            os.replace(temp, path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise
    finally:
        os.close(lock)


def call(token, method, params=None, timeout=10):
    """One Bot API call; returns its `result`."""
    body = urllib.parse.urlencode(params or {}).encode("utf-8")
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=body)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            answer = json.load(reply)
    except urllib.error.HTTPError as e:
        try:
            said = json.load(e).get("description", "")
        except ValueError:
            said = ""
        raise TelegramError(f"Telegram answered {e.code}" + (f": {said}" if said else "")) from None
    except (OSError, ValueError) as e:
        raise TelegramError(f"could not reach Telegram: {type(e).__name__}") from None
    if not answer.get("ok"):
        raise TelegramError(answer.get("description") or "Telegram refused it")
    return answer.get("result")


def _data(*parts):
    data = ":".join(parts)
    return data if len(data.encode("utf-8")) <= CALLBACK_LIMIT else None


def approve_markup(task, head, language):
    """The button under a message about a PR that waits for the user's yes, or None when its data would not fit.
    It carries the head the message is about, so a commit pushed after the message is never approved by it."""
    if not head or len(head) < HEAD_CHARS:
        return None
    data = _data(APPROVE, task, head[:HEAD_CHARS])
    if data is None:
        return None
    return {"inline_keyboard": [[{"text": _words(language)["tg_approve"], "callback_data": data}]]}


def _words(language):
    return MESSAGES.get(language, MESSAGES["en"])


def _chat_name(chat):
    return chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("username") or str(chat["id"])


class Bot:
    """The dashboard's side of the bot: its token and chat, the chats that wrote to it, and the presses it takes.

    `approve(task, head)` is the dashboard's approval; it raises Refused with its reason."""

    def __init__(self, api, approve):
        self.api = api
        self.approve = approve
        self.username = None    # the bot's name, from getMe
        self.error = None       # what went wrong the last time it spoke to Telegram, or None
        self.listening = False  # it takes presses now
        self.chats = {}         # id -> name, the newest last
        self._lock = threading.Lock()

    # The settings and the secrets.

    def _telegram(self):
        notify = self.api.config.settings.notify
        return notify.telegram if notify.adapter == "telegram" else None

    def _language(self):
        settings = self.api.config.settings
        return settings.notify.language or settings.language

    def secrets(self):
        telegram = self._telegram()
        if telegram is None:
            return "", ""
        env = read_env(self.api.local_dir)
        return env.get(telegram.bot_token_env, ""), env.get(telegram.chat_id_env, "")

    def status(self):
        """What the page may see: whether each is set, never the values."""
        telegram = self._telegram()
        if telegram is None:
            return None
        token, chat = self.secrets()
        return {"token_set": bool(token), "chat_set": bool(chat), "bot": self.username, "buttons": telegram.buttons,
                "listening": self.listening, "error": self.error}

    def set_token(self, token):
        telegram = self._telegram()
        if telegram is None:
            raise TelegramError("notify.adapter is not telegram")
        token = token.strip()
        if not TOKEN_SHAPE.fullmatch(token):
            raise TelegramError("this does not look like a bot token: digits, a colon, then letters")
        me = call(token, "getMe")
        write_env(self.api.local_dir, {telegram.bot_token_env: token})
        os.environ[telegram.bot_token_env] = token
        self.username, self.error, self.chats = me.get("username"), None, {}
        _offset_path(self.api).unlink(missing_ok=True)  # another bot's updates are not this one's

    def set_chat(self, chat):
        telegram = self._telegram()
        if telegram is None:
            raise TelegramError("notify.adapter is not telegram")
        chat = chat.strip()
        if not CHAT_SHAPE.fullmatch(chat):
            raise TelegramError("a chat ID is a whole number")
        write_env(self.api.local_dir, {telegram.chat_id_env: chat})
        os.environ[telegram.chat_id_env] = chat

    def found_chats(self):
        """The chats that wrote to the bot lately, newest first: one of them is the user's."""
        with self._lock:
            return [{"id": chat, "name": name} for chat, name in reversed(self.chats.items())]

    def test(self):
        token, chat = self.secrets()
        if not token or not chat:
            raise TelegramError("set the bot's token and the chat first")
        call(token, "sendMessage", {"chat_id": chat, "text": _words(self._language())["tg_test"]})

    # Taking presses.

    def run_forever(self):
        while True:
            try:
                waited = self.poll_once()
            except Exception as e:  # never stops: the dashboard says what went wrong
                self.error, self.listening = str(e) if isinstance(e, TelegramError) else type(e).__name__, False
                waited = False
                time.sleep(TROUBLE_SECONDS)
            if not waited:
                time.sleep(IDLE_SECONDS)

    def poll_once(self, wait=POLL_SECONDS):
        """Takes what reached the bot since last time. Returns whether it asked Telegram."""
        token, chat = self.secrets()
        if not token:
            self.listening = False
            return False
        if self.username is None:
            self.username = call(token, "getMe").get("username")
        offset = _read_offset(self.api)
        updates = call(token, "getUpdates", {"offset": offset, "timeout": wait,
                                             "allowed_updates": json.dumps(["message", "callback_query"])}, timeout=wait + 15)
        self.listening, self.error = True, None
        for update in updates:
            _write_offset(self.api, update["update_id"] + 1)
            self.take(update, token, chat)
        return True

    def take(self, update, token, chat):
        message = update.get("message")
        if message and "chat" in message:
            with self._lock:
                key = str(message["chat"]["id"])
                self.chats.pop(key, None)
                self.chats[key] = _chat_name(message["chat"])
                while len(self.chats) > KEPT_CHATS:
                    self.chats.pop(next(iter(self.chats)))
        press = update.get("callback_query")
        if press:
            self.press(press, token, chat)

    def press(self, press, token, chat):
        words = _words(self._language())
        message = press.get("message") or {}
        where = str((message.get("chat") or {}).get("id", ""))
        action, _, rest = (press.get("data") or "").partition(":")
        task, _, head = rest.partition(":")
        if not chat or where != chat:
            self._answer(token, press, words["tg_not_yours"])
            self.api.store.record("telegram_pressed", action=action, task=task, result="not_your_chat")
            return
        result = self._act(token, press, message, words, action, task, head)
        self.api.store.record("telegram_pressed", action=action, task=task, result=result)

    def _act(self, token, press, message, words, action, task, head):
        telegram = self._telegram()
        if telegram is None or not telegram.buttons:
            self._answer(token, press, words["tg_buttons_off"])
            self._markup(token, message, None)
            return "off"
        repo, _, number = task.partition("#")
        if action not in (APPROVE, SURE, CANCEL) or not number.isdigit() or repo.count("/") != 1:
            self._answer(token, press, "")
            return "unknown"
        if len(head) != HEAD_CHARS:  # every button carries the whole of its head's prefix, never less
            self._answer(token, press, words["tg_changed"])
            self._markup(token, message, None)
            return "changed"
        if action == CANCEL:
            self._answer(token, press, "")
            self._markup(token, message, approve_markup(task, head, self._language()))
            return "cancelled"
        pr = self.api.github.find_pr(repo, int(number))
        if pr is None or pr.get("state") != "OPEN":
            self._answer(token, press, words["tg_no_pr"])
            self._markup(token, message, None)
            return "no_pr"
        if not pr["headRefOid"].startswith(head):  # pushed to since the message: look at it on the dashboard
            self._answer(token, press, words["tg_changed"])
            self._markup(token, message, None)
            return "changed"
        if action == APPROVE:
            sure = _data(SURE, task, head)
            if sure is None:
                self._answer(token, press, words["tg_on_dashboard"])
                return "too_long"
            self._answer(token, press, "")
            self._markup(token, message, {"inline_keyboard": [[
                {"text": words["tg_sure"].format(head=pr["headRefOid"][:7]), "callback_data": sure},
                {"text": words["tg_cancel"], "callback_data": _data(CANCEL, task, head)}]]})
            return "asked"
        try:
            self.approve(task, pr["headRefOid"])
        except Refused as e:
            self._answer(token, press, words["tg_refused"].format(reason=str(e))[:190])
            self._markup(token, message, None)
            return "refused"
        self._answer(token, press, words["tg_approved"])
        self._markup(token, message, None)
        call(token, "sendMessage", {"chat_id": message["chat"]["id"], "text": f"✅ {words['tg_approved']}",
                                    "reply_to_message_id": message.get("message_id", "")})
        return "approved"

    def _answer(self, token, press, text):
        try:
            call(token, "answerCallbackQuery", {"callback_query_id": press["id"], "text": text})
        except TelegramError:
            pass  # a press answered late is still taken

    def _markup(self, token, message, markup):
        if not message.get("message_id"):
            return
        try:
            call(token, "editMessageReplyMarkup", {"chat_id": message["chat"]["id"], "message_id": message["message_id"],
                                                   "reply_markup": json.dumps(markup or {"inline_keyboard": []})})
        except TelegramError:
            pass  # an old message can no longer change; the press still counted


def _offset_path(api):
    return Path(api.store.root) / OFFSET_FILE


def _read_offset(api):
    try:
        return int(_offset_path(api).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _write_offset(api, offset):
    path = _offset_path(api)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{offset}\n", encoding="utf-8")
