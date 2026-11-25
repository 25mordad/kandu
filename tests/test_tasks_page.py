"""The Tasks page and the task sheet (#45): every task found by its title or number, in a group per project, the bees
at work, the queen's brief, and a task opened in a sheet with its story and its actions. The page's own functions run in
node on the state and queue the API gives."""
import json
import os
import re
import subprocess
import unittest

from tests import test_today as today
from tests.test_today import BLOG, FLOWERS, NODE, QUEUE, REPO, ROOT, SHOP, Page, columns, mark, state, status, task

NOW = "2026-11-15T15:00:00+00:00"
LINES = ("const FEW_PROJECTS = ", "const COLUMN_ORDER = ", "const PHASES = ", "const QUIET_STEPS = ", "const stateText = ",
         "const limitText = ", "const dayName = ")
FUNCTIONS = ("taskQuery", "taskMatches", "taskMarks", "taskRow", "taskGroups", "groupsHtml", "soFar", "beesAtWork",
             "beesHtml", "taskPhases", "stepOf", "sheetView", "sheetBody", "sheetFoot", "waitLines", "approval", "testsView", "sendBack",
             "splitAccept", "byHandBox", "heldLines", "keptLines", "flightControls", "roundBox", "limitBadge", "bazrasLine",
             "lookBadge", "doneBox")


