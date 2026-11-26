"""The merge gate (DESIGN §5). Every condition is checked here, in code, on the reviewed `SHA`."""

FAILED = ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE")
RUNNING = ("PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "EXPECTED")


def ci_state(rollup):
    """Reads `statusCheckRollup`, where a check run has `conclusion` and `status`, a commit status `state`."""
    results = [c.get("conclusion") or c.get("state") or c.get("status") for c in rollup or []]
    if any(r in FAILED for r in results):
        return "failing"
    return "running" if any(r in RUNNING for r in results) else "ok"


def _thread_reasons(threads):
    reasons = []
    if any(not t["resolved"] for t in threads):
        reasons.append("a review thread is open")
    if any(t["resolved_by_other"] for t in threads):
        reasons.append("a thread was resolved by someone other than its author")
    return reasons


def merge_waits(project, pr, reviewed, threads, unreviewed_companions, approved=False, open_questions=()):
    """Returns why this head may not merge alone. `reviewed` comes from the local log, never from a comment.

    `approved` is the user's own yes to this head. It answers the review's questions and the door, nothing else.
    `open_questions` are those an earlier head left unanswered.
    """
    if reviewed is None:
        return ["this head has no review in the local log"]
    reasons = [] if approved else list(dict.fromkeys([*reviewed["wait_for_user"], *open_questions]))
    if reviewed["blockers"]:
        reasons.append("a blocker is open")
    if not reviewed["criteria_met"]:
        reasons.append("an acceptance criterion is not met")
    if reviewed["door"] != "two-way" and not approved:
        reasons.append("this is a one-way door")
    if pr["isDraft"]:
        reasons.append("the PR is a draft")
    if pr["mergeable"] == "CONFLICTING":
        reasons.append("the PR has conflicts")
    ci = ci_state(pr["statusCheckRollup"])
    if ci != "ok":
        reasons.append(f"CI is {ci}")
    if project.frozen:
        reasons.append("the project is frozen")
    reasons += [f"companion {name} is open and unreviewed" for name in unreviewed_companions]
    return reasons + _thread_reasons(threads)


WRONG_BASE = "the PR's base is not the project's base branch"  # no yes merges it: Kandu merges into no other branch


def base_waits(pr, base):
    """Why a PR may not merge whatever says yes: it would merge into another branch than `base`, the project's base
    branch. A PR read without its base counts as based elsewhere."""
    return [] if pr.get("baseRefName") == base else [WRONG_BASE]


TESTS_FAIL = "the tests fail on this head"
TESTS_WAIT = ("the tests failed twice on this head: they run again on a new commit or a recorded "
              "\"test again\" request")
TEST_TRIES = 2  # the first run and one more


def tests_wait(events, key, head):
    """True when this head's tests failed `TEST_TRIES` times in the merge gate since the last "test again" request.

    A new commit is a new head, so its count starts at none. `events` are the log's events, oldest first.
    """
    failed = 0
    for event in events:
        if event.get("task") != key or event.get("head_sha") != head:
            continue
        if event["type"] == "tests_again_requested":
            failed = 0
        elif event["type"] == "tests_failed":
            failed += 1
    return failed >= TEST_TRIES
