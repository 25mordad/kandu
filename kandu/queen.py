"""The queen: the LLM that runs the cycle for every project (DESIGN §5).

She decides what starts and judges what stopped. Code decides when she wakes,
so a quiet cycle costs no tokens, and code checks each decision before it
happens. She can release a task from Backlog, send a stopped task back or ask
the user. She cannot merge, deploy or pass a lock.
"""
RETRYABLE = ("failed", "blocked")
LARGE = "Large"
NEEDS_A_LOOK = RETRYABLE + ("deploy_failed", "live_check_failed", "pr_closed", "sizing_failed")
MOVED_ON = ("user_approved", "task_picked", "task_finished")
PROMPT_EVENTS = ("task_finished", "decision", "project_checked", "review_finished", "deployed", "run_finished")


def all_events(store):
    return [event for day in store.days() for event in store.events(day)]


def since_last_look(store):
    """Returns (events up to her last look, events after it), oldest first."""
    events = all_events(store)
    last = max((i for i, e in enumerate(events) if e["type"] == "queen_finished"), default=-1)
    return events[:last + 1], events[last + 1:]


def _is_news(event, before):
    if event["type"] == "task_finished":
        return event["outcome"] in NEEDS_A_LOOK
    if event["type"] == "project_checked":
        return not event["passed"]
    if event["type"] == "decision" and event["decision"] == "merge_waits":
        # A task keeps waiting for the same reason each cycle. She judges that once.
        return not any(e["type"] == "decision" and e["decision"] == "merge_waits" and e["task"] == event["task"]
                       and e["reason"] == event["reason"] for e in before)
    return False


def worth_a_look(before, news, backlog, today, paused_repos=()):
    """`backlog` holds the Backlog tasks nothing blocks. She sees the same list only once a day.

    Pausing a project takes its tasks off the list, which is no reason to wake her.
    """
    if any(_is_news(event, before) for event in news):
        return True
    if not backlog:
        return False
    looks = [e for e in before if e["type"] == "queen_finished"]
    seen = [key for key in looks[-1]["backlog"] if key.partition("#")[0] not in paused_repos] if looks else []
    return (any(e["type"] == "task_finished" for e in news) or not looks
            or sorted(seen) != sorted(backlog) or not looks[-1]["ts"].startswith(today))


def prompt_events(news):
    return [event for event in news if event["type"] in PROMPT_EVENTS and event.get("role") != "queen"]


def retries_used(events, task):
    return sum(1 for e in events if e["type"] == "queen_decided" and e["task"] == task and e["action"] == "retry")


def note_for(events, task):
    """Her newest note for the task's next worker, or None."""
    notes = [e["note"] for e in events if e["type"] == "queen_decided" and e["task"] == task
             and e["action"] in ("release", "retry")]
    return notes[-1] if notes else None


def why_no_release(item, startable, spent, limit, sized, group):
    """Returns why this task may not leave Backlog, or None.

    `sized` is its `task_sized` event: the local log counts, as a worker can edit a board field.
    `group` is an unfinished group that comes before this task.
    """
    if item.status != "Backlog":
        return "it is not in Backlog"
    if sized is None:
        return "the splitter has not sized it"
    if LARGE in (item.size, sized["size"]):
        return "it is Large and must be split first"
    if group is not None:
        return f"the group of {group} comes first"
    if not startable:
        return "another task blocks it"
    if spent >= limit:
        return "its project reached its daily token limit"
    return None


def why_no_retry(events, task, limit, blocked, merged, sized=None):
    """Returns why this task may not go back, or None. A task that reached a worker before sizing existed has no `sized`."""
    if not blocked:
        return "it is not Blocked"
    if merged:
        return "its PR is already merged"
    if sized is not None and sized["size"] == LARGE:
        return "it is Large and must be split first"
    worked = any(e["type"] == "run_finished" and e["task"] == task and e["role"] != "splitter" for e in events)
    if sized is None and not worked:
        return "the splitter has not sized it"
    finished = [e for e in events if e["type"] == "task_finished" and e["task"] == task]
    if not finished or finished[-1]["outcome"] not in RETRYABLE:
        return "its last outcome is not one a retry can change"
    if retries_used(events, task) >= limit:
        return f"its {limit} retries are used up"
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
