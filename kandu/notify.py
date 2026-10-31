"""Tells the user the important things, as they enter the event log (DESIGN §8).

Each message comes from a fixed text: an error or an agent's words never reach
a third party, and details stay on the dashboard. A notification can never stop
a cycle; one that fails is recorded and the dashboard shows it.
"""
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .queen import COULD_NOT_FINISH

TIMEOUT_SECONDS = 10
QUIET_SLEEPS = ("nothing ready", "tasks ran out", "waiting for a machine", "cycle time is up",
                "stopped while an agent was running")  # The last two: the next cycle resumes the task.
BOARDS_UNREADABLE = "could not read the boards"
QUEEN_TROUBLE = "the queen could not look"
LIMIT_SLEEPS = ("5-hour usage", "weekly usage", "the queen reached her daily token limit")
USAGE_UNKNOWN = "usage unknown"
FAILED_OUTCOMES = {"failed": "task_failed", "deploy_failed": "deploy_failed",
                   "live_check_failed": "live_check_failed", "sizing_failed": "sizing_failed"}
STUCK_OUTCOMES = {"blocked": "blocked", "needs_info": "needs_info", "pr_closed": "pr_closed"}
READY_FOR_USER = ("approved", "approved_with_notes")

MESSAGES = {
    "en": {
        "stopped": "Kandu's supervisor is stopped. No task starts until it is resumed.",
        "cycle_trouble": "A Kandu cycle ran into trouble. The details are on the dashboard.",
        "crashed": "A Kandu cycle crashed. The details are in its service log.",
        "queen_trouble": "The queen could not finish her look. The details are on the dashboard.",
        "usage_limit": "Kandu reached a usage limit and starts no new work for now.",
        "project_limit": "{project} reached its daily token limit.",
        "claude_login": "Kandu cannot read Claude's usage, so the login may have stopped working. No agent starts.",
        "task_failed": "{task} stopped on an error. The details are on the dashboard.",
        "deploy_failed": "{task} was merged, but the deploy failed. Nothing was reverted.",
        "live_check_failed": "{task} is deployed, but the live check failed. Nothing was reverted.",
        "sizing_failed": "Kandu could not size {task}.",
        "check_failed": "{project} failed the check after its merges. The details are on the dashboard.",
        "blocked": "{task} is blocked and needs your decision.",
        "needs_info": "{task} needs more information from you.",
        "pr_closed": "The PR of {task} was closed, so the task stopped.",
        "split": "A proposal to split {task} waits for you.",
        "queen_asks": "The queen has a question about {task}. It is on the dashboard.",
        "pr_ready": "The PR of {task} is ready for your approval.",
    },
    "fa": {
        "stopped": "ناظر کندو متوقف است و تا ادامه ندهی هیچ تسکی شروع نمی‌شود.",
        "cycle_trouble": "یک چرخه‌ی کندو به مشکل خورد. جزئیات در داشبورد است.",
        "crashed": "یک چرخه‌ی کندو از کار افتاد. جزئیات در لاگ سرویس است.",
        "queen_trouble": "ملکه نتوانست بررسی‌اش را تمام کند. جزئیات در داشبورد است.",
        "usage_limit": "کندو به سقف مصرف رسید و فعلاً کار تازه‌ای شروع نمی‌کند.",
        "project_limit": "{project} به سقف روزانه‌ی توکن رسید.",
        "claude_login": "کندو نمی‌تواند مصرف Claude را بخواند و شاید ورود از کار افتاده باشد. هیچ عاملی شروع نمی‌شود.",
        "task_failed": "{task} با خطا متوقف شد. جزئیات در داشبورد است.",
        "deploy_failed": "{task} ادغام شد ولی deploy شکست خورد. چیزی برگردانده نشد.",
        "live_check_failed": "{task} روی نسخه‌ی زنده رفت ولی بررسی زنده رد شد. چیزی برگردانده نشد.",
        "sizing_failed": "کندو نتوانست اندازه‌ی {task} را تعیین کند.",
        "check_failed": "{project} در بررسی بعد از ادغام‌ها رد شد. جزئیات در داشبورد است.",
        "blocked": "{task} گیر کرده و تصمیم تو را لازم دارد.",
        "needs_info": "{task} اطلاعات بیشتری از تو لازم دارد.",
        "pr_closed": "PR مربوط به {task} بسته شد و تسک متوقف شد.",
        "split": "پیشنهاد تقسیم {task} منتظر تو است.",
        "queen_asks": "ملکه درباره‌ی {task} سؤالی دارد. سؤالش در داشبورد است.",
        "pr_ready": "PR مربوط به {task} آماده‌ی تأیید تو است.",
    },
}


class NotifyError(Exception):
    """Holds a safe reason: it goes into the log, so never the token or its URL."""


