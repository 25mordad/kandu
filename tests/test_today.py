"""The Today page: what waits for the user first, then each project's flight path, the queen's brief and today's
events (#44). The page's own functions run in node on the state and queue the API gives."""
import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from kandu import dashboard

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
REPO = "acme-org/website"
BLOG = "acme-org/blog"
SHOP = "acme-org/shop"
WAKE = "2026-11-15T21:22:00+00:00"
NOW = "2026-11-15T15:00:00+00:00"


def task(repo, number, title="", **fields):
    return {"task": f"{repo}#{number}", "title": title or f"Task {number}", "url": f"https://github.com/{repo}/issues/{number}",
            "by_hand": False, "yours": False} | fields


def columns(**found):
    names = ("Done", "Backlog", "Ready", "In progress", "In review", "Needs info", "Needs split", "Blocked", "Technical debt")
    return {name: found.get(name.replace(" ", "_"), []) for name in names}


def status(state, why=None, detail=None, wait=None):
    return {"state": state, "why": why, "detail": detail, "tries": None, "wait": wait, "cleaned": None}


def wait(on, **fields):
    return {"on": on, "checks": [], "blockers": [], "detail": [], "held": False} | fields


def mark(**fields):
    return {"size": None, "door": None, "sent_back": False, "slice": False, "pr": None, "questions": 0, "yours": False,
            "waited": [], "status": None} | fields


QUEUE = {"done_days": 5, "projects": {
    "website": columns(
        Backlog=[task(REPO, 1, "Offer gift cards")],
        Ready=[task(REPO, 2, "Group teas by region")],
        In_progress=[task(REPO, 3, "Search by tag"), task(REPO, 4), task(REPO, 5), task(REPO, 6), task(REPO, 7)],
        In_review=[task(REPO, 8, "Store the delivery window"), task(REPO, 14, "Sort by date")],
        Blocked=[task(REPO, 9, "Show stock")],
        Needs_split=[task(REPO, 10, "Export as Markdown")],
        Technical_debt=[task(REPO, 13)],
        Done=[task(REPO, 11, "Word count"), task(REPO, 12, "Keep a draft")]),
    "blog": columns(Ready=[task(BLOG, 1, "Dark mode")]),
    "shop": columns(Ready=[task(SHOP, 2, "Price per 100 g")],
                    Backlog=[task(SHOP, 3, "Write to the landlord", by_hand=True, yours=True)],
                    Done=[task(SHOP, 5, "RSS feed")]),
}}

FLOWERS = {
    f"{REPO}#1": mark(yours=True, status=status("fine", wait=wait("decision"))),
    f"{REPO}#8": mark(pr="waits", yours=True, status=status("fine", "waits_for_you", wait=wait("yes"))),
    f"{REPO}#9": mark(status=status("stuck", "failed", "the tests time out", wait=wait("decision"))),
    f"{REPO}#14": mark(status=status("stuck", "round_no_commit", "nothing was pushed", wait=wait("decision"))),
    f"{REPO}#3": mark(status=status("fine")),
}


def state(**fields):
    return {"supervisor": "awake", "next_wake": WAKE, "usage": {"session": 10, "week": 20},
            "limits": {"session": 80, "week": 90}, "timer_on": True, "stop_requested": False,
            "workers": [{"task": f"{REPO}#3", "project": "website", "machine": "hive-box", "phase": "implement",
                         "started": NOW, "run_id": "r-1"}],
            "waiting_for_machine": [f"{REPO}#2"], "project_order": ["website", "blog", "shop"],
            "paused_projects": ["blog"], "paused_in_config": [], "over_limit": {}, "parallel": {"total": 4, "projects": {}},
            "project_settings": [{"name": "website", "split_approval": "user"}, {"name": "blog", "split_approval": "user"},
                                 {"name": "shop", "split_approval": "queen"}],
            "flowers": FLOWERS, "waits_for_you": 3, "unreachable_machines": [],
            "queen": {"ts": "2026-11-15T14:20:00+00:00", "brief": "Released #18 and #21: both small.", "tokens": 41200,
                      "today": 90000, "limit": 400000,
                      "questions": [{"task": f"{REPO}#1", "question": "Should gift cards expire?"}], "drops": []}} | fields


class Page:
    page = dashboard.PAGE.read_text(encoding="utf-8")
    text = json.loads(re.search(r'<script type="application/json" id="text">(.*?)</script>', page, re.DOTALL).group(1))
    style = page.split("<style>")[1].split("</style>")[0]

    @classmethod
    def function(cls, name):
        begin = cls.page.index(f"function {name}(")
        if cls.page[begin - 6:begin] == "async ":
            begin -= 6
        return cls.page[begin:cls.page.index("\n}\n", begin) + 2]

    @classmethod
    def line(cls, start):
        return next(line for line in cls.page.splitlines() if line.startswith(start)) + "\n"


