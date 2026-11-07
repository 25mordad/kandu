"""The queen: the LLM that runs the cycle for every project (DESIGN §5).

She decides what starts and sees every stopped task through, as the user would.
Code decides when she wakes, so a quiet cycle costs no tokens, and code checks
each decision before it happens. She can release a task from Backlog, send a
stopped task back, send a worker back to a PR with her own order, have a
checker find something out, open a new task, make a merge wait for another,
answer a merge's questions for one head, park a note, or ask the user. She
cannot merge, deploy or pass a lock: the merge gate still decides.

The loop guard is in code: each of her steps on one task has a limit. A step
past it stops the task in `Blocked` and tells the user, and she takes no step on
that task again until the user has acted on it.
"""
from .github import BLOCKED_BY, HTML_COMMENT

RETRYABLE = ("failed", "blocked", "needs_info", "sizing_failed")
LIMITED = ("retry", "rework", "verify", "new_task", "block_on")  # each one this often per task
SENT_ON = ("rework", "verify")  # code carries these out when it next drives the task
LARGE = "Large"
HIGH = "High"
NEEDS_A_LOOK = RETRYABLE + ("deploy_failed", "live_check_failed", "pr_closed")
MOVED_ON = ("user_approved", "queen_approved", "task_picked", "task_finished", "user_resumed")
USER_ACTED = ("task_picked", "user_approved", "question_answered", "user_resumed")  # after a loop stop, only the user moves it
APPROVALS = ("user_approved", "queen_approved")
COULD_NOT_FINISH = "The queen could not finish this look"
PROMPT_EVENTS = ("task_finished", "decision", "project_checked", "review_finished", "deployed", "run_finished",
                 "queen_step_done")


def all_events(store):
    return [event for day in store.days() for event in store.events(day)]


def since_last_look(store):
    """Returns (events up to her last look, events after it), oldest first."""
    events = all_events(store)
    last = max((i for i, e in enumerate(events) if e["type"] == "queen_finished"), default=-1)
    return events[:last + 1], events[last + 1:]


def _is_news(event, before):
    if event["type"] in ("priority_raised", "queen_step_done", "wake_requested"):
        return True
    if event["type"] == "task_finished":
        return event["outcome"] in NEEDS_A_LOOK
    if event["type"] == "project_checked":
        return not event["passed"]
    if event["type"] == "decision" and event["decision"] == "merge_waits":
        # A task keeps waiting for the same reason each cycle. She judges that once.
        return not any(e["type"] == "decision" and e["decision"] == "merge_waits" and e["task"] == event["task"]
                       and e["reason"] == event["reason"] for e in before)
    return False


def unparked(events):
    """Returns each note the splitter reported that has no task in `Technical debt` yet."""
    parked = {(e["task"], e["finding"]) for e in events if e["type"] == "debt_parked"}
    return [e for e in events if e["type"] == "debt_reported" and (e["task"], e["finding"]) not in parked]


def worth_a_look(before, news, backlog, today, paused_repos=()):
    """`backlog` holds the Backlog tasks nothing blocks. She sees the same list only once a day.

    Pausing a project takes its tasks off the list, which is no reason to wake her.
    A note she left unparked also gets one look a day.
    """
    if any(_is_news(event, before) for event in news):
        return True
    looks = [e for e in before if e["type"] == "queen_finished"]
    debts = [e for e in unparked(before + news) if e["task"].partition("#")[0] not in paused_repos]
    if debts and (any(e["type"] == "debt_reported" for e in news) or not looks
                  or not looks[-1]["ts"].startswith(today)):
        return True
    if not backlog:
        return False
    seen = [key for key in looks[-1]["backlog"] if key.partition("#")[0] not in paused_repos] if looks else []
    return (any(e["type"] == "task_finished" for e in news) or not looks
            or sorted(seen) != sorted(backlog) or not looks[-1]["ts"].startswith(today))


def prompt_events(news):
    # Her own runs and Bazras's belong to no task she judges.
    return [event for event in news if event["type"] in PROMPT_EVENTS and event.get("role") not in ("queen", "bazras")]


def _since_loop_stop(events, task):
    stops = [i for i, e in enumerate(events) if e["type"] == "loop_stopped" and e.get("task") == task]
    return events[stops[-1] + 1:] if stops else events


def waits_after_loop(events, task):
    """True when code stopped this task for a loop and the user has not acted on it since."""
    if not any(e["type"] == "loop_stopped" and e.get("task") == task for e in events):
        return False
    return not any(e["type"] in USER_ACTED and e.get("task") == task for e in _since_loop_stop(events, task))


def steps_used(events, task, action):
    """Her steps of this kind on the task, since code last stopped it for a loop."""
    return sum(1 for e in _since_loop_stop(events, task)
               if e["type"] == "queen_decided" and e["task"] == task and e["action"] == action)


def retries_used(events, task):
    return steps_used(events, task, "retry")


def steps_on(events, tasks):
    """What she already did on each task, so she can see herself going round."""
    return {task: [{"action": e["action"], "note": e["note"]} for e in _since_loop_stop(events, task)
                   if e["type"] == "queen_decided" and e["task"] == task and e["action"] in LIMITED]
            for task in tasks}