@dataclass
class Message:
    kind: str       # one of config.NOTIFY_EVENTS
    text: str       # a name in MESSAGES
    key: str | None = None  # the same key is told once; None for something that happens once anyway
    until_clear: bool = False  # told again after the supervisor has left this state
    values: dict = field(default_factory=dict)


def _sleep_message(reason):
    """A sleep is a state, so each kept state has one key."""
    if QUEEN_TROUBLE in reason or reason.startswith(BOARDS_UNREADABLE):
        return Message("failure", "cycle_trouble", "sleep:trouble", True)
    if reason.startswith(QUIET_SLEEPS):
        return None
    if reason.startswith(LIMIT_SLEEPS):
        return Message("usage_limit", "usage_limit", "sleep:limit", True)
    if reason.startswith(USAGE_UNKNOWN):
        return Message("claude_login", "claude_login", "sleep:login", True)
    return Message("supervisor_stopped", "stopped", "sleep:stopped", True)


def _task_message(kind, text, event):
    return Message(kind, text, values={"task": event["task"]})


def message_for(event):
    """Returns what the user must hear about this event, or None."""
    kind = event["type"]
    if kind == "supervisor_slept":
        return _sleep_message(event["reason"])
    if kind == "task_finished":
        if event["outcome"] in FAILED_OUTCOMES:
            return _task_message("failure", FAILED_OUTCOMES[event["outcome"]], event)
        if event["outcome"] in STUCK_OUTCOMES:
            return _task_message("needs_decision", STUCK_OUTCOMES[event["outcome"]], event)
    if kind == "task_sized" and event["slices"]:
        return _task_message("needs_decision", "split", event)
    if kind == "queen_decided" and event["action"] == "ask_user":
        return _task_message("needs_decision", "queen_asks", event)
    if kind == "decision" and event["decision"] in READY_FOR_USER:
        return _task_message("pr_ready", "pr_ready", event)
    if kind == "decision" and event["decision"] == "over_budget":
        key = f"over_budget:{event['project']}:{event['ts'][:10]}"
        return Message("usage_limit", "project_limit", key, values={"project": event["project"]})
    if kind == "project_checked" and not event["passed"]:
        return Message("failure", "check_failed", values={"project": event["project"]})
    if kind == "queen_finished" and event["brief"].startswith(COULD_NOT_FINISH):
        return Message("failure", "queen_trouble")
    return None


def _render(message, language):
    text = MESSAGES.get(language, MESSAGES["en"])[message.text].format(**message.values)
    task = message.values.get("task")
    if task is None:
        return text
    repo, number = task.split("#")
    return f"{text}\nhttps://github.com/{repo}/issues/{number}"


class Telegram:
    def __init__(self, bot_token_env, chat_id_env):
        self.token_env, self.chat_env = bot_token_env, chat_id_env

    def send(self, text):
        token, chat = os.environ.get(self.token_env), os.environ.get(self.chat_env)
        if not token or not chat:
            raise NotifyError(f"{self.token_env} or {self.chat_env} is not set")
        body = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode("utf-8")
        request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as reply:
                if not json.load(reply).get("ok"):
                    raise NotifyError("Telegram refused the message")
        except urllib.error.HTTPError as e:
            raise NotifyError(f"Telegram answered {e.code}") from None
        except (OSError, ValueError) as e:
            raise NotifyError(f"could not reach Telegram: {type(e).__name__}") from None


class Notifier:
    def __init__(self, store, adapter, events, language):
        self.store = store
        self.adapter = adapter
        self.events = events
        self.language = language
        self._lock = threading.Lock()

    def on_event(self, event):
        message = message_for(event)
        if message is not None:
            self.tell(message)

    def crashed(self):
        self.tell(Message("failure", "crashed"))

    def _told(self, message):
        if not message.until_clear:
            return self.store.last_event("notification_sent", key=message.key) is not None
        for day in reversed(self.store.days()):
            for event in reversed(self.store.events(day)):
                if event["type"] == "notification_sent" and event["key"] == message.key:
                    return True
                if event["type"] == "supervisor_slept":
                    now = _sleep_message(event["reason"])
                    if now is None or now.key != message.key:
                        return False
        return False

    def tell(self, message):
        if message.kind not in self.events:
            return
        with self._lock:
            try:
                if message.key is not None and self._told(message):
                    return
                self.adapter.send(_render(message, self.language))
            except Exception as e:
                reason = str(e) if isinstance(e, NotifyError) else type(e).__name__
                self.store.record("notification_failed", kind=message.kind, key=message.key, reason=reason)
                return
            self.store.record("notification_sent", kind=message.kind, key=message.key)


def notifier_for(settings, store):
    """Returns the configured notifier, or None when notifications are off."""
    notify = settings.notify
    if notify.adapter == "none":
        return None
    adapter = Telegram(notify.telegram.bot_token_env, notify.telegram.chat_id_env)
    return Notifier(store, adapter, notify.events, notify.language or settings.language)
