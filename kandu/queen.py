"""The queen: the LLM that oversees the workers' results (DESIGN §5).

She judges what the code loop cannot, but code decides when she wakes, so a
quiet cycle costs no tokens, and code checks each decision before it happens.
She can send a stopped task back or ask the user. She cannot merge, deploy or
pass a lock.
"""
RETRYABLE = ("failed", "blocked")
NEEDS_A_LOOK = RETRYABLE + ("deploy_failed", "live_check_failed", "pr_closed")
PROMPT_EVENTS = ("task_finished", "decision", "project_checked", "review_finished", "deployed", "run_finished")


def _all_events(store):
    return [event for day in store.days() for event in store.events(day)]


def since_last_look(store):
    """Returns (events up to her last look, events after it), oldest first."""
    events = _all_events(store)
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


def worth_a_look(before, news):
    return any(_is_news(event, before) for event in news)


def prompt_events(news):
    return [event for event in news if event["type"] in PROMPT_EVENTS and event.get("role") != "queen"]


def retries_used(events, task):
    return sum(1 for e in events if e["type"] == "queen_decided" and e["task"] == task and e["action"] == "retry")


def why_no_retry(events, task, limit, blocked, merged):
    """Returns why this task may not go back, or None."""
    if not blocked:
        return "it is not Blocked"
    if merged:
        return "its PR is already merged"
    finished = [e for e in events if e["type"] == "task_finished" and e["task"] == task]
    if not finished or finished[-1]["outcome"] not in RETRYABLE:
        return "its last outcome is not one a retry can change"
    if retries_used(events, task) >= limit:
        return f"its {limit} retries are used up"
    return None


def last_report(store):
    """Her newest brief and the questions from that look, or None."""
    events = _all_events(store)
    looks = [i for i, e in enumerate(events) if e["type"] == "queen_finished"]
    if not looks:
        return None
    start = looks[-2] + 1 if len(looks) > 1 else 0
    questions = [{"task": e["task"], "question": e["note"]} for e in events[start:looks[-1]]
                 if e["type"] == "queen_decided" and e["action"] == "ask_user"]
    return {"ts": events[looks[-1]]["ts"], "brief": events[looks[-1]]["brief"], "questions": questions}