@unittest.skipUnless(NODE, "needs node to run the page's script")
class TasksTest(unittest.TestCase, Page):
    def run_js(self, script, lang="en"):
        """Runs the page's own functions in node, with its texts, in `lang`."""
        prelude = (f"const TEXT = {json.dumps(self.text)};\nlet lang = {json.dumps(lang)};\n"
                   "let confirming = false, sendingBack = false, flights = {open: [], flown: []};\n"
                   + "".join(self.line(start) for start in today.LINES + LINES)
                   + "".join(self.function(name) for name in today.FUNCTIONS + FUNCTIONS))
        done = subprocess.run([NODE, "-"], input=prelude + f"\nconsole.log(JSON.stringify((() => {{ {script} }})()));",
                              capture_output=True, text=True, timeout=30, env={**os.environ, "TZ": "UTC"})
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def groups(self, query="", s=None, queue=QUEUE, folded=None, lang="en"):
        return self.run_js(f"return taskGroups({json.dumps(s or state())}, {json.dumps(queue)}, taskQuery({json.dumps(query)}), "
                           f"{json.dumps(folded or {})});", lang)

    # Search.

    def test_a_search_finds_tasks_by_title_across_all_projects(self):
        found = self.groups("dark")
        self.assertEqual([(g["name"], [r["task"] for r in g["rows"]]) for g in found], [("blog", [f"{BLOG}#1"])])
        found = self.groups("BY")  # any case, in every project
        tasks = sorted(r["task"] for g in found for r in g["rows"])
        self.assertEqual(tasks, sorted([f"{REPO}#2", f"{REPO}#3", f"{REPO}#14"]))

    def test_a_search_finds_a_task_by_its_number_with_or_without_the_hash_in_either_script(self):
        for query in ("#2", "2", "۲"):
            found = self.groups(query)
            self.assertEqual(sorted(r["task"] for g in found for r in g["rows"]), [f"{SHOP}#2", f"{REPO}#2"], query)
        self.assertEqual([r["task"] for g in self.groups("#14") for r in g["rows"]], [f"{REPO}#14"])

    def test_while_searching_every_group_with_a_match_is_open_and_the_others_are_left_out(self):
        found = self.groups("2", folded={"website": True, "shop": True})
        self.assertEqual([(g["name"], g["open"], g["count"]) for g in found], [("website", True, 1), ("shop", True, 1)])

    def test_a_search_that_matches_nothing_says_so(self):
        self.assertEqual(self.groups("no such thing"), [])
        html = self.run_js('return groupsHtml([], taskQuery("no such thing"), {});')
        self.assertIn("No task matches this search.", html)
        persian = self.run_js('return groupsHtml([], taskQuery("x"), {});', "fa")
        self.assertIn(self.text["fa"]["no_match"], persian)
        self.assertIn("Reading the boards…", self.run_js("return groupsHtml([], null, null);"))

    def test_typing_in_the_search_box_draws_the_groups_again(self):
        self.assertIn('$("search").oninput = renderProjects;', self.page)
        self.assertIn('taskQuery($("search").value)', self.function("renderProjects"))

    # Groups.

    def test_tasks_are_grouped_by_project_with_its_state_line_and_count_those_that_need_attention_first(self):
        found = self.groups()
        self.assertEqual([(g["name"], g["line"], g["tone"], g["count"]) for g in found],
                         [("website", "2 stuck", "red", 14), ("shop", "1 waits for you", "amber", 3), ("blog", "Paused", "muted", 1)])

    def test_inside_a_group_tasks_waiting_for_the_user_come_first_then_by_state(self):
        rows = self.groups()[0]["rows"]
        numbers = [int(r["task"].split("#")[1]) for r in rows]
        self.assertEqual(numbers[:3], [8, 1, 10])  # its yes, the queen's question, its split
        self.assertEqual(numbers[3:], [14, 3, 4, 5, 6, 7, 2, 9, 13, 11, 12])  # stuck, at work, waiting, blocked, debt, done
        self.assertEqual(rows[0]["label"], "Your approval")
        self.assertEqual(rows[1]["label"], "The queen asks")

    def test_a_group_can_be_folded_and_a_few_projects_all_start_open(self):
        self.assertEqual([g["open"] for g in self.groups()], [True, True, True])
        self.assertEqual([g["open"] for g in self.groups(folded={"website": True})], [False, True, True])
        html = self.run_js(f"return groupsHtml(taskGroups({json.dumps(state())}, {json.dumps(QUEUE)}, null, {{website: true}}), null, {{}});")
        self.assertRegex(html, r'<details class="group" data-project="website" data-open="false">')
        self.assertRegex(html, r'<details class="group" data-project="shop" data-open="true" open>')
        toggle = self.page.split('document.addEventListener("toggle"')[1].split("\n}, true);")[0]
        self.assertIn("folded[name] = !e.target.open;", toggle)
        self.assertIn("save(FOLDED_KEY, JSON.stringify(folded));", toggle)

    def folded_at_start(self, kept):
        """Runs the page's start-up reading of the folded projects, with `kept` saved in the browser."""
        start = self.page.index("try {\n  const kept = JSON.parse(saved(FOLDED_KEY))")
        block = self.page[start:self.page.index("} catch {}", start) + len("} catch {}")]
        script = (f"const store = {{'kandu-closed-projects': {json.dumps(kept)}}};\n"
                  "const localStorage = {getItem: key => key in store ? store[key] : null};\nlet folded = {};\n"
                  + self.line("const LANGUAGE_KEY = ") + self.line("const saved = ") + block
                  + "\nconsole.log(JSON.stringify(folded));")
        done = subprocess.run([NODE, "-"], input=script, capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_the_folded_projects_an_older_page_saved_stay_folded(self):
        self.assertEqual(self.folded_at_start(json.dumps(["website", "blog"])), {"website": True, "blog": True})
        self.assertEqual(self.folded_at_start(json.dumps({"website": True, "blog": False})), {"website": True, "blog": False})
        self.assertEqual(self.folded_at_start(None), {})
        self.assertEqual(self.folded_at_start("not json"), {})

    def test_with_many_projects_only_those_that_need_attention_start_open(self):
        many = dict(QUEUE["projects"])
        for name in ("garden-log", "invoice-bot", "recipe-box"):
            many[name] = columns(Ready=[task(f"acme-org/{name}", 1, "Something")])
        found = self.groups(queue={"done_days": 5, "projects": many})
        self.assertEqual({g["name"]: g["open"] for g in found},
                         {"website": True, "shop": True, "blog": False, "garden-log": False, "invoice-bot": False,
                          "recipe-box": False})
        chosen = self.groups(queue={"done_days": 5, "projects": many}, folded={"blog": False, "website": True})
        self.assertEqual({g["name"]: g["open"] for g in chosen}["blog"], True)  # the user's own choice stays
        self.assertEqual({g["name"]: g["open"] for g in chosen}["website"], False)

    def test_a_group_keeps_its_daily_token_limit_and_its_last_bazras_look(self):
        s = state(over_limit={"shop": {"today": 9000, "limit": 5000}},
                  bazras={"last": {"website": {"date": "2026-11-14", "all_well": True, "findings": 0}, "shop": None}})
        html = self.run_js(f"return groupsHtml(taskGroups({json.dumps(s)}, {json.dumps(QUEUE)}, null, {{}}), null, {{}});")
        groups = dict(re.findall(r'<details class="group" data-project="([^"]+)".*?</summary>(.*?)(?=<details|$)', html, re.DOTALL))
        summaries = dict(re.findall(r'data-project="([^"]+)"[^>]*><summary>(.*?)</summary>', html, re.DOTALL))
        self.assertIn("Daily token limit reached", summaries["shop"])
        self.assertNotIn("Daily token limit reached", summaries["website"])
        self.assertIn("All was well", groups["website"])
        self.assertIn('data-tab="bazras"', groups["shop"])  # watched, no look yet
        self.assertNotIn('data-tab="bazras"', groups["blog"])

    # A task's row.

    def test_a_row_shows_its_state_with_a_coloured_dot_its_number_title_and_its_phase_and_marks(self):
        flowers = FLOWERS | {f"{REPO}#3": mark(size="Medium", door="one-way", sent_back=True, questions=2, status=status("fine"))}
        rows = {r["task"]: r for r in self.groups(s=state(flowers=flowers))[0]["rows"]}
        self.assertEqual({k: rows[f"{REPO}#3"][k] for k in ("number", "title", "label", "tone", "dot", "meta")},
                         {"number": "⁦#3⁩", "title": "Search by tag", "label": "In progress", "tone": "ink", "dot": "bee",
                          "meta": "implement · Medium · one-way · sent back · 2 questions"})
        self.assertEqual((rows[f"{REPO}#8"]["dot"], rows[f"{REPO}#8"]["tone"]), ("you", "amber"))
        self.assertEqual((rows[f"{REPO}#9"]["dot"], rows[f"{REPO}#9"]["tone"], rows[f"{REPO}#9"]["label"]), ("stuck", "red", "Blocked"))
        self.assertEqual((rows[f"{REPO}#2"]["dot"], rows[f"{REPO}#2"]["label"]), ("turn", "Waiting for a machine"))
        self.assertEqual((rows[f"{REPO}#11"]["tone"], rows[f"{REPO}#11"]["label"]), ("muted", "Done"))
        html = self.run_js(f"return groupsHtml(taskGroups({json.dumps(state(flowers=flowers))}, {json.dumps(QUEUE)}, null, {{}}), null, {{}});")
        row = re.search(rf'<button class="task-row" data-review="{re.escape(REPO)}#3">.*?</button>', html, re.DOTALL).group(0)
        for part in ('<i class="pdot bee"></i>', "In progress", "#3", "Search by tag", "implement · Medium · one-way"):
            self.assertIn(part, row)

    def test_a_row_reads_in_persian(self):
        rows = self.groups(lang="fa")[0]["rows"]
        self.assertEqual(rows[0]["label"], self.text["values_fa"]["Your approval"])
        self.assertEqual(rows[1]["label"], self.text["fa"]["kind_queen"])
        self.assertNotRegex(rows[0]["number"], r"[0-9]")

    def test_the_dot_colours_are_the_designs_tokens(self):
        self.assertRegex(self.style, r"\.pdot\.ok \{[^}]*background:var\(--green\)")

    # Bees at work.

    def bees(self, s, lang="en"):
        return self.run_js(f"return beesAtWork({json.dumps(s)}, {json.dumps(QUEUE)}, new Date({json.dumps(NOW)}));", lang)

    def test_bees_at_work_lists_each_worker_with_its_task_project_phase_machine_and_time_so_far(self):
        workers = [{"task": f"{REPO}#3", "project": "website", "machine": "hive-box", "phase": "implement",
                    "started": "2026-11-15T14:48:00+00:00", "run_id": "r-1"},
                   {"task": f"{REPO}#14", "project": "website", "machine": "studio-mac", "phase": "fix round 1",
                    "started": "2026-11-15T13:35:00+00:00", "run_id": "r-2"}]
        view = self.bees(state(workers=workers))
        self.assertEqual(view["count"], "2 of 4")
        self.assertIsNone(view["none"])
        self.assertEqual([(r["title"], r["meta"], r["run"]) for r in view["rows"]],
                         [("Search by tag", "website · implement · hive-box · 12 min", "r-1"),
                          ("Sort by date", "website · fix round 1 · studio-mac · 1 h 25 min", "r-2")])
        html = self.run_js(f"return beesHtml(beesAtWork({json.dumps(state(workers=workers))}, {json.dumps(QUEUE)}, new Date({json.dumps(NOW)})));")
        self.assertIn("Bees at work", html)
        self.assertEqual(re.findall(r'data-stop="([^"]+)"', html), ["r-1", "r-2"])
        self.assertEqual(re.findall(r'data-review="([^"]+)"', html), [f"{REPO}#3", f"{REPO}#14"])
        clicks = self.page.split('document.addEventListener("click"')[1].split("\n});")[0]
        self.assertIn('if (stop) { act(stop, `/workers/${encodeURIComponent(stop.dataset.stop)}/stop`); return; }', clicks)

    def test_bees_at_work_reads_in_persian(self):
        view = self.bees(state(), "fa")
        self.assertNotRegex(view["count"] + view["rows"][0]["meta"], r"[0-9]")
        self.assertIn(self.text["values_fa"]["implement"], view["rows"][0]["meta"])

    def test_with_no_bee_it_says_why(self):
        asleep = self.bees(state(workers=[], supervisor="asleep"))
        self.assertEqual((asleep["rows"], asleep["none"]), ([], "No bees fly while Kandu sleeps."))
        limit = self.bees(state(workers=[], usage={"session": 85, "week": 20, "session_resets": "2026-11-15T18:20:00+00:00"}))
        self.assertEqual(limit["none"], "No bees until usage resets at 15 Nov, 18:20.")
        unknown = self.bees(state(workers=[], usage={"session": 1, "week": 95}))
        self.assertEqual(unknown["none"], "No bees fly at a usage limit.")
        self.assertEqual(self.bees(state(workers=[]))["none"], "No bees flying.")
        stopped = self.bees(state(workers=[], stop_requested=True, supervisor="asleep", usage={"session": 85, "week": 20}))
        self.assertEqual(stopped["none"], "No bees fly while Kandu is stopped.")  # stopped says so before a limit or sleep
        html = self.run_js(f"return beesHtml(beesAtWork({json.dumps(state(workers=[]))}, {json.dumps(QUEUE)}, new Date()));")
        self.assertIn("No bees flying.", html)
        self.assertIn(self.text["fa"]["bees_none_asleep"], json.dumps(self.bees(state(workers=[], supervisor="asleep"), "fa"), ensure_ascii=False))

    # The sheet.

    def sheet(self, number, s=None, queue=QUEUE, review=None, info=None, proposal=None, repo=REPO, lang="en", part=None):
        view = (f"sheetView({json.dumps(f'{repo}#{number}')}, {json.dumps(s or state())}, {json.dumps(queue)}, "
                f"{{review: {json.dumps(review)}, info: {json.dumps(info)}, proposal: {json.dumps(proposal)}}})")
        return self.run_js(f"return {part}({view});" if part else f"return {view};", lang)

    STEPS = [{"ts": "2026-11-15T14:02:00+00:00", "type": "task_sized", "run_id": "r-0", "size": "Medium", "door": "one-way"},
             {"ts": "2026-11-15T14:05:00+00:00", "type": "phase_changed", "phase": "implement"},
             {"ts": "2026-11-15T14:31:00+00:00", "type": "run_finished", "run_id": "r-1", "role": "worker", "duration_ms": 1560000},
             {"ts": "2026-11-15T14:32:00+00:00", "type": "notification_sent", "kind": "x"},
             {"ts": "2026-11-15T14:33:00+00:00", "type": "phase_changed", "phase": "review round 1"},
             {"ts": "2026-11-15T14:40:00+00:00", "type": "review_finished", "run_id": "r-2", "round": 1, "blockers": 0}]
    PR = {"number": 7, "title": "Store it", "url": "https://github.com/acme-org/website/pull/7", "state": "OPEN", "head_sha": "abc",
          "approved": False, "can_approve": True, "why_not": None}

    def info(self, body="## Goal\nAdd a delivery window to each order.\n\n## Acceptance criteria\n- one\n"):
        return {"task": f"{REPO}#8", "issue": {"title": "t", "body": body, "url": "u"}, "outcome": None,
                "cost": {"total": {"total": 0, "cache_read": 0}, "runs": 0, "duration_ms": 0, "roles": {}}, "steps": self.STEPS}

    def review(self, **pr):
        return {"task": f"{REPO}#8", "pr": self.PR | pr, "rounds": [], "blockers": [], "waited": [], "holds": [], "kept": []}

    def test_the_sheet_shows_number_project_size_marks_title_links_and_what_it_does(self):
        flowers = FLOWERS | {f"{REPO}#8": FLOWERS[f"{REPO}#8"] | {"size": "Medium", "door": "one-way"}}
        view = self.sheet(8, s=state(flowers=flowers), review=self.review(), info=self.info())
        self.assertEqual(view["number"], "⁦#8⁩")
        self.assertEqual(view["meta"], "website · Medium · one-way")
        self.assertEqual(view["title"], "Store the delivery window")
        self.assertEqual(view["links"], {"pr": self.PR["url"], "issue": f"https://github.com/{REPO}/issues/8", "run": "r-2"})
        self.assertEqual(view["what"], "Add a delivery window to each order.")
        self.assertEqual((view["state"], view["tone"]), ("Your approval", "amber"))
        body = self.sheet(8, s=state(flowers=flowers), review=self.review(), info=self.info(), part="sheetBody")
        for part in ("Store the delivery window", "website · Medium · one-way", f'href="{self.PR["url"]}"',
                     f'href="https://github.com/{REPO}/issues/8"', 'data-run="r-2"', "Add a delivery window to each order.",
                     "data-close-sheet"):
            self.assertIn(part, body)

    def test_what_it_does_falls_back_to_the_issues_first_lines_without_a_goal(self):
        view = self.sheet(8, review=self.review(), info=self.info(body="Plain text first.\n\nMore after."))
        self.assertEqual(view["what"], "Plain text first.")
        self.assertEqual(self.sheet(8, review=self.review(), info=None)["what"], "")

    def test_the_sheet_shows_its_phases_with_the_current_one_marked(self):
        working = state(workers=[{"task": f"{REPO}#3", "project": "website", "machine": "hive-box", "phase": "review round 1",
                                  "started": NOW, "run_id": "r-9"}])
        phases = self.sheet(3, s=working, info=self.info())["phases"]
        self.assertEqual([(p["name"], p["at"]) for p in phases],
                         [("implement", "done"), ("review round 1", "now"), ("merge", "next"), ("deploy", "next"), ("live check", "next")])
        approval = self.sheet(8, review=self.review(), info=self.info())["phases"]  # its yes: at merge
        self.assertEqual([(p["name"], p["at"]) for p in approval],
                         [("implement", "done"), ("review round 1", "done"), ("merge", "now"), ("deploy", "next"), ("live check", "next")])
        ready = self.sheet(2, info=None)["phases"]  # waits its turn: none is current yet
        self.assertEqual([(p["name"], p["at"]) for p in ready], [(name, "next") for name in ("sizing", "implement", "review", "merge", "deploy", "live check")])
        done = self.sheet(11, info=self.info())["phases"]
        self.assertEqual({p["at"] for p in done}, {"done"})
        body = self.sheet(3, s=working, info=self.info(), part="sheetBody")
        self.assertRegex(body, r'<div class="phase now"><i class="hex"></i><span>review round 1</span></div>')

    def test_the_sheet_shows_its_steps_so_far_with_their_times(self):
        steps = self.sheet(8, review=self.review(), info=self.info())["steps"]
        self.assertEqual([(s["title"], s["time"], s["text"]) for s in steps], [
            ("Task sized", "15 Nov, 14:02", "Medium · one-way"), ("Implement", "15 Nov, 14:05", ""),
            ("Run finished", "15 Nov, 14:31", "worker · 26 min"), ("Review round 1", "15 Nov, 14:33", ""),
            ("Review finished", "15 Nov, 14:40", "round 1 · 0 blockers")])
        body = self.sheet(8, review=self.review(), info=self.info(), part="sheetBody")
        self.assertIn('<div class="steps">', body)
        self.assertIn("15 Nov, 14:40", body)
        self.assertIn("Nothing has happened yet.", self.sheet(2, info=None, part="sheetBody"))

    def test_the_sheet_shows_the_queens_question_while_it_waits(self):
        view = self.sheet(1, info=self.info())
        self.assertEqual(view["question"], "Should gift cards expire?")
        self.assertIn("Should gift cards expire?", self.sheet(1, info=self.info(), part="sheetBody"))
        self.assertIsNone(self.sheet(2, info=None)["question"])

    def test_the_sheet_shows_the_proposed_slices_while_it_waits_for_a_split(self):
        proposal = {"title": "Export", "reason": "Too big for one PR.", "fingerprint": "ab12", "user_text": None,
                    "slices": [{"title": "One note", "size": "Small", "files": 2, "door": "two-way", "body": "b1"},
                               {"title": "All notes", "size": "Small", "files": 3, "door": "two-way", "body": "b2"}]}
        body = self.sheet(10, proposal=proposal, info=None, part="sheetBody")
        self.assertIn("Too big for one PR.", body)
        self.assertLess(body.index("One note"), body.index("All notes"))
        foot = self.sheet(10, proposal=proposal, info=None, part="sheetFoot")
        self.assertIn("Accept this split", foot)
        self.assertIn("data-sure", foot)

    def test_the_footer_says_what_waits_and_offers_the_actions_kandu_has_for_its_state(self):
        foot = self.sheet(8, review=self.review(), info=self.info(), part="sheetFoot")
        self.assertIn("Waits on: your yes.", foot)
        self.assertIn("data-sure", foot)  # approve, asked first
        self.assertIn("Approve and merge", foot)
        stuck = self.sheet(9, info=None, part="sheetFoot")
        self.assertIn("Waits on: your decision.", stuck)
        self.assertIn("It failed and stopped: the tests time out.", self.sheet(9, info=None, part="sheetBody"))  # and why
        self.assertIn("data-back-sure", stuck)  # send it back to work
        asks = self.sheet(1, info=None, part="sheetFoot")
        self.assertIn('data-fly="didban" data-focus="website"', asks)
        working = self.sheet(3, info=None, part="sheetFoot")
        self.assertIn('data-stop="r-1"', working)
        quiet = self.sheet(2, info=None, part="sheetFoot")
        self.assertNotIn("sheet-note", quiet)
        self.assertIn(f'href="https://github.com/{REPO}/issues/2"', quiet)  # with nothing else to do, its issue
        error = state(flowers=FLOWERS | {f"{REPO}#2": mark(status=status("error", "internal_error", "x"))})
        self.assertIn(f'data-to-queen="{REPO}#2"', self.sheet(2, s=error, info=None, part="sheetFoot"))
        by_hand = self.sheet(3, repo=SHOP, info=None, part="sheetFoot")
        self.assertIn('id="taskflight"', by_hand)

    def test_the_footer_offers_the_queens_yes_to_drop_a_blocker(self):
        queen = state()["queen"] | {"drops": [{"task": f"{REPO}#9", "blocker": f"{REPO}#4", "reason": "Done by hand."}]}
        foot = self.sheet(9, s=state(queen=queen), info=None, part="sheetFoot")
        button = re.search(r'<button[^>]*data-drop="([^"]+)"[^>]*data-blocker="([^"]+)"[^>]*>([^<]*)</button>', foot)
        self.assertIsNotNone(button, foot)
        self.assertEqual(button.groups(), (f"{REPO}#9", f"{REPO}#4", self.text["en"]["drop_yes"]))
        self.assertNotIn("data-drop", self.sheet(8, s=state(queen=queen), review=self.review(), info=self.info(), part="sheetFoot"))

    def test_a_done_task_shows_its_tokens_as_the_daily_limit_counts_them(self):
        info = self.info() | {"task": f"{REPO}#11", "outcome": None,
                              "cost": {"total": {"total": 52000, "cache_read": 12000}, "runs": 2, "duration_ms": 0, "roles": {}}}
        body = self.sheet(11, info=info, part="sheetBody")
        self.assertIn("Tokens used: 40,000", body)  # without the tokens read from the cache
        self.assertNotIn("Tokens used", self.sheet(8, review=self.review(), info=info, part="sheetBody"))  # not done yet

    # What the Classic look's task window showed besides (#64).

    def test_the_sheet_says_the_prs_state_or_that_there_is_none_yet(self):
        self.assertIn('<span class="chip">OPEN</span>', self.sheet(8, review=self.review(), info=self.info(), part="sheetBody"))
        self.assertIn('<span class="chip">MERGED</span>', self.sheet(8, review=self.review(state="MERGED"), info=None, part="sheetBody"))
        self.assertIn(f'<span class="muted">{self.text["en"]["no_pr"]}</span>', self.sheet(2, info=None, part="sheetBody"))
        self.assertIn(self.text["values_fa"]["OPEN"], self.sheet(8, review=self.review(), info=None, lang="fa", part="sheetBody"))

    def test_the_sheet_marks_a_task_that_waits_for_the_user_once(self):
        yours = self.text["en"]["waits_for_you"]
        self.assertIn(f'<span class="badge" style="--c:#e8590c">{yours}</span>', self.sheet(8, review=self.review(), info=None, part="sheetBody"))
        asks = self.sheet(1, info=None, part="sheetBody")  # its state says so already
        self.assertEqual(asks.count(yours), 1, asks)
        self.assertNotIn(yours, self.sheet(2, info=None, part="sheetBody"))

    def test_the_slices_show_their_english_text_when_the_proposal_is_in_the_users_language(self):
        proposal = {"title": "Export", "reason": "Too big.", "fingerprint": "ab12",
                    "user_text": {"reason": "Zu groß.", "slices": [{"title": "Eine Notiz", "body": "Text eins"}]},
                    "slices": [{"title": "One note", "size": "Small", "files": 2, "door": "two-way", "body": "English body"}]}
        body = self.sheet(10, s=state(language="de"), proposal=proposal, info=None, part="sheetBody")
        self.assertIn("Eine Notiz", body)
        self.assertIn(self.text["en"]["english_text"], body)
        self.assertIn("English body", body)
        plain = self.sheet(10, s=state(language="de"), proposal=proposal | {"user_text": None}, info=None, part="sheetBody")
        self.assertNotIn(self.text["en"]["english_text"], plain)

    def test_a_done_task_folds_its_whole_issue_and_its_cost_by_role(self):
        info = self.info(body="## Goal\nThe whole issue, not only its goal.") | {"task": f"{REPO}#11", "outcome": "merged",
            "cost": {"total": {"total": 1500, "cache_read": 500}, "runs": 2, "duration_ms": 600000,
                     "roles": {"worker": {"runs": 2, "duration_ms": 600000, "tokens": {"total": 1200, "cache_read": 200}}}}}
        body = self.sheet(11, info=info, part="sheetBody")
        folded = body[body.index(f'<summary>{self.text["en"]["task_full_issue"]}</summary>'):]
        for part in ("The whole issue, not only its goal.", f'{self.text["en"]["cost_cached"]}: 500', self.text["en"]["cost_role"],
                     "<td>worker</td>", "10 min", "1,000"):
            self.assertIn(part, folded, part)
        self.assertNotIn(self.text["en"]["task_full_issue"], self.sheet(8, review=self.review(), info=info, part="sheetBody"))

    def test_the_footer_offers_a_free_flight_on_any_task(self):
        quiet = self.sheet(2, info=None, part="sheetFoot")
        self.assertIn(f'<span id="taskflight" class="flightbox"><button class="btn" data-fly="{REPO}#2">', quiet)
        self.assertIn(f'href="https://github.com/{REPO}/issues/2"', quiet)  # and still its issue, with nothing else to do
        self.assertEqual(self.sheet(8, review=self.review(), info=None, part="sheetFoot").count('id="taskflight"'), 1)

    def test_the_sheet_reads_in_persian(self):
        view = self.sheet(8, review=self.review(), info=self.info(), lang="fa")
        self.assertEqual(view["state"], self.text["values_fa"]["Your approval"])
        self.assertNotRegex(view["number"] + "".join(s["time"] for s in view["steps"]), r"[0-9]")
        self.assertIn(self.text["values_fa"]["implement"], [p["label"] for p in view["phases"]])


class SheetPageTest(unittest.TestCase, Page):
    def test_the_sheet_rises_from_the_bottom_on_a_phone_and_is_centred_on_a_wide_screen(self):
        self.assertIn('<dialog id="sheet" class="sheet"', self.page)
        self.assertRegex(self.style, r"dialog\.sheet \{[^}]*width:min\(600px")
        phone = self.style.split("@media (max-width: 760px) {")[1].split("\n}\n")[0]
        self.assertRegex(phone, r"dialog\.sheet \{[^}]*margin:auto 0 0;[^}]*border-radius:22px 22px 0 0")

    def test_the_sheet_closes_with_its_close_button_or_a_tap_outside_it(self):
        clicks = self.page.split('document.addEventListener("click"')[1].split("\n});")[0]
        self.assertIn('if (hit("close-sheet")) { $("sheet").close(); return; }', clicks)
        self.assertIn('if (e.target === $("sheet")) $("sheet").close();', clicks)

    def test_every_place_that_opened_a_tasks_details_opens_the_sheet(self):
        self.assertNotIn("showReviews", self.page)
        clicks = self.page.split('document.addEventListener("click"')[1].split("\n});")[0]
        self.assertIn("if (review) { confirming = false; sendingBack = false; showTask(review.dataset.review); }", clicks)
        self.assertIn('$("sheet").showModal()', self.function("showTask"))
        self.assertNotIn('"modal"', self.function("showTask"))
        self.assertNotIn("modalbody", self.function("showTask"))

    def test_the_queens_brief_shows_beside_the_tasks_on_a_wide_screen(self):
        tasks = self.page.split('<section data-panel="tasks"')[1].split("</section>")[0]
        side = tasks.split('<div class="side">')[1]
        self.assertIn('id="workers"', side)
        self.assertIn('id="queenbox"', side)
        self.assertRegex(self.style, r"\.layout \{[^}]*grid-template-columns:minmax\(0,1fr\) 340px")
        self.assertIn('$("queenbox").innerHTML = briefHtml(s.queen);', self.function("renderQueen"))

    def test_the_stat_tiles_and_the_asks_stay_off_the_tasks_page(self):
        self.assertRegex(self.style, r'body\[data-page="tasks"\] #stats, body\[data-page="tasks"\] #asks \{ display:none; \}')

    def test_nothing_on_the_tasks_page_pushes_a_phone_page_sideways(self):
        self.assertRegex(self.style, r"\.task-row \{[^}]*flex-wrap:wrap")
        self.assertRegex(self.style, r"\.row-title \{[^}]*min-width:0")

    def test_the_new_words_read_in_both_languages(self):
        new = ("bees_of", "bees_none", "bees_none_asleep", "bees_none_limit", "bees_none_limit_at", "bees_none_stopped",
               "so_far_min", "so_far_hours", "mark_sent_back", "mark_slice", "mark_question", "mark_questions", "tasks_empty",
               "task_pr", "task_issue", "task_run_log", "task_steps", "no_steps", "proposed_slices", "stop_bee",
               "step_round", "step_blockers", "task_sheet", "waiting_machine")
        for key in new:
            self.assertTrue(self.text["en"].get(key), key)
            self.assertTrue(self.text["fa"].get(key), key)

    def test_design_describes_the_tasks_page_and_the_sheet(self):
        design = (ROOT / "DESIGN.md").read_text(encoding="utf-8").split("## 7. Dashboard")[1].split("\n## 8.")[0]
        tasks = next(line for line in design.splitlines() if line.startswith("- **Tasks:**"))
        for part in ("search", "number", "fold", "Bees at work", "brief", "sheet"):
            self.assertIn(part, tasks)


if __name__ == "__main__":
    unittest.main()