LINES = ("const esc = ", "const locale = ", "const t = ", "const num = ", "const pct = ", "const faDigits = ", "const short = ",
         "const when = ", "const clock = ", "const githubRefused = ", "const bdi = ", "const numberOf = ", "const repoOf = ",
         "const fillIn = ", "const overLimit = ", "const ON_THE_WAY = ", "const STAGES = ", "const STAGE_OF = ",
         "const DOTS_SHOWN = ", "const HIVE_CELLS = ", "const DOT_ORDER = ", "const WAIT_ORDER = ", "const EVENT_TONES = ")
FUNCTIONS = ("tr", "stopWord", "stateChip", "gauge", "mergeMode", "fields", "statusText", "waitItems", "waitView", "todaySentence",
             "healthItems", "dotKind", "flightPaths", "hiveCells", "eventTone", "eventLine", "todayEvents", "briefMeta",
             "healthHtml", "sentenceHtml", "waitsHtml", "pathsHtml", "hiveHtml", "eventsHtml", "briefHtml")


@unittest.skipUnless(NODE, "needs node to run the page's script")
class TodayTest(unittest.TestCase, Page):
    def run_js(self, script, lang="en"):
        """Runs the page's own functions in node, with its texts, in `lang`."""
        prelude = (f"const TEXT = {json.dumps(self.text)};\nlet lang = {json.dumps(lang)};\n"
                   + "".join(self.line(start) for start in LINES) + "".join(self.function(name) for name in FUNCTIONS))
        done = subprocess.run([NODE, "-"], input=prelude + f"\nconsole.log(JSON.stringify((() => {{ {script} }})()));",
                              capture_output=True, text=True, timeout=30, env={**os.environ, "TZ": "UTC"})
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def given(self, s=None, queue=QUEUE):
        return f"const s = {json.dumps(s or state())}; const queue = {json.dumps(queue)}; const items = waitItems(s, queue);"

    # The sentence at a glance.

    def sentence(self, s, lang="en", queue=QUEUE):
        parts = self.run_js(self.given(s, queue) + "return todaySentence(s, items);", lang)
        return " · ".join(p["text"] for p in parts), [p.get("tone") for p in parts]

    def test_the_sentence_says_how_many_wait_and_are_stuck_with_the_bees_flying(self):
        text, tones = self.sentence(state())
        self.assertEqual(text, "4 wait for you · 2 stuck · 1 bee flying")
        self.assertEqual(tones[:2], ["amber", "red"])
        two = state(workers=state()["workers"] * 2)
        self.assertEqual(self.sentence(two)[0], "4 wait for you · 2 stuck · 2 bees flying")

    def test_with_nothing_waiting_it_says_all_clear(self):
        quiet = {"done_days": 5, "projects": {"website": columns(Ready=[task(REPO, 2)])}}
        self.assertEqual(self.sentence(state(flowers={}, queen=None), queue=quiet)[0], "All clear · 1 bee flying")
        one = state(flowers={f"{REPO}#2": FLOWERS[f"{REPO}#8"]}, queen=None, workers=[])
        self.assertEqual(self.sentence(one, queue=quiet)[0], "1 waits for you · 0 bees flying")

    def test_asleep_it_says_when_kandu_wakes(self):
        quiet = {"done_days": 5, "projects": {"website": columns(Ready=[task(REPO, 2)])}}
        asleep = state(supervisor="asleep", workers=[], flowers={}, queen=None)
        self.assertEqual(self.sentence(asleep, queue=quiet)[0], "Asleep · wakes 21:22")
        self.assertEqual(self.sentence(asleep | {"timer_on": False, "next_wake": None}, queue=quiet)[0], "Asleep · timer off")
        waiting = state(supervisor="asleep", workers=[])
        self.assertEqual(self.sentence(waiting)[0], "4 wait for you · 2 stuck · Asleep · wakes 21:22")

    def test_at_a_usage_limit_it_says_so_and_when_it_resets(self):
        usage = {"session": 85, "week": 20, "session_resets": "2026-11-15T18:20:00+00:00", "week_resets": "2026-11-19T20:00:00+00:00"}
        text, tones = self.sentence(state(usage=usage))
        self.assertEqual(text, "At the 5-hour limit · resets 15 Nov, 18:20")
        self.assertEqual(tones[0], "amber")
        week = usage | {"session": 1, "week": 95}
        self.assertEqual(self.sentence(state(usage=week))[0], "At the weekly limit · resets 19 Nov, 20:00")
        self.assertEqual(self.sentence(state(usage=week, timer_on=False))[0], "At the weekly limit · resets 19 Nov, 20:00")
        self.assertIn("بازنشانی", self.sentence(state(usage=week), "fa")[0])

    def test_at_both_limits_it_says_when_the_later_one_resets(self):
        usage = {"session": 85, "week": 95, "session_resets": "2026-11-15T18:20:00+00:00", "week_resets": "2026-11-19T20:00:00+00:00"}
        self.assertEqual(self.sentence(state(usage=usage))[0], "At the weekly limit · resets 19 Nov, 20:00")

    def test_at_a_usage_limit_with_no_reset_known_it_says_when_kandu_looks_again(self):
        text, tones = self.sentence(state(usage={"session": 85, "week": 20}))
        self.assertEqual(text, "At the 5-hour limit · Kandu checks it again at 21:22")
        self.assertEqual(self.sentence(state(usage={"session": 1, "week": 95}))[0], "At the weekly limit · Kandu checks it again at 21:22")

    def test_stopped_it_says_so(self):
        self.assertEqual(self.sentence(state(stop_requested=True))[0], "Stopped · nothing starts until you resume")

    def test_while_bees_finish_their_step_it_says_kandu_is_stopping_and_how_many(self):
        text, tones = self.sentence(state(supervisor="awake", stop_requested=True, stopping=2))
        self.assertEqual(text, "Stopping · 2 bees still at work · the bees at work finish their step; "
                               "nothing new starts until you resume")
        self.assertEqual(tones[0], "red")
        self.assertIn("۲", self.sentence(state(supervisor="awake", stop_requested=True, stopping=2), "fa")[0])

    def test_the_sentence_reads_in_persian(self):
        text, _ = self.sentence(state(), "fa")
        self.assertIn("۴", text)
        self.assertIn("۲", text)
        self.assertNotRegex(text, r"[0-9]")

    def test_the_sentence_is_drawn_with_its_colours(self):
        html = self.run_js(self.given() + "return sentenceHtml(todaySentence(s, items));")
        self.assertIn('<b class="tone-amber">4 wait for you</b>', html)
        self.assertIn('<b class="tone-red">2 stuck</b>', html)

    # The health line on a phone.

    def test_the_health_line_shows_the_state_usage_bees_and_a_machine_that_is_down(self):
        down = state(unreachable_machines=[{"machine": "studio-mac", "since": "2026-11-15T14:05:00+00:00", "tasks": [f"{REPO}#2"]}])
        items = self.run_js(f"return healthItems({json.dumps(down)});")
        self.assertEqual([(i["kind"], i["tab"]) for i in items],
                         [("state", "activity"), ("bar", "days"), ("bar", "days"), ("bees", "tasks"), ("machine", "tasks")])
        self.assertEqual(items[0]["text"], "Awake · wakes 21:22")
        self.assertEqual((items[1]["value"], items[1]["limit"]), (10, 80))
        self.assertEqual((items[2]["value"], items[2]["limit"]), (20, 90))
        self.assertEqual(items[3]["text"], "Bees 1 of 4")
        self.assertEqual(items[4]["text"], "studio-mac down since 15 Nov, 14:05")
        html = self.run_js(f"return healthHtml(healthItems({json.dumps(down)}));")
        self.assertEqual(re.findall(r'<button class="hitem[^"]*" data-tab="(\w+)"', html), ["activity", "days", "days", "tasks", "tasks"])
        self.assertIn('class="gauge', html)  # each usage against its limit
        fine = self.run_js(f"return healthItems({json.dumps(state())}).map(i => i.kind);")
        self.assertNotIn("machine", fine)

    def test_the_health_line_shows_only_on_a_phone(self):
        self.assertRegex(self.style, r"\.health \{[^}]*display:none;")
        phone = self.style.split("@media (max-width: 760px) {")[1].split("\n}\n")[0]
        self.assertRegex(phone, r"\.health \{ display:flex; \}")

    # Waits for you.

    def test_waits_for_you_lists_what_waits_needs_first_with_kind_place_title_why_and_actions(self):
        items = self.run_js(self.given() + "return items;")
        self.assertEqual([(i["task"], i["kind"]) for i in items], [
            (f"{REPO}#8", "Your approval"), (f"{REPO}#1", "The queen asks"), (f"{REPO}#10", "Needs split"),
            (f"{SHOP}#3", "By hand, waits for you"), (f"{REPO}#9", "Stuck"), (f"{REPO}#14", "Stuck")])
        first = items[0]
        self.assertEqual((first["project"], first["title"], first["tone"]), ("website", "Store the delivery window", "amber"))
        self.assertEqual(first["line"], "The review passed; the merge waits for your approval.")
        self.assertEqual([(a["label"], a["attrs"]) for a in first["actions"]],
                         [("Approve and merge", f'data-approve-open="{REPO}#8"'), ("Open", f'data-review="{REPO}#8"')])
        asks = items[1]
        self.assertEqual(asks["line"], "Should gift cards expire?")
        self.assertEqual(asks["actions"][0]["attrs"], 'data-fly="didban" data-focus="website"')
        split = items[2]
        self.assertEqual(split["actions"][0], {"label": "Accept this split", "attrs": f'data-approve-open="{REPO}#10"', "primary": True})
        by_hand = items[3]
        self.assertEqual(by_hand["actions"][0]["attrs"], f'data-fly="{SHOP}#3"')
        stuck = items[4]
        self.assertEqual((stuck["tone"], stuck["stuck"]), ("red", True))
        self.assertEqual(stuck["line"], "It failed and stopped: the tests time out. Read its log, then send it back.")
        self.assertEqual(stuck["actions"][0]["attrs"], f'data-back-open="{REPO}#9"')  # Blocked: it can go back to work
        self.assertEqual([a["attrs"] for a in items[5]["actions"]], [f'data-review="{REPO}#14"'])  # In review: only open

    def test_the_queen_asking_to_drop_a_blocker_offers_her_yes(self):
        s = state()
        s["queen"]["drops"] = [{"task": f"{SHOP}#2", "blocker": f"{SHOP}#1", "reason": "It was done by hand."}]
        items = self.run_js(self.given(s) + "return items.filter(i => i.task.endsWith('shop#2'));")
        self.assertEqual(items[0]["kind"], "The queen asks")
        self.assertEqual(items[0]["line"], f"She asks to drop the blocker {SHOP}#1: It was done by hand.")
        self.assertEqual(items[0]["actions"][0], {"label": "Yes, drop it", "attrs": f'data-drop="{SHOP}#2" data-blocker="{SHOP}#1"', "primary": True})

    def test_a_split_the_queen_accepts_and_a_finished_or_working_task_do_not_wait(self):
        flowers = FLOWERS | {f"{SHOP}#5": mark(status=status("stuck", "failed", "x")),  # Done: it is not open any more
                             f"{REPO}#4": mark(status=status("stuck", "failed", "x"))}
        s = state(flowers=flowers, workers=state()["workers"] + [{"task": f"{REPO}#4", "phase": "fix round 1"}])
        tasks = self.run_js(self.given(s) + "return items.map(i => i.task);")
        self.assertNotIn(f"{SHOP}#2", tasks)  # its project's splits are the queen's
        self.assertNotIn(f"{SHOP}#5", tasks)
        self.assertNotIn(f"{REPO}#4", tasks)  # a worker is on it again

    def test_the_stepper_shows_one_item_at_a_time_with_its_place_and_its_neighbours(self):
        views = self.run_js(self.given() + """
const pick = v => v && {at: v.current.task, place: v.place, prev: v.prev, next: v.next, rest: v.rest.map(i => i.task)};
return {first: pick(waitView(items, null)), third: pick(waitView(items, items[2].task)), gone: pick(waitView(items, "x/y#99")),
        last: pick(waitView(items, items[5].task)), none: waitView([], null), one: pick(waitView(items.slice(0, 1), null))};""")
        tasks = [f"{REPO}#8", f"{REPO}#1", f"{REPO}#10", f"{SHOP}#3", f"{REPO}#9", f"{REPO}#14"]
        self.assertEqual(views["first"], {"at": tasks[0], "place": "1 of 6", "prev": tasks[5], "next": tasks[1], "rest": tasks[1:]})
        self.assertEqual(views["third"], {"at": tasks[2], "place": "3 of 6", "prev": tasks[1], "next": tasks[3],
                                          "rest": tasks[:2] + tasks[3:]})
        self.assertEqual(views["gone"]["at"], tasks[0])  # an item that went away leaves the first in its place
        self.assertEqual((views["last"]["place"], views["last"]["next"]), ("6 of 6", tasks[0]))
        self.assertIsNone(views["none"])
        self.assertEqual(views["one"], {"at": tasks[0], "place": "1 of 1", "prev": tasks[0], "next": tasks[0], "rest": []})
        persian = self.run_js(self.given() + "return waitView(items, items[1].task).place;", "fa")
        self.assertEqual(persian, "۲ از ۶")

    def test_the_card_draws_its_kind_place_title_why_actions_and_the_others_below(self):
        html = self.run_js(self.given() + "return waitsHtml(waitView(items, items[1].task));")
        self.assertIn("2 of 6", html)
        self.assertIn(f'data-wait-to="{REPO}#8"', html.split('class="wait-card')[0])  # previous
        self.assertIn(f'data-wait-to="{REPO}#10"', html.split('class="wait-card')[0])  # next
        card = html.split('class="wait-card')[1].split('class="wait-rest')[0]
        for part in ("The queen asks", "website", "#1", "Offer gift cards", "Should gift cards expire?", 'data-fly="didban"', f'data-review="{REPO}#1"'):
            self.assertIn(part, card)
        rest = html.split('class="wait-rest')[1]
        self.assertEqual(re.findall(r'data-wait-to="([^"]+)"', rest), [f"{REPO}#8", f"{REPO}#10", f"{SHOP}#3", f"{REPO}#9", f"{REPO}#14"])
        self.assertIn("Store the delivery window", rest)

    def test_with_nothing_waiting_the_section_is_not_shown(self):
        render = self.function("renderToday")
        self.assertIn('$("waits").hidden = !view;', render)
        self.assertIn('if (hit("wait-to")) { waitKey = hit("wait-to").dataset.waitTo; renderToday(); return; }',
                      self.page.split('document.addEventListener("click"')[1].split("\n});")[0])

    def test_an_action_opens_the_task_at_its_step(self):
        clicks = self.page.split('document.addEventListener("click"')[1].split("\n});")[0]
        self.assertIn('if (hit("approve-open")) { confirming = true; sendingBack = false; showTask(hit("approve-open").dataset.approveOpen); return; }', clicks)
        self.assertIn('if (hit("back-open")) { sendingBack = true; confirming = false; showTask(hit("back-open").dataset.backOpen); return; }', clicks)

    # Flight paths.

    def test_each_board_column_maps_to_its_stage(self):
        stages = self.run_js("return STAGES.map(t);")
        self.assertEqual(stages, ["Shape", "Ready", "Build", "Review", "Merge", "Live"])
        mapping = {"Backlog": "Shape", "Technical debt": "Shape", "Needs info": "Shape", "Needs split": "Shape",
                   "Ready": "Ready", "waiting": "Ready", "In progress": "Build", "Blocked": "Build",
                   "In review": "Review", "Your approval": "Merge", "Approved": "Merge", "Done": "Live"}
        found = self.run_js("return Object.fromEntries(Object.entries(STAGE_OF).map(([column, at]) => [column, t(STAGES[at])]));")
        self.assertEqual(found, mapping)

    def lanes(self, s=None, queue=QUEUE):
        return self.run_js(self.given(s, queue) + "return flightPaths(s, queue, items);")

    def test_a_task_goes_to_the_stage_of_its_column(self):
        board = {"done_days": 5, "projects": {"website": columns(
            Backlog=[task(REPO, 1)], Technical_debt=[task(REPO, 2)], Needs_info=[task(REPO, 3)], Needs_split=[task(REPO, 4)],
            Ready=[task(REPO, 5), task(REPO, 6)], In_progress=[task(REPO, 7)], Blocked=[task(REPO, 8)],
            In_review=[task(REPO, 9), task(REPO, 10), task(REPO, 11), task(REPO, 12)], Done=[task(REPO, 13)])}}
        flowers = {f"{REPO}#10": mark(pr="waits"), f"{REPO}#11": mark(pr="approved"),
                   f"{REPO}#12": mark(status=status("stuck", "merge_held", "x"))}
        s = state(flowers=flowers, queen=None, workers=[], waiting_for_machine=[f"{REPO}#6"], paused_projects=[])
        stages = self.run_js(self.given(s, board) + "return fields(s, queue)[0].tasks.map(i => [i.task.split('#')[1], i.stage]);")
        self.assertEqual(dict(stages), {"1": 0, "2": 0, "3": 0, "4": 0, "5": 1, "6": 1, "7": 2, "8": 2, "9": 3,
                                        "10": 4, "11": 4, "12": 3, "13": 5})  # a stuck PR stays where it was
        lane = self.lanes(s, board)[0]
        self.assertEqual([len(stage["dots"]) + stage["more"] for stage in lane["stages"][:5]], [4, 2, 2, 2, 2])
        self.assertEqual(lane["stages"][5]["done"], 1)

    def test_a_dot_says_needs_you_stuck_a_bee_on_it_or_waiting_its_turn(self):
        lane = self.lanes()[0]
        kinds = {d["task"].split("#")[1]: d["kind"] for stage in lane["stages"] for d in stage["dots"]}
        self.assertEqual(kinds["8"], "you")  # its yes
        self.assertEqual(kinds["1"], "you")  # the queen asks
        self.assertEqual(kinds["10"], "you")  # its split is the user's to accept
        self.assertEqual(kinds["9"], "stuck")
        self.assertEqual(kinds["14"], "stuck")
        self.assertEqual(kinds["3"], "bee")
        self.assertEqual(kinds["2"], "turn")
        self.assertEqual(kinds["13"], "turn")
        shop = next(l for l in self.lanes() if l["name"] == "shop")
        self.assertEqual([d["kind"] for d in shop["stages"][0]["dots"]], ["you"])  # an open by-hand task

    def test_a_stage_shows_three_dots_most_urgent_first_and_counts_the_rest(self):
        build = self.lanes()[0]["stages"][2]
        self.assertEqual([d["task"] for d in build["dots"]], [f"{REPO}#9", f"{REPO}#3", f"{REPO}#4"])
        self.assertEqual(build["more"], 3)

    def test_live_shows_how_many_are_done(self):
        lanes = {l["name"]: l for l in self.lanes()}
        self.assertEqual(lanes["website"]["stages"][5], {"dots": [], "more": 0, "done": 2})
        self.assertEqual(lanes["shop"]["stages"][5]["done"], 1)
        self.assertEqual(lanes["blog"]["stages"][5]["done"], 0)

    def test_lanes_that_need_attention_come_first_and_paused_ones_fade_last(self):
        lanes = self.lanes()
        self.assertEqual([(l["name"], l["paused"]) for l in lanes], [("website", False), ("shop", False), ("blog", True)])
        self.assertEqual([(l["line"], l["tone"]) for l in lanes],
                         [("2 stuck", "red"), ("1 waits for you", "amber"), ("Paused", "muted")])
        theirs = [{"name": name, "split_approval": "queen"} for name in ("website", "blog", "shop")]
        quiet = state(flowers={}, queen=None, paused_projects=[], project_settings=theirs)
        self.assertEqual([(l["name"], l["line"]) for l in self.lanes(quiet)],
                         [("shop", "1 waits for you"), ("website", "1 bee at work"), ("blog", "Quiet · 1 open")])
        html = self.run_js(self.given() + "return pathsHtml(flightPaths(s, queue, items), queue);")
        self.assertIn('<div class="lane off">', html)
        self.assertRegex(self.style, r"\.lane\.off \{[^}]*opacity:")

    def test_a_project_at_its_daily_token_limit_says_so_on_its_lane(self):
        quiet = state(flowers={}, queen=None, paused_projects=[], over_limit={"blog": {"today": 9, "limit": 5}})
        lanes = {l["name"]: l for l in self.lanes(quiet)}
        self.assertEqual((lanes["blog"]["line"], lanes["blog"]["tone"]), ("Daily token limit reached", "red"))

    def test_clicking_a_dot_opens_its_task(self):
        html = self.run_js(self.given() + "return pathsHtml(flightPaths(s, queue, items), queue);")
        dots = re.findall(r'<button class="pdot (\w+)" data-review="([^"]+)" title="([^"]*)"', html)
        self.assertIn(("stuck", f"{REPO}#9", "#9 Show stock"), [(k, t, re.sub("[⁦⁩]", "", title)) for k, t, title in dots])
        self.assertEqual(len(dots), 12)
        self.assertIn('<span class="pmore">+3</span>', html)
        self.assertRegex(html, r'<span class="pdone"[^>]*>2</span>')
        clicks = self.page.split('document.addEventListener("click"')[1].split("\n});")[0]
        self.assertIn("if (review) { confirming = false; sendingBack = false; showTask(review.dataset.review); }", clicks)

    def test_the_legend_explains_the_four_colours(self):
        html = self.run_js(self.given() + "return pathsHtml(flightPaths(s, queue, items), queue);")
        legend = html.split('class="plegend"')[1]
        self.assertEqual(re.findall(r'<i class="pdot (\w+)"></i>([^<]+)<', legend),
                         [("you", "needs you"), ("stuck", "stuck"), ("bee", "a bee on it"), ("turn", "waiting its turn")])

    def test_the_dot_colours_are_the_designs_tokens(self):
        self.assertRegex(self.style, r"\.pdot\.you \{[^}]*background:var\(--amber\)")
        self.assertRegex(self.style, r"\.pdot\.stuck \{[^}]*background:var\(--red\)")
        self.assertRegex(self.style, r"\.pdot\.bee \{[^}]*var\(--honey\)")
        self.assertRegex(self.style, r"\.pdot\.turn \{[^}]*background:var\(--dot\)")

    def test_an_unread_board_is_not_called_empty(self):
        html = self.run_js(self.given(queue=None) + "return pathsHtml(flightPaths(s, queue, items), queue);")
        self.assertIn("Reading the boards…", html)

    # The hive.

    def test_the_hive_shows_the_tasks_done_in_the_last_5_days_naming_each_on_hover(self):
        cells = self.run_js(f"return hiveCells({json.dumps(QUEUE)});")
        self.assertEqual(cells, [{"task": f"{REPO}#11", "project": "website", "title": "Word count"},
                                 {"task": f"{REPO}#12", "project": "website", "title": "Keep a draft"},
                                 {"task": f"{SHOP}#5", "project": "shop", "title": "RSS feed"}])
        html = self.run_js(f"return hiveHtml(hiveCells({json.dumps(QUEUE)}), 5);")
        self.assertIn("3 done in the last 5 days", html)
        self.assertEqual(re.findall(r'class="cell full" data-review="([^"]+)" title="([^"]+)"', html),
                         [(f"{REPO}#11", "website · Word count"), (f"{REPO}#12", "website · Keep a draft"), (f"{SHOP}#5", "shop · RSS feed")])
        self.assertIn("get(`/tasks/${repo}/${number}`)", self.page)  # a done task's dialog shows its cost
        empty = self.run_js("return hiveHtml([], 5);")
        self.assertIn("0 done in the last 5 days", empty)
        self.assertIn('class="cell"', empty)

    def test_the_hive_reads_in_persian(self):
        html = self.run_js(f"return hiveHtml(hiveCells({json.dumps(QUEUE)}), 5);", "fa")
        self.assertIn("۳ تسک انجام‌شده در ۵ روز اخیر", html)

    # The queen's brief.

    def test_the_queens_latest_brief_shows_with_its_time_and_tokens(self):
        self.assertEqual(self.run_js(f"return briefMeta({json.dumps(state()['queen'])});"), "15 Nov, 14:20 · 41.2k tokens")
        html = self.run_js(f"return briefHtml({json.dumps(state()['queen'])});")
        self.assertIn("The queen", html)
        self.assertIn("Released #18 and #21: both small.", html)
        self.assertEqual(self.run_js("return briefHtml(null);"), "")
        persian = self.run_js(f"return briefMeta({json.dumps(state()['queen'])});", "fa")
        self.assertIn("توکن", persian)
        self.assertNotRegex(persian, r"[0-9]")

    # Today's events.

    def test_today_lists_todays_events_newest_first_with_their_time(self):
        events = [{"ts": "2026-11-15T14:20:00+00:00", "type": "supervisor_woke"},
                  {"ts": "2026-11-15T14:52:00+00:00", "type": "review_finished", "task": f"{REPO}#8", "project": "website"},
                  {"ts": "2026-11-14T23:50:00+00:00", "type": "task_picked", "task": f"{REPO}#3"},
                  {"ts": "2026-11-15T14:31:00+00:00", "type": "task_finished", "task": f"{REPO}#9", "outcome": "failed"}]
        found = self.run_js(f"return todayEvents({json.dumps(events)}, new Date({json.dumps(NOW)})).map(e => e.ts);")
        self.assertEqual(found, ["2026-11-15T14:52:00+00:00", "2026-11-15T14:31:00+00:00", "2026-11-15T14:20:00+00:00"])
        html = self.run_js(f"return eventsHtml(todayEvents({json.dumps(events)}, new Date({json.dumps(NOW)})));")
        self.assertEqual(re.findall(r'<time>([^<]+)</time><i class="tone-(\w+)"', html), [("14:52", "blue"), ("14:31", "red"), ("14:20", "green")])
        self.assertIn("Review finished", html)
        self.assertIn(f'data-review="{REPO}#8"', html)
        self.assertIn("Nothing happened yet today.", self.run_js("return eventsHtml([]);"))

    def test_an_events_colour_says_its_kind(self):
        cases = {"woke": {"type": "supervisor_woke"}, "merged": {"type": "task_finished", "outcome": "merged"},
                 "failed": {"type": "task_finished", "outcome": "failed"}, "info": {"type": "task_finished", "outcome": "needs_info"},
                 "asks": {"type": "queen_decided", "action": "ask_user"}, "released": {"type": "queen_decided", "action": "release"},
                 "yours": {"type": "decision", "decision": "pr_settled", "reason": "the user decides next; CI is failing"},
                 "held": {"type": "decision", "decision": "merge_waits", "reason": "CI is running"},
                 "error": {"type": "decision", "decision": "internal_error", "reason": "x"},
                 "picked": {"type": "task_picked"}, "review": {"type": "review_finished"}, "down": {"type": "machine_down"},
                 "up": {"type": "machine_up"}, "loop": {"type": "loop_stopped"}, "approved": {"type": "user_approved"},
                 "other": {"type": "limits_changed"}}
        tones = self.run_js(f"return Object.fromEntries(Object.entries({json.dumps(cases)}).map(([k, e]) => [k, eventTone(e)]));")
        self.assertEqual(tones, {"woke": "green", "merged": "green", "failed": "red", "info": "amber", "asks": "amber",
                                 "released": "muted", "yours": "amber", "held": "amber", "error": "red", "picked": "blue",
                                 "review": "blue", "down": "red", "up": "green", "loop": "red", "approved": "green",
                                 "other": "muted"})

    def test_an_event_reads_in_persian(self):
        line = self.run_js('return eventLine({type: "supervisor_woke"});', "fa")
        self.assertEqual(line, self.text["values_fa"]["supervisor_woke"])

    def test_the_tones_are_the_designs_tokens(self):
        for tone, token in (("amber", "amber"), ("red", "red"), ("green", "green"), ("blue", "blue"), ("muted", "ink3")):
            self.assertRegex(self.style, rf"\.tone-{tone} \{{ color:var\(--{token}\); \}}")
            self.assertRegex(self.style, rf"i\.tone-{tone} \{{ background:var\(--{token}\); \}}")


