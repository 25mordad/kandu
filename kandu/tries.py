"""Kandu's own error on a task: the next cycle tries again by itself, `INTERNAL_TRIES` times in a row at most, then
the task stops for the queen to look at. The user may stop the tries sooner, from the task's card.

The supervisor and the dashboard both count the tries here, so the card says what the supervisor will do.
"""
INTERNAL_TRIES = 3
PROGRESS = ("review_finished", "task_finished", "task_sized")  # a task that moved on starts its count again
FAILURE_COMMENT = "Kandu could not finish this step and stopped here. The details are in its local log."
SIZING_FAILURE_COMMENT = ("Kandu could not size this task. Move it back to Backlog to try again. "
                          "The details are in its local log.")
STOPPED_BY_USER = "the user stopped Kandu's tries"
UNREACHABLE = "kandu.runner.Unreachable"  # a machine that dropped the connection: the work was not tried


def is_try(event):
    """Kandu's own error on a step; a machine that dropped the connection is no try, as the step never ran."""
    return (event["type"] == "decision" and event["decision"] == "internal_error"
            and not event.get("reason", "").startswith(UNREACHABLE))


def count(events, key):
    """Kandu's own errors on this task since it last moved on."""
    tries = 0
    for event in events:
        if event.get("task") != key:
            continue
        if event["type"] in PROGRESS:
            tries = 0
        elif is_try(event):
            tries += 1
    return tries


def last_error(events, key):
    return next((e["reason"] for e in reversed(events) if e.get("task") == key and e["type"] == "decision"
                 and e["decision"] == "internal_error"), "")


def stop(github, store, project, item, key, events):
    """Stops the tries as if the last one had failed. The caller has checked that it may.

    A task never sized stops as a sizing that failed, in Needs info; any other in Blocked. Either way the queen sees
    it in her next look. Returns the column it went to.
    """
    reason = f"{STOPPED_BY_USER}: {last_error(events, key)}"
    sized = any(e.get("task") == key and e["type"] == "task_sized" for e in events)
    decision, comment, column = (("failed", FAILURE_COMMENT, "Blocked") if sized
                                 else ("sizing_failed", SIZING_FAILURE_COMMENT, "Needs info"))
    store.record("decision", task=key, project=project.name, decision=decision, reason=reason)
    github.comment_issue(item.repo, item.number, comment)
    github.set_status(project.board, item.url, column)
    store.record("task_finished", task=key, project=project.name, outcome=decision)
    return column
