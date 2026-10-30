"""What a review left for the user to decide, answered through Didban (DESIGN §13).

A review can meet what the task never settled. The question stays open until the
user answers, and the answer goes into the project's memory, so the next worker
and reviewer know it. An answer is not the merge yes: that stays on the dashboard.
"""
import argparse
import sys
from pathlib import Path

from .config import DEFAULT_LOCAL_DIR
from .queen import all_events
from .state import StateStore


class QuestionError(Exception):
    pass


def open_questions(store):
    """Returns the unanswered questions of each task's newest review, oldest task first."""
    events = all_events(store)
    reviews = {e["task"]: e for e in events if e["type"] == "review_finished"}
    answered = {(e["task"], e["question"]) for e in events if e["type"] == "question_answered"}
    settled = {e["task"] for e in events if e["type"] == "task_finished" and e["outcome"] == "merged"}
    settled |= {e["task"] for e in events if e["type"] == "user_approved"
                and e["task"] in reviews and e["head_sha"] == reviews[e["task"]]["head_sha"]}
    return [{"task": task, "project": review["project"], "question": question}
            for task, review in reviews.items() if task not in settled
            for question in review["wait_for_user"] if (task, question) not in answered]


def answer(store, task, number, text, local_dir):
    """`number` counts this task's open questions from 1, as `list` prints them."""
    waiting = [q for q in open_questions(store) if q["task"] == task]
    if not 1 <= number <= len(waiting):
        raise QuestionError(f"{task} has no open question {number}")
    if not text.strip():
        raise QuestionError("an answer needs text")
    question = waiting[number - 1]
    store.record("question_answered", task=task, project=question["project"], question=question["question"], answer=text)
    memory = Path(local_dir) / "memory" / f"{question['project']}.md"
    memory.parent.mkdir(parents=True, exist_ok=True)
    with open(memory, "a", encoding="utf-8") as file:
        file.write(f"\n- Decided on {task}: {question['question']} Answer: {text}\n")


def main():
    parser = argparse.ArgumentParser(description="See and answer what reviews left for the user.")
    parser.add_argument("command", choices=("list", "answer"))
    parser.add_argument("task", nargs="?", help="owner/repo#number")
    parser.add_argument("number", nargs="?", type=int, help="which question of this task, from 1")
    parser.add_argument("text", nargs="?", help="the user's answer")
    parser.add_argument("--local", default=DEFAULT_LOCAL_DIR, help="config directory (default: local/)")
    args = parser.parse_args()
    store = StateStore()
    if args.command == "list":
        counts = {}
        for question in open_questions(store):
            counts[question["task"]] = counts.get(question["task"], 0) + 1
            print(f"{question['task']} {counts[question['task']]}: {question['question']}")
        return 0
    if None in (args.task, args.number, args.text):
        parser.error("answer needs a task, a question number and the answer")
    try:
        answer(store, args.task, args.number, args.text, args.local)
    except QuestionError as e:
        print(f"questions: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
