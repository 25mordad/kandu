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


class QuestionError(Exception):
    pass


def question_id(question):
    """Stays the same while other questions are answered, unlike a place in the list."""
    return hashlib.sha256(question.encode("utf-8")).hexdigest()[:6]


def open_questions(store):
    """Returns the unanswered questions of each task's newest review, oldest task first.

    An answer holds for the head it was given on: the next reviewer reads it in the
    memory, so a question on a new head is a new one. A task that ended asks nothing.
    """
    events = all_events(store)
    reviews = {e["task"]: e for e in events if e["type"] == "review_finished"}
    finished = {e["task"]: e for e in events if e["type"] == "task_finished"}
    answered = {(e["head_sha"], e["question"]) for e in events if e["type"] == "question_answered"}
    settled = {task for task, e in finished.items() if task in reviews and e["ts"] >= reviews[task]["ts"]}
    settled |= {e["task"] for e in events if e["type"] == "user_approved"
                and e["task"] in reviews and e["head_sha"] == reviews[e["task"]]["head_sha"]}
    return [{"task": task, "project": review["project"], "head_sha": review["head_sha"], "question": question,
             "id": question_id(question)}
            for task, review in reviews.items() if task not in settled
            for question in review["wait_for_user"] if (review["head_sha"], question) not in answered]


def answer(store, task, question_key, text, local_dir):
    """`question_key` is the id `list` prints."""
    question = next((q for q in open_questions(store) if q["task"] == task and q["id"] == question_key), None)
    if question is None:
        raise QuestionError(f"{task} has no open question {question_key}")
    if not text.strip():
        raise QuestionError("an answer needs text")
    store.record("question_answered", task=task, project=question["project"], head_sha=question["head_sha"],
                 question=question["question"], answer=text)
    memory = Path(local_dir) / "memory" / f"{question['project']}.md"
    memory.parent.mkdir(parents=True, exist_ok=True)
    with open(memory, "a", encoding="utf-8") as file:
        file.write(f"\n- Decided on {task}: {question['question']} Answer: {text}\n")


def main():
    parser = argparse.ArgumentParser(description="See and answer what reviews left for the user.")
    parser.add_argument("command", choices=("list", "answer"))
    parser.add_argument("task", nargs="?", help="owner/repo#number")
    parser.add_argument("id", nargs="?", help="the question's id, from `list`")
    parser.add_argument("text", nargs="?", help="the user's answer")
    parser.add_argument("--local", default=DEFAULT_LOCAL_DIR, help="config directory (default: local/)")
    args = parser.parse_args()
    store = StateStore()
    if args.command == "list":
        for question in open_questions(store):
            print(f"{question['task']} {question['id']}: {question['question']}")
        return 0
    if None in (args.task, args.id, args.text):
        parser.error("answer needs a task, a question id and the answer")
    try:
        answer(store, args.task, args.id, args.text, args.local)
    except QuestionError as e:
        print(f"questions: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
