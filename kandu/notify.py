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
from datetime import datetime

from . import waits
from .queen import COULD_NOT_FINISH

TIMEOUT_SECONDS = 10
QUIET_SLEEPS = ("nothing ready", "tasks ran out", "waiting for a machine", "cycle time is up",
                "cycle time ran out while an agent was running",
                "stopped while an agent was running")  # The last three: the next cycle resumes the task. The last
# one is how a cycle that ran out of time ended before; the events of those days still hold it.
BOARDS_UNREADABLE = "could not read the boards"
QUEEN_TROUBLE = "the queen could not look"
LIMIT_SLEEPS = ("5-hour usage", "weekly usage", "the queen reached her daily token limit")
USAGE_UNKNOWN = "usage unknown"
# The end-of-cycle check's problems, as the supervisor writes them in `project_checked`'s detail.
CHECK_NOT_RUN = "the check could not run"
TESTS_FAIL_IN = "tests fail in "
LIVE_DOWN = "the live URL does not answer"
FAILED_OUTCOMES = {"failed": "task_failed", "deploy_failed": "deploy_failed",
                   "live_check_failed": "live_check_failed", "sizing_failed": "sizing_failed"}
STUCK_OUTCOMES = {"blocked": "blocked", "needs_info": "needs_info", "pr_closed": "pr_closed",
                  "issue_closed": "issue_closed"}
# With the queen on she sees these first and stops the task herself when nothing can be done.
QUEENS_OUTCOMES = ("failed", "sizing_failed", "blocked", "needs_info", "pr_closed")
READY_FOR_USER = ("approved", "approved_with_notes")
# The first line of every message says how soon the user must act, so a glance sorts them.
STUCK, YOUR_TURN, DONE, FOR_YOUR_INFO = "stuck", "your_turn", "done", "for_your_info"
MARKS = {STUCK: "🔴", YOUR_TURN: "🟡", DONE: "🟢", FOR_YOUR_INFO: "⚪"}
LEVELS = {"pr_ready": YOUR_TURN, "pr_held": YOUR_TURN, "split": YOUR_TURN, "flight_left_open": YOUR_TURN,
          "task_done": DONE, "task_replaced": DONE,
          "split_made": FOR_YOUR_INFO, "check_not_run": FOR_YOUR_INFO,
          "queen_split": FOR_YOUR_INFO,
          "blocker_rewritten": FOR_YOUR_INFO,
          "usage_limit": FOR_YOUR_INFO, "project_limit": FOR_YOUR_INFO, "machine_back": FOR_YOUR_INFO}  # Everything else stops work: STUCK.