class TodayPageTest(unittest.TestCase, Page):
    def test_today_holds_its_parts_in_the_designs_order(self):
        today = self.page.split('<section data-panel="today"')[1].split("</section>")[0]
        order = ['id="health"', 'id="waits"', 'id="hivebox"', 'id="sentence"', 'id="paths"', 'id="briefbox"', 'id="todayevents"']
        places = [today.index(part) for part in order]
        self.assertEqual(places, sorted(places))

    def test_the_stat_tiles_and_the_asks_stay_off_today(self):
        self.assertRegex(self.style, r'body\[data-page="today"\] #stats, body\[data-page="today"\] #asks \{ display:none; \}')
        self.assertIn("document.body.dataset.page = tab;", self.function("showTab"))
        self.assertNotIn("body.dataset.tab", self.page)  # a click anywhere would read as a click on a tab

    def test_the_meadow_stays_folded_under_today(self):
        today = self.page.split('<section data-panel="today"')[1].split("</section>")[0]
        self.assertIn('<details id="meadowbox"', today)
        self.assertIn('id="scene"', today.split('<details id="meadowbox"')[1])
        self.assertIn('if (tab !== "today" || !$("meadowbox").open) return;', self.function("renderScene"))

    def test_today_is_drawn_whenever_the_state_the_queue_or_the_events_change(self):
        self.assertIn("renderToday();", self.function("refreshState"))
        self.assertIn("renderToday();", self.function("refreshQueue"))
        self.assertIn("renderToday();", self.function("loadLive"))

    def test_the_new_words_read_in_both_languages(self):
        new = ("stage_shape", "stage_ready", "stage_build", "stage_review", "stage_merge", "stage_live", "dot_you", "dot_stuck",
               "dot_bee", "dot_turn", "flight_paths", "today_events", "today_none", "today_all_clear", "wait_place", "kind_queen",
               "kind_check", "kind_blocker", "split_waits", "ask_didban", "health_bees", "health_machine", "brief_tokens",
               "meadow", "previous", "next")
        for key in new:
            self.assertTrue(self.text["en"].get(key), key)
            self.assertTrue(self.text["fa"].get(key), key)

    def test_nothing_on_today_pushes_a_phone_page_sideways(self):
        self.assertRegex(self.style, r"\.lane-track \{[^}]*grid-template-columns:repeat\(6, minmax\(0, 1fr\)\)")
        self.assertRegex(self.style, r"\.today-grid > \* \{[^}]*min-width:0")
        self.assertRegex(self.style, r"\.health \{[^}]*flex-wrap:wrap")

    def test_design_describes_today(self):
        design = (ROOT / "DESIGN.md").read_text(encoding="utf-8").split("## 7. Dashboard")[1].split("\n## 8.")[0]
        today = next(line for line in design.splitlines() if line.startswith("- **Today:**"))
        for part in ("Waits for you", "Flight paths", "Shape, Ready, Build, Review, Merge and Live", "brief", "hive", "meadow"):
            self.assertIn(part, today)


if __name__ == "__main__":
    unittest.main()
