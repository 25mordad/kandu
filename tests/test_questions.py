import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kandu import didban, questions
from kandu.state import StateStore

TASK = "acme-org/website#1"


class QuestionsTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.local = Path(folder.name) / "local"
        self.store = StateStore(Path(folder.name) / "state")

    def review(self, *asked, head="abc", round=1, task=TASK):
        self.store.record("review_finished", task=task, project="website", run_id="r", round=round, head_sha=head,
                          findings=0, blockers=0, door="two-way", criteria_met=True, wait_for_user=list(asked),
                          user_brief=None)

    def asked(self):
        return [q["question"] for q in questions.open_questions(self.store)]

    def answer(self, question, text):
        questions.answer(self.store, TASK, questions.question_id(question), text, self.local)

    def test_only_the_newest_review_counts(self):
        self.review("Old?")
        self.review("New?", round=2)
        self.assertEqual(self.asked(), ["New?"])

    def test_an_answer_closes_it_and_goes_into_the_memory(self):
        self.review("Keep it?", "Move it?")
        self.answer("Move it?", "Move it to the cron.")
        self.assertEqual(self.asked(), ["Keep it?"])
        memory = (self.local / "memory" / "website.md").read_text(encoding="utf-8")
        self.assertIn("Move it? Answer: Move it to the cron.", memory)
        self.assertEqual(self.store.last_event("question_answered", task=TASK)["answer"], "Move it to the cron.")

    def test_answering_one_leaves_the_id_of_the_next(self):
        self.review("Keep it?", "Move it?", "Name it?")
        self.answer("Keep it?", "Yes.")
        self.answer("Move it?", "No.")
        self.assertEqual(self.asked(), ["Name it?"])

    def test_an_answer_holds_for_its_head(self):
        self.review("Keep it?")
        self.answer("Keep it?", "Yes.")
        self.review("Keep it?", round=2)
        self.assertEqual(self.asked(), [])
        self.review("Keep it?", head="def", round=3)
        self.assertEqual(self.asked(), ["Keep it?"])

    def test_a_task_that_ended_asks_nothing(self):
        self.review("Keep it?")
        self.store.record("task_finished", task=TASK, project="website", outcome="pr_closed")
        self.assertEqual(self.asked(), [])

    def test_a_merged_or_approved_task_asks_nothing(self):
        self.review("Keep it?")
        self.store.record("user_approved", task=TASK, project="website", head_sha="abc")
        self.assertEqual(self.asked(), [])
        self.review("Keep it?", head="def", round=2)
        self.assertEqual(self.asked(), ["Keep it?"])
        self.store.record("task_finished", task=TASK, project="website", outcome="merged")
        self.assertEqual(self.asked(), [])

    def test_a_wrong_number_or_empty_answer_changes_nothing(self):
        self.review("Keep it?")
        for key, text in (("nope", "Yes."), (questions.question_id("Keep it?"), "  ")):
            with self.assertRaises(questions.QuestionError):
                questions.answer(self.store, TASK, key, text, self.local)
        self.assertEqual(self.asked(), ["Keep it?"])
        self.assertFalse((self.local / "memory").exists())

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.argv", ["questions", *args, "--local", str(self.local)]), \
                mock.patch.object(questions, "StateStore", return_value=self.store), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = questions.main()
        return code, out.getvalue(), err.getvalue()

    def test_the_command_lists_ids_and_answers_by_them(self):
        self.review("Keep it?", "Move it?")
        keep, move = questions.question_id("Keep it?"), questions.question_id("Move it?")
        self.assertEqual(self.run_cli("list"), (0, f"{TASK} {keep}: Keep it?\n{TASK} {move}: Move it?\n", ""))
        self.assertEqual(self.run_cli("answer", TASK, move, "Yes.")[0], 0)
        self.assertEqual(self.asked(), ["Keep it?"])
        code, _, err = self.run_cli("answer", TASK, "nope", "Yes.")
        self.assertEqual(code, 1)
        self.assertIn("no open question", err)


class DidbanOpensWithThemTest(unittest.TestCase):
    def test_they_reach_the_session(self):
        result = mock.Mock(returncode=0, stdout=f"{TASK} 1: Keep it?\n", stderr="")
        brain = mock.Mock(reach="ssh host", install_dir="~/kandu")
        with mock.patch("kandu.didban.subprocess.run", return_value=result) as run:
            self.assertEqual(didban.waiting_questions(brain), f"{TASK} 1: Keep it?")
        self.assertIn("kandu.questions list", run.call_args.args[0][-1])
