"""What a review left for the user to decide, answered through Didban (DESIGN §13).

A review can meet what the task never settled. The question stays open until the
user answers, and the answer goes into the project's memory, so the next worker
and reviewer know it. An answer is not the merge yes: that stays on the dashboard.
"""
import argparse
import hashlib
import sys
from pathlib import Path

from .config import DEFAULT_LOCAL_DIR
from .queen import all_events
from .state import StateStore


# The rest can come back, as the queen or the user sends the task on, and the merge still waits on its questions.
ENDED = ("merged", "pr_closed", "stopped_by_user", "deploy_failed", "live_check_failed")


class QuestionError(Exception):
    pass


def question_id(question):
    """Stays the same while other questions are answered, unlike a place in the list."""
    return hashlib.sha256(question.encode("utf-8")).hexdigest()[:6]


def open_questions(store):
    """Returns the unanswered questions of each task, oldest task first.

    A question stays open across heads, even if a later review does not repeat it.
    An answer holds for the head it was given on: the next reviewer reads it in the
    memory, so the same question on a new head is a new one. The user's yes settles
    all that is open then, and a task that ended asks nothing.
    """
    events = all_events(store)
    answered = {(e["head_sha"], e["question"]) for e in events if e["type"] == "question_answered"}
    approved = {(e["task"], e["head_sha"]) for e in events if e["type"] == "user_approved"}
    waiting = {}
    for event in events:
        task = event.get("task")
        if event["type"] == "review_finished" and (task, event["head_sha"]) not in approved:
            for question in event["wait_for_user"]:
                if (event["head_sha"], question) not in answered:
                    waiting.setdefault(task, {})[question] = event
        elif event["type"] == "question_answered":
            waiting.get(task, {}).pop(event["question"], None)
        elif event["type"] == "user_approved" or (event["type"] == "task_finished" and event["outcome"] in ENDED):
            waiting.pop(task, None)
    return [{"task": task, "project": review["project"], "head_sha": review["head_sha"], "question": question,
             "id": question_id(question)}
            for task, asked in waiting.items() for question, review in asked.items()]


def answer(store, task, question_key, text, local_dir):
    """`question_key` is the id `list` prints."""
    question = next((q for q in open_questions(store) if q["task"] == task and q["id"] == question_key), None)
    if question is None:
        raise QuestionError(f"{task} has no open question {question_key}")
    if not text.strip():
        raise QuestionError("an answer needs text")
    text = " ".join(text.split())
    # The memory comes first: the event closes the question, and a failed write must leave it open.
    memory = Path(local_dir) / "memory" / f"{question['project']}.md"
    memory.parent.mkdir(parents=True, exist_ok=True)
    with open(memory, "a", encoding="utf-8") as file:
        file.write(f"\n- Decided on {task}: {' '.join(question['question'].split())} Answer: {text}\n")
    store.record("question_answered", task=task, project=question["project"], head_sha=question["head_sha"],
                 question=question["question"], answer=text)


def main():
    parser = argparse.ArgumentParser(description="See and answer what reviews left for the user.")
    parser.add_argument("command", choices=("list", "answer"))
    parser.add_argument("task", nargs="?", help="owner/repo#number")
    parser.add_argument("id", nargs="?", help="the question's id, from `list`")
    parser.add_argument("--local", default=DEFAULT_LOCAL_DIR, help="config directory (default: local/)")
    args = parser.parse_args()
    store = StateStore()
    if args.command == "list":
        for question in open_questions(store):
            print(f"{question['task']} {question['id']}: {' '.join(question['question'].split())}")
        return 0
    if None in (args.task, args.id):
        parser.error("answer needs a task and a question id; the answer comes on stdin")
    try:
        answer(store, args.task, args.id, sys.stdin.read(), args.local)  # On stdin, out of reach of a shell.
    except QuestionError as e:
        print(f"questions: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
