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

    def test_a_question_stays_open_when_a_new_head_does_not_repeat_it(self):
        self.review("Old?")
        self.review("New?", head="def", round=2)
        self.review(head="ghi", round=3)
        self.assertEqual(self.asked(), ["Old?", "New?"])

    def test_a_task_that_can_come_back_keeps_its_questions(self):
        self.review("Keep it?")
        for outcome in ("failed", "blocked", "needs_info"):
            self.store.record("task_finished", task=TASK, project="website", outcome=outcome)
            self.assertEqual(self.asked(), ["Keep it?"])

    def test_a_failed_memory_write_leaves_the_question_open(self):
        self.review("Keep it?")
        with mock.patch("builtins.open", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.answer("Keep it?", "Yes.")
        self.assertEqual(self.asked(), ["Keep it?"])

    def test_a_question_on_several_lines_stays_one_line(self):
        self.review("Keep\nit?")
        self.assertIn("Keep it?\n", self.run_cli("list")[1])
        self.answer("Keep\nit?", "Yes,\nkeep it.\n")
        memory = (self.local / "memory" / "website.md").read_text(encoding="utf-8")
        self.assertIn("Keep it? Answer: Yes, keep it.\n", memory)

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

    def run_cli(self, *args, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.argv", ["questions", *args, "--local", str(self.local)]), \
                mock.patch("sys.stdin", io.StringIO(stdin)), \
                mock.patch.object(questions, "StateStore", return_value=self.store), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = questions.main()
        return code, out.getvalue(), err.getvalue()

    def test_the_command_lists_ids_and_answers_by_them(self):
        self.review("Keep it?", "Move it?")
        keep, move = questions.question_id("Keep it?"), questions.question_id("Move it?")
        self.assertEqual(self.run_cli("list"), (0, f"{TASK} {keep}: Keep it?\n{TASK} {move}: Move it?\n", ""))
        self.assertEqual(self.run_cli("answer", TASK, move, stdin="Under $HOME/bin\n")[0], 0)
        self.assertEqual(self.asked(), ["Keep it?"])
        self.assertEqual(self.store.last_event("question_answered", task=TASK)["answer"], "Under $HOME/bin")
        code, _, err = self.run_cli("answer", TASK, "nope", stdin="Yes.")
        self.assertEqual(code, 1)
        self.assertIn("no open question", err)


class DidbanOpensWithThemTest(unittest.TestCase):
    def test_they_reach_the_session(self):
        result = mock.Mock(returncode=0, stdout=f"{TASK} 1: Keep it?\n", stderr="")
        brain = mock.Mock(reach="ssh host", install_dir="~/kandu")
        with mock.patch("kandu.didban.subprocess.run", return_value=result) as run:
            self.assertEqual(didban.waiting_questions(brain), f"{TASK} 1: Keep it?")
        self.assertIn("kandu.questions list", run.call_args.args[0][-1])

    def test_the_prompt_says_who_wrote_them(self):
        session = mock.Mock(brain=mock.Mock(reach="ssh host", install_dir="~/kandu"))
        self.assertIn("written by an agent: Keep it?", didban.system_prompt(session, "ok", questions="Keep it?"))
