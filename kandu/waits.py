"""What a task that waits for the user waits on, where the problem is, and the user's next step.

The dashboard and the notifications both read it, so neither says only that a task waits. It is worked out from the
reasons Kandu records for a held merge (`merge.merge_waits` and the supervisor), never from an agent's words.
"""
import re

from .merge import FAILED, WRONG_BASE

YES, CHECK, BLOCKER, DECISION, UNKNOWN = "yes", "check", "blocker", "decision", "unknown"
CI_FAILING = "CI is failing"
TEST_RUN_BUSY = "another merge test of this project is running"  # one at a time per project; it passes by itself
USER_DECIDES = "the user decides next"  # a settled PR left to the user; what else holds it follows, as more reasons
REASONS_APART = "; "  # how Kandu joins the reasons of one decision
# Why a merge waits. A reason here needs a fix; one that passes by itself is pending; anything else is what only the
# user's yes answers: the door or the review's question.
MERGE_PROBLEMS = ("this head has no review in the local log", "a blocker is open", "an acceptance criterion is not met",
                  "the PR is a draft", "the PR has conflicts", CI_FAILING, "a review thread is open",
                  "a thread was resolved by someone other than its author", "the tests fail on this head",
                  "main's tests fail", WRONG_BASE,
                  "the tests failed twice on this head: they run again on a new commit or a recorded "
                  "\"test again\" request")
MERGE_PENDING = re.compile(r"(CI is running|another merge test of this project is running|the project is frozen|companion .+ is open and unreviewed|"
                           r"blocker \S+#\d+ is not merged yet)")
CHECK_NAME = re.compile(r"[^\w .:/()\[\]-]")  # a check's name is someone else's text: only plain characters stay
CHECK_URL = re.compile(r"https://[^\s;<>\"']+")
CHECK_REASON = re.compile(r"check (?P<name>[\w .:/()\[\]-]+?) is failing(?:: (?P<url>https://\S+))?")
BLOCKER_REASON = re.compile(r"blocker (?P<key>[\w.-]+/[\w.-]+#\d+) is not merged yet")


def failing_checks(rollup):
    """The failing checks of a PR's `statusCheckRollup`, each named as on the PR, with its details link or None."""
    checks = []
    for check in rollup or []:
        if (check.get("conclusion") or check.get("state") or check.get("status")) not in FAILED:
            continue
        name = " ".join(CHECK_NAME.sub("", str(check.get("name") or check.get("context") or "")).split())[:60]
        url = str(check.get("detailsUrl") or check.get("targetUrl") or "")
        checks.append({"name": name or "a check", "url": url if CHECK_URL.fullmatch(url) else None})
    return checks


def check_reasons(rollup):
    """One reason per failing check, so the log names each one with its link."""
    return [f"check {c['name']} is failing" + (f": {c['url']}" if c["url"] else "") for c in failing_checks(rollup)]


def user_decides(reason):
    """True when the review left the merge to the user."""
    return reason.split(REASONS_APART)[0] == USER_DECIDES


def _wait(on, checks=(), blockers=(), detail=(), held=False):
    return {"on": on, "checks": list(checks), "blockers": list(blockers), "detail": list(detail), "held": held}


def yes():
    return _wait(YES)


def decision():
    return _wait(DECISION)


def unknown():
    return _wait(UNKNOWN)


def from_reasons(reasons):
    """What a merge held for `reasons` waits on, or None when it waits for nothing but time.

    A failing check or an open blocker holds the merge whatever the user says, so then the yes is not the next step.
    With no reason at all, it says it does not know.
    """
    reasons = [r for r in reasons if r.strip()]
    if not reasons:
        return unknown()
    checks = [m.groupdict() for r in reasons if (m := CHECK_REASON.fullmatch(r))]
    blockers = [m["key"] for r in reasons if (m := BLOCKER_REASON.fullmatch(r))]
    if checks or CI_FAILING in reasons:
        return _wait(CHECK, checks, blockers, held=True)
    if blockers:
        return _wait(BLOCKER, blockers=blockers, held=True)
    problems = [r for r in reasons if r in MERGE_PROBLEMS]
    if problems:
        return _wait(DECISION, detail=problems, held=True)
    if all(MERGE_PENDING.fullmatch(r) for r in reasons):
        return None
    return yes()


def settled(reason):
    """A settled PR left to the user waits for their yes, unless a check or a blocker it records holds it."""
    holds = reason.split(REASONS_APART)[1:]
    return from_reasons(holds) if holds else yes()


WORDS = {
    "en": {
        "on": "Waits on: {}.", "where": "Where: {}", "step": "Next step: {}.",
        YES: "your yes", DECISION: "your decision", UNKNOWN: "a reason Kandu does not know",
        "check": "the failing check {}", "checks": "a failing check on the PR", "blocker": "blocker {}, not merged yet",
        "decision_on": "your decision, as {}",
        "step_yes": "approve it on the dashboard", "step_ask": "ask Didban about it",
        "step_log": "open the {} log", "step_check": "open the {} check on the PR",
        "step_checks": "open the failing checks on the PR", "step_blocker": "approve {} first",
        "held": "Your yes counts once that is cleared.",
    },
    "fa": {
        "on": "منتظر: {}.", "where": "کجا: {}", "step": "قدم بعدی: {}.",
        YES: "تأیید تو", DECISION: "تصمیم تو", UNKNOWN: "دلیلی که کندو نمی‌داند",
        "check": "بررسی ناموفق {}", "checks": "یک بررسی ناموفق روی PR", "blocker": "پیش‌نیاز {} که هنوز ادغام نشده",
        "decision_on": "تصمیم تو، چون {}",
        "step_yes": "در داشبورد تأییدش کن", "step_ask": "از دیدبان درباره‌اش بپرس",
        "step_log": "لاگ {} را باز کن", "step_check": "بررسی {} را روی PR باز کن",
        "step_checks": "بررسی‌های ناموفق روی PR را باز کن", "step_blocker": "اول {} را تأیید کن",
        "held": "تأیید تو وقتی حساب می‌شود که این برطرف شود.",
    },
}


def lines(wait, language, step=True):
    """The wait in plain words: what it waits on, where the problem is, and the next step. `step` False leaves the
    step out, when an agent's note already says it."""
    words = WORDS.get(language, WORDS["en"])
    on, where = wait["on"], []
    first = wait["checks"][0] if wait["checks"] else None
    if on == CHECK:
        what = words["check"].format(", ".join(c["name"] for c in wait["checks"])) if first else words["checks"]
        where = [f"{c['name']}: {c['url']}" for c in wait["checks"] if c["url"]]
        todo = (words["step_log"] if first and first["url"] else words["step_check"]).format(first["name"]) \
            if first else words["step_checks"]
    elif on == BLOCKER:
        what = words["blocker"].format(", ".join(wait["blockers"]))
        where = ["https://github.com/{}/issues/{}".format(*b.split("#")) for b in wait["blockers"]]
        todo = words["step_blocker"].format(wait["blockers"][0])
    elif on == DECISION and wait["detail"]:
        what, todo = words["decision_on"].format("; ".join(wait["detail"])), words["step_ask"]
    else:
        what, todo = words[on], words["step_yes"] if on == YES else words["step_ask"]
    said = [words["on"].format(what), *[words["where"].format(w) for w in where]]
    if step:
        said.append(words["step"].format(todo))
    return said + ([words["held"]] if wait["held"] else [])