MESSAGES = {
    "en": {
        "stopped": "Kandu's supervisor is stopped. No task starts until it is resumed.",
        "cycle_trouble": "A Kandu cycle ran into trouble. The details are on the dashboard.",
        "crashed": "A Kandu cycle crashed. The details are in its service log.",
        "queen_trouble": "The queen could not finish her look. The details are on the dashboard.",
        "usage_limit": "Kandu reached a usage limit and starts no new work for now.",
        "project_limit": "{project} reached its daily token limit.",
        "claude_login": "Kandu cannot read Claude's usage, so the login may have stopped working. No agent starts.",
        "codex_login": "Kandu cannot read Codex's usage, so its login may have stopped working. No Codex agent starts.",
        "task_failed": "{task} stopped on an error. The details are on the dashboard.",
        "deploy_failed": "{task} was merged, but the deploy failed. Nothing was reverted.",
        "live_check_failed": "{task} is deployed, but the live check failed. Nothing was reverted.",
        "sizing_failed": "Kandu could not size {task}.",
        "check_failed": "{project} failed the check after its merges: {what}.",
        "check_tests": "the tests fail on the default branch of {repo}",
        "check_live": "its live URL does not answer",
        "check_other": "the details are on the dashboard",
        "check_and": "; ",
        "check_not_run": "Kandu could not run the check of {project} after its merges, so nothing is known of its "
                         "default branch. The details are on the dashboard.",
        "blocked": "{task} is blocked and needs your decision.",
        "needs_info": "{task} needs more information from you.",
        "pr_closed": "The PR of {task} was closed, so the task stopped.",
        "issue_closed": "The issue of {task} was closed while the task was open, so it stopped.",
        "task_replaced": "{task} was done by another PR or commit, so Kandu closed its own PR.",
        "card_stuck": "{task} is in Ready, but its issue is closed, so Kandu cannot work on it. It takes no worker's place.",
        "split": "A proposal to split {task} waits for you.",
        "flight_left_open": "{task} was closed without a merge, but its free flight is still open. End it from the "
                            "Free flights list when you are done with it.",
        "queen_asks": "The queen has a question about {task}. It is on the dashboard.",
        "loop_stopped": "The queen went round on {task} without getting it done, so Kandu stopped it. It waits for you.",
        "machine_unreachable": "Machine {machine} has not answered since {since}. Tasks waiting for it: {waiting}.",
        "machine_back": "Machine {machine} is reachable again. It had not answered since {since}.",
        "pr_held": "The PR of {task} passed review, but something holds its merge.",
        "pr_ready": "The PR of {task} is ready for your approval.",
        "task_done": "{task} is merged and done.",
        "split_made": "Kandu split {task} into smaller tasks by itself. Every slice can be undone.",
        "queen_split": "The queen accepted the proposal to split {task} into {slices}.",
        "blocker_rewritten": "The new task {task} named {old} as a blocker. {old} was split, so it waits for its "
                             "last slice {new} instead.",
        "backup_failed": "Kandu's daily backup failed. The previous copies are untouched. The details are on the dashboard.",
        "tg_approve": "✅ Approve the merge",
        "tg_sure": "✅ Yes, merge {head}",
        "tg_cancel": "Cancel",
        "tg_approved": "Approved from Telegram. Kandu merges it in the cycle that starts now.",
        "tg_changed": "The PR changed since this message, so nothing was approved. Look at it again first.",
        "tg_no_pr": "This task has no open PR now.",
        "tg_refused": "Not approved: {reason}",
        "tg_buttons_off": "Approving from Telegram is turned off on the dashboard.",
        "tg_not_yours": "This bot takes presses only from its own chat.",
        "tg_on_dashboard": "Approve this one on the dashboard.",
        "tg_test": "Kandu here: Telegram is connected. ✓",
        STUCK: "Stuck: Kandu cannot go on without you",
        YOUR_TURN: "Your turn: it waits for your yes",
        DONE: "Done",
        FOR_YOUR_INFO: "For your information",
    },
    "fa": {
        "stopped": "ناظر کندو متوقف است و تا ادامه ندهی هیچ تسکی شروع نمی‌شود.",
        "cycle_trouble": "یک چرخه‌ی کندو به مشکل خورد. جزئیات در داشبورد است.",
        "crashed": "یک چرخه‌ی کندو از کار افتاد. جزئیات در لاگ سرویس است.",
        "queen_trouble": "ملکه نتوانست بررسی‌اش را تمام کند. جزئیات در داشبورد است.",
        "usage_limit": "کندو به سقف مصرف رسید و فعلاً کار تازه‌ای شروع نمی‌کند.",
        "project_limit": "{project} به سقف روزانه‌ی توکن رسید.",
        "claude_login": "کندو نمی‌تواند مصرف Claude را بخواند و شاید ورود از کار افتاده باشد. هیچ عاملی شروع نمی‌شود.",
        "codex_login": "کندو نمی‌تواند مصرف Codex را بخواند و شاید ورودش از کار افتاده باشد. هیچ عامل Codex شروع نمی‌شود.",
        "task_failed": "{task} با خطا متوقف شد. جزئیات در داشبورد است.",
        "deploy_failed": "{task} ادغام شد ولی deploy شکست خورد. چیزی برگردانده نشد.",
        "live_check_failed": "{task} روی نسخه‌ی زنده رفت ولی بررسی زنده رد شد. چیزی برگردانده نشد.",
        "sizing_failed": "کندو نتوانست اندازه‌ی {task} را تعیین کند.",
        "check_failed": "{project} در بررسی بعد از ادغام‌ها رد شد: {what}.",
        "check_tests": "تست‌ها روی شاخه‌ی اصلی {repo} رد می‌شوند",
        "check_live": "آدرس زنده‌اش جواب نمی‌دهد",
        "check_other": "جزئیات در داشبورد است",
        "check_and": "، ",
        "check_not_run": "کندو نتوانست {project} را بعد از ادغام‌هایش بررسی کند، پس از حال شاخه‌ی اصلی‌اش خبری "
                         "نیست. جزئیات در داشبورد است.",
        "blocked": "{task} گیر کرده و تصمیم تو را لازم دارد.",
        "needs_info": "{task} اطلاعات بیشتری از تو لازم دارد.",
        "pr_closed": "PR مربوط به {task} بسته شد و تسک متوقف شد.",
        "issue_closed": "issue مربوط به {task} وسط کار بسته شد و تسک متوقف شد.",
        "task_replaced": "{task} با PR یا کامیت دیگری انجام شد و کندو PR خودش را بست.",
        "card_stuck": "{task} در Ready است، ولی issue آن بسته است و کندو نمی‌تواند رویش کار کند. جای هیچ کارگری را نمی‌گیرد.",
        "split": "پیشنهاد تقسیم {task} منتظر تو است.",
        "flight_left_open": "{task} بدون ادغام بسته شد، ولی پرواز آزادش هنوز باز است. هر وقت کارت با آن تمام شد، "
                            "از فهرست پروازهای آزاد ببندش.",
        "queen_asks": "ملکه درباره‌ی {task} سؤالی دارد. سؤالش در داشبورد است.",
        "loop_stopped": "ملکه روی {task} دور خودش چرخید و کار جلو نرفت، پس کندو آن را متوقف کرد. منتظر تو است.",
        "machine_unreachable": "ماشین {machine} از {since} جواب نمی‌دهد. تسک‌های منتظرش: {waiting}.",
        "machine_back": "ماشین {machine} دوباره در دسترس است. از {since} جواب نمی‌داد.",
        "pr_held": "PR مربوط به {task} بازبینی را رد کرد، ولی چیزی ادغامش را نگه داشته است.",
        "pr_ready": "PR مربوط به {task} آماده‌ی تأیید تو است.",
        "task_done": "{task} ادغام شد و تمام شد.",
        "split_made": "کندو {task} را خودش به تسک‌های کوچک‌تر شکست. همه‌ی تکه‌ها برگشت‌پذیرند.",
        "queen_split": "ملکه پیشنهاد تقسیم {task} به {slices} را پذیرفت.",
        "blocker_rewritten": "تسک تازه‌ی {task} منتظر {old} بود. {old} شکسته شده، پس به جایش منتظر آخرین تکه‌اش، "
                             "{new}، می‌ماند.",
        "backup_failed": "پشتیبان روزانه‌ی کندو شکست خورد. نسخه‌های قبلی دست نخورده‌اند. جزئیات در داشبورد است.",
        "tg_approve": "✅ تأیید ادغام",
        "tg_sure": "✅ بله، {head} ادغام شود",
        "tg_cancel": "انصراف",
        "tg_approved": "از تلگرام تأیید شد. کندو در چرخه‌ای که همین حالا شروع می‌شود ادغامش می‌کند.",
        "tg_changed": "PR بعد از این پیام عوض شده، پس چیزی تأیید نشد. اول دوباره نگاهش کن.",
        "tg_no_pr": "این تسک الان PR بازی ندارد.",
        "tg_refused": "تأیید نشد: {reason}",
        "tg_buttons_off": "تأیید از تلگرام در داشبورد خاموش است.",
        "tg_not_yours": "این بات فقط از چت خودش دکمه می‌پذیرد.",
        "tg_on_dashboard": "این یکی را در داشبورد تأیید کن.",
        "tg_test": "کندو این‌جاست: تلگرام وصل است. ✓",
        STUCK: "گیر کرده: کندو بدون تو جلو نمی‌رود",
        YOUR_TURN: "نوبت توست: منتظر تأیید تو است",
        DONE: "انجام شد",
        FOR_YOUR_INFO: "برای اطلاع",
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
    wait: dict | None = None  # what the task waits on from the user, where and the next step (waits.py)


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


def _task_message(kind, text, event, wait=None):
    return Message(kind, text, values={"task": event["task"]}, wait=wait)


def _ready_message(event):
    """A PR left to the user. When a failing check or an open blocker holds it, the yes is not the next step."""
    wait = waits.settled(event["reason"]) or waits.yes()
    return _task_message("pr_ready", "pr_held" if wait["held"] else "pr_ready", event, wait)


def _check_message(event):
    """Told once a day per project: each cycle with merges checks again, and a new day may be new trouble."""
    detail = event["detail"]
    text = "check_not_run" if detail.startswith(CHECK_NOT_RUN) else "check_failed"
    failing = [part.removeprefix(TESTS_FAIL_IN) for part in detail.split("; ") if part.startswith(TESTS_FAIL_IN)]
    values = {"project": event["project"], "failing": failing, "live_down": LIVE_DOWN in detail}
    return Message("failure", text, f"{text}:{event['project']}:{event['ts'][:10]}", values=values)


def _check_problems(values, words):
    """What the check found, in fixed words."""
    found = [words["check_tests"].format(repo=repo) for repo in values["failing"]]
    found += [words["check_live"]] if values["live_down"] else []
    return words["check_and"].join(found) or words["check_other"]


def _machine_message(text, event):
    """Once per outage: its key is the machine and when the outage began."""
    since = datetime.fromisoformat(event["since"]).strftime("%Y-%m-%d %H:%M")
    values = {"machine": event["machine"], "since": since, "waiting": event.get("waiting")}
    return Message("machine_unreachable", text, f"{text}:{event['machine']}:{event['since']}", values=values)


def message_for(event, queen=False):
    """Returns what the user must hear about this event, or None. `queen`: the queen is on."""
    kind = event["type"]
    if kind == "task_finished" and queen and event["outcome"] in QUEENS_OUTCOMES:
        return None
    if kind == "loop_stopped":
        return Message("needs_decision", "loop_stopped", f"loop_stopped:{event['task']}:{event['ts']}",
                       values={"task": event["task"]}, wait=waits.decision())
    if kind == "supervisor_slept":
        return _sleep_message(event["reason"])
    if kind == "task_finished":
        if event["outcome"] in FAILED_OUTCOMES:
            return _task_message("failure", FAILED_OUTCOMES[event["outcome"]], event)
        if event["outcome"] in STUCK_OUTCOMES:
            return _task_message("needs_decision", STUCK_OUTCOMES[event["outcome"]], event, waits.decision())
        if event["outcome"] == "merged":
            return _task_message("task_done", "task_done", event)
        if event["outcome"] == "replaced":
            return _task_message("task_done", "task_replaced", event)
    if kind == "card_stuck":
        return Message("failure", "card_stuck", f"card_stuck:{event['task']}", values={"task": event["task"]})
    if kind == "flight_left_open":
        return Message("needs_decision", "flight_left_open", f"flight_left_open:{event['task']}:{event['session']}",
                       values={"task": event["task"]})
    if kind == "decision" and event["decision"] == "split_waits":
        return _task_message("needs_decision", "split", event, waits.yes())
    if kind == "decision" and event["decision"] == "split_approved":
        return _task_message("task_done", "split_made", event)
    if kind == "queen_accepted_split":
        return Message("task_done", "queen_split", values={"task": event["task"], "slices": ", ".join(event["children"])})
    if kind == "blocker_rewritten" and event["when"] == "defined":  # one waiting before the split is no news
        return Message("task_done", "blocker_rewritten",
                       values={"task": event["task"], "old": event["old"], "new": event["new"]})
    if kind == "queen_decided" and event["action"] in ("ask_user", "drop_blocker"):
        return _task_message("needs_decision", "queen_asks", event, waits.decision())
    if kind == "decision" and event["decision"] in READY_FOR_USER:
        return _ready_message(event)
    if kind == "decision" and event["decision"] == "over_budget":
        key = f"over_budget:{event['project']}:{event['ts'][:10]}"
        return Message("usage_limit", "project_limit", key, values={"project": event["project"]})
    if kind == "project_checked" and not event["passed"]:
        return _check_message(event)
    if kind == "machine_unreachable":
        return _machine_message("machine_unreachable", event)
    if kind == "machine_up" and event["told"]:  # the user heard of the outage, so hears of its end
        return _machine_message("machine_back", event)
    if kind == "codex_usage_read" and not event["readable"]:  # written once each time it cannot be read
        return Message("claude_login", "codex_login", f"codex_login:{event['ts']}")
    if kind == "backup_failed":  # Told once a day: the timer tries again until a copy is made.
        return Message("failure", "backup_failed", f"backup_failed:{event['ts'][:10]}")
    if kind == "queen_finished" and event["brief"].startswith(COULD_NOT_FINISH):
        return Message("failure", "queen_trouble")
    return None


def _render(message, language):
    words = MESSAGES.get(language, MESSAGES["en"])
    level = LEVELS.get(message.text, STUCK)
    values = message.values | ({"what": _check_problems(message.values, words)} if message.text == "check_failed" else {})
    text = f"{MARKS[level]} {words[level]}\n" + words[message.text].format(**values)
    if message.wait:  # what it waits on, where and the next step
        text += "".join(f"\n{line}" for line in waits.lines(message.wait, language))
    task = message.values.get("task")
    if task is None:  # a repo whose tests fail is linked, so the user sees its last commits
        return text + "".join(f"\nhttps://github.com/{repo}/commits" for repo in message.values.get("failing", ()))
    repo, number = task.split("#")
    return f"{text}\nhttps://github.com/{repo}/issues/{number}"


class Telegram:
    def __init__(self, bot_token_env, chat_id_env):
        self.token_env, self.chat_env = bot_token_env, chat_id_env

    def send(self, text, buttons=None):
        token, chat = os.environ.get(self.token_env), os.environ.get(self.chat_env)
        if not token or not chat:
            raise NotifyError(f"{self.token_env} or {self.chat_env} is not set")
        fields = {"chat_id": chat, "text": text} | ({"reply_markup": json.dumps(buttons)} if buttons else {})
        body = urllib.parse.urlencode(fields).encode("utf-8")
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
    def __init__(self, store, adapter, events, language, queen=False, buttons=False):
        self.queen = queen
        self.buttons = buttons  # an approve button under a PR that waits for the yes (telegram_bot.py)
        self.store = store
        self.adapter = adapter
        self.events = events
        self.language = language
        self._lock = threading.Lock()

    def on_event(self, event):
        message = message_for(event, self.queen)
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

    def _buttons(self, message):
        if not self.buttons or message.text != "pr_ready":
            return None
        from .telegram_bot import approve_markup  # here, as telegram_bot reads this module's words
        task = message.values["task"]
        reviewed = self.store.last_event("review_finished", task=task)  # the head the yes is asked for
        return approve_markup(task, reviewed and reviewed.get("head_sha"), self.language)

    def tell(self, message):
        if message.kind not in self.events:
            return
        with self._lock:
            try:
                if message.key is not None and self._told(message):
                    return
                markup = self._buttons(message)
                text = _render(message, self.language)
                self.adapter.send(text, markup) if markup else self.adapter.send(text)
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
    return Notifier(store, adapter, notify.events, notify.language or settings.language, settings.supervisor.queen,
                    getattr(notify.telegram, "buttons", False))