def pending_step(events, task):
    """Her newest rework or verify on the task that code has not carried out yet, or None."""
    for event in reversed(events):
        if event.get("task") != task:
            continue
        if event["type"] == "queen_step_done":
            return None
        if event["type"] == "queen_decided" and event["action"] in SENT_ON:
            return event
    return None


def with_blocker(body, repo, key):
    """The issue's body with `key` added under `## Blocked by`."""
    other, number = key.split("#")
    reference = f"#{number}" if other == repo else key
    section = BLOCKED_BY.search(body or "")
    if section is None:
        return f"{(body or '').rstrip()}\n\n## Blocked by\n{reference}\n"
    lines = [line for line in HTML_COMMENT.sub("", section.group(1)).strip().splitlines()
             if line.strip() and line.strip().lower() != "none"]
    text = "\n".join([*lines, reference])
    return BLOCKED_BY.sub(lambda _: f"## Blocked by\n{text}\n\n", body, count=1)


def note_for(events, task):
    """Her newest note for the task's next worker, or None."""
    notes = [e["note"] for e in events if e["type"] == "queen_decided" and e["task"] == task
             and e["action"] in ("release", "retry")]
    return notes[-1] if notes else None


def why_no_release(item, startable, spent, limit, sized, first=(), held=None):
    """Returns why this task may not leave Backlog, or None.

    `sized` is its `task_sized` event: the local log counts, as a worker can edit a board field.
    `first` holds the tasks of its project that go before any other. `held` is its `task_held` event, if any.
    """
    if item.status != "Backlog":
        return "it is not in Backlog"
    if sized is None:
        return "the splitter has not sized it"
    if held is not None:
        return "it needs a forbidden area and waits for the user"
    if LARGE in (item.size, sized["size"]):
        return "it is Large and must be split first"
    if not startable:
        return "it is closed"
    if first and f"{item.repo}#{item.number}" not in first:
        return f"{first[0]} goes first"
    if spent >= limit:
        return "its project reached its daily token limit"
    return None


def why_no_approval(pr, reviewed, head_sha):
    """Returns why she may not answer this head's questions, or None. `reviewed` is the head's review in the local log."""
    if pr is None or pr["state"] != "OPEN":
        return "it has no open PR"
    if pr["headRefOid"] != head_sha:
        return "its PR has a newer head than the one she judged"
    if reviewed is None:
        return "this head has no review"
    if reviewed["blockers"]:
        return "a blocker is open"
    if not reviewed["criteria_met"]:
        return "an acceptance criterion is not met"
    return None


def why_no_retry(events, task, limit, blocked, merged, sized=None):
    """Returns why this task may not go back, or None. A task that reached a worker before sizing existed has no `sized`.

    `blocked` is true in `Blocked` or `Needs info`. A task that could not be sized goes back to be sized again.
    """
    if not blocked:
        return "it is not Blocked"
    if merged:
        return "its PR is already merged"
    finished = [e for e in events if e["type"] == "task_finished" and e["task"] == task]
    if not finished or finished[-1]["outcome"] not in RETRYABLE:
        return "its last outcome is not one a retry can change"
    if sized is not None and sized["size"] == LARGE:
        return "it is Large and must be split first"
    worked = any(e["type"] == "run_finished" and e["task"] == task and e["role"] != "splitter" for e in events)
    if sized is None and not worked and finished[-1]["outcome"] != "sizing_failed":
        return "the splitter has not sized it"
    if retries_used(events, task) >= limit:
        return f"its {limit} retries are used up"
    return None


def last_outcome(events, task):
    return next((e["outcome"] for e in reversed(events) if e["type"] == "task_finished" and e["task"] == task), None)


def why_no_step(events, task, action, limit):
    """The loop guard, for every step that has a limit. Returns why not, or None."""
    if waits_after_loop(events, task):
        return "it was stopped for going round, and waits for the user"
    if action in LIMITED and steps_used(events, task, action) >= limit:
        return f"its {limit} {'retries' if action == 'retry' else action + ' steps'} are used up"
    return None


def why_no_rework(pr, merged):
    if merged:
        return "its PR is already merged"
    if pr is None or pr["state"] != "OPEN":
        return "it has no open PR: use retry"
    return None


def last_report(store):
    """Her newest brief and the questions from that look that still wait, or None.

    A task the user or a cycle moved on since she asked has no question left.
    """
    events = all_events(store)
    looks = [i for i, e in enumerate(events) if e["type"] == "queen_finished"]
    if not looks:
        return None
    start = looks[-2] + 1 if len(looks) > 1 else 0
    moved_on = {e["task"] for e in events[looks[-1]:] if e["type"] in MOVED_ON}
    questions = [{"task": e["task"], "question": e["note"]} for e in events[start:looks[-1]]
                 if e["type"] == "queen_decided" and e["action"] == "ask_user" and e["task"] not in moved_on]
    run_id = events[looks[-1]]["run_id"]
    run = next((e for e in events if e["type"] == "run_finished" and e["run_id"] == run_id), None)
    usage = run["models"].values() if run else ()
    return {"ts": events[looks[-1]]["ts"], "brief": events[looks[-1]]["brief"], "questions": questions,
            "tokens": sum(count for model in usage for kind, count in model.items() if kind != "cache_read")}
