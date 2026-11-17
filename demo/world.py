"""The demo world: a made-up GitHub and a fixed story, played on the real dashboard (demo/README.md).

Every event goes through Kandu's own `StateStore`, with the fields the supervisor writes, so the page shows
exactly what it would show for a real run. Nothing here reaches GitHub or Claude.
"""
import json
import threading
import time
from datetime import datetime, timedelta, timezone

from kandu.github import AGENT_LABEL, BoardItem
from kandu.review import comment_for

ACCOUNT = "kandu-demo"
NOTES, TEA = "demo-org/pocket-notes", "demo-org/tea-shop"
BOARD_OF = {NOTES: 1, TEA: 2}
PROJECT_OF = {NOTES: "pocket-notes", TEA: "tea-shop"}
MACHINE = "hive-box"
WORKER_MODEL, SMALL_MODEL = "claude-opus-5-5", "claude-sonnet-5-5"

# What Kandu says to the user is in the config's language; GitHub stays in English.
TEXT = {
    "en": {
        "queen_yesterday": "Quiet day. Two tasks merged in pocket-notes; tea-shop waits for your yes on a one-way door.",
        "queen_release": "Released pocket-notes #21 and #18: both Small and easy to undo, and the project has room. "
                         "tea-shop #7 still waits for your yes: it changes the orders table.",
        "brief_21_r1": "The sort works, but notes with the same date have no test, so their order is not shown.",
        "brief_21_r2": "The missing test is added and passes. Ready to merge.",
        "brief_7": "Adds a delivery window to each order. The migration adds a column with a default, so old orders "
                   "keep working, but it cannot be undone without a second migration.",
        "brief_12": "Shows a word count under each note. Tests cover empty and long notes.",
        "brief_14": "Keeps an unsaved draft when the page reloads.",
        "brief_5": "Shows the price per 100 g on every product.",
    },
    "fa": {
        "queen_yesterday": "روز آرامی بود. دو تسک در pocket-notes مرج شد؛ tea-shop منتظر تأیید شما برای یک تغییر سخت‌برگشت است.",
        "queen_release": "تسک‌های ۲۱ و ۱۸ از pocket-notes را آزاد کردم: هر دو کوچک‌اند و برگشت‌پذیر، و پروژه جا دارد. "
                         "تسک ۷ از tea-shop هنوز منتظر تأیید شماست: جدول سفارش‌ها را عوض می‌کند.",
        "brief_21_r1": "مرتب‌سازی کار می‌کند، ولی برای یادداشت‌هایی که تاریخشان یکی است تستی نیست، پس ترتیبشان نشان داده نشده.",
        "brief_21_r2": "تست جاافتاده اضافه شده و قبول می‌شود. آماده‌ی مرج است.",
        "brief_7": "به هر سفارش یک بازه‌ی تحویل اضافه می‌کند. migration یک ستون با مقدار پیش‌فرض اضافه می‌کند، پس "
                   "سفارش‌های قدیمی کار می‌کنند، ولی برگرداندنش یک migration دیگر می‌خواهد.",
        "brief_12": "زیر هر یادداشت تعداد کلمه‌ها را نشان می‌دهد. تست‌ها یادداشت خالی و بلند را پوشش می‌دهند.",
        "brief_14": "پیش‌نویس ذخیره‌نشده بعد از بارگذاری دوباره‌ی صفحه می‌ماند.",
        "brief_5": "قیمت هر ۱۰۰ گرم را روی همه‌ی محصولات نشان می‌دهد.",
    },
}

ISSUE_21 = """## Goal
Readers can sort the notes list by date, newest or oldest first.

## Acceptance criteria
1. A "Sort" control above the list offers "Newest first" and "Oldest first".
2. The chosen order stays after the page reloads.
3. Notes with the same date keep the order they were created in.

## Out of scope
Sorting by title or by tag."""

WORKER_STEPS = [
    ("Read", "src/notes/list.ts"), ("Read", "src/notes/store.ts"), ("Grep", "createdAt"),
    ("Read", "tests/notes/list.test.ts"), ("Edit", "tests/notes/list.test.ts"), ("Bash", "npm test -- list"),
    ("Edit", "src/notes/sort.ts"), ("Edit", "src/notes/list.ts"), ("Edit", "src/settings/prefs.ts"),
    ("Bash", "npm test"), ("Bash", "git commit -m \"Sort the notes list by date\""),
    ("Bash", "git push -u origin kandu/21-sort-the-notes-list-by-date"),
]
FIX_STEPS = [("Read", "tests/notes/list.test.ts"), ("Edit", "tests/notes/list.test.ts"), ("Bash", "npm test"),
             ("Bash", "git commit -m \"Test notes with the same date\""), ("Bash", "git push")]
OTHER_STEPS = [("Read", "src/search/index.ts"), ("Grep", "tags"), ("Read", "tests/search.test.ts"),
               ("Edit", "tests/search.test.ts"), ("Bash", "npm test -- search"), ("Edit", "src/search/index.ts")]


def now():
    return datetime.now().astimezone()


def iso(moment):
    return moment.isoformat(timespec="seconds")


def tokens(input_, output, cache_read, model=WORKER_MODEL):
    return {model: {"input": input_, "output": output, "cache_read": cache_read, "cache_creation": cache_read // 8}}


class FakeGitHub:
    """The few reads the dashboard makes, answered from memory."""

    def __init__(self):
        self.items = {}
        self.prs = {}
        self.version = 0
        self.lock = threading.Lock()

    def add(self, repo, number, title, status, body="", closed_ago=None):
        closed = iso((now() - closed_ago).astimezone(timezone.utc)) if closed_ago is not None else ""
        self.items[(repo, number)] = BoardItem(repo=repo, number=number, title=title, status=status,
                                               url=f"https://github.com/{repo}/issues/{number}", labels=[AGENT_LABEL],
                                               body=body, closed_at=closed)
        self.version += 1

    def move(self, repo, number, status):
        item = self.items[(repo, number)]
        item.status = status
        if status == "Done":
            item.closed_at = iso(now().astimezone(timezone.utc))
        self.version += 1

    def open_pr(self, repo, issue, number, title, slug, head):
        self.prs[(repo, issue)] = {"number": number, "title": title, "url": f"https://github.com/{repo}/pull/{number}",
                                   "state": "OPEN", "headRefOid": head, "headRefName": f"kandu/{issue}-{slug}",
                                   "comments": []}

    def comment(self, repo, issue, body):
        self.prs[(repo, issue)]["comments"].append({"author": {"login": ACCOUNT}, "body": body})

    def board_items(self, board):
        with self.lock:
            return [i for i in self.items.values() if BOARD_OF[i.repo] == board.number]

    def board_version(self, board):
        return self.version

    def find_pr(self, repo, number):
        return self.prs.get((repo, number))

    def issue_text(self, repo, number):
        item = self.items[(repo, number)]
        return {"title": item.title, "body": item.body, "url": item.url}


class World:
    def __init__(self, store, logs_dir, lang):
        self.store = store
        self.logs = logs_dir
        self.text = TEXT[lang]
        self.github = FakeGitHub()
        self.workers = {}
        self.queen_run = None
        self.usage = {"session": 18, "week": 27}
        self.runs = 0
        self.wakes = []  # what each press of a cycle button sets going, in order

    # Plumbing, as the supervisor does it.

    def record(self, kind, at=None, **fields):
        self.store.record(kind, now=at, **fields)

    def snapshot(self, supervisor="awake", reason=None):
        self.store.write_current({"supervisor": supervisor, "last_reason": reason,
                                  "next_wake": iso(now() + timedelta(minutes=52)), "usage": self.usage,
                                  "workers": list(self.workers.values()), "waiting_for_machine": [],
                                  "queen_run": self.queen_run})

    def run_id(self):
        self.runs += 1
        return f"r-demo-{self.runs}"

    def phase(self, repo, number, phase, run_id=None):
        task = f"{repo}#{number}"
        self.record("phase_changed", task=task, project=PROJECT_OF[repo], phase=phase)
        worker = self.workers.setdefault(task, {"task": task, "project": PROJECT_OF[repo], "machine": MACHINE,
                                                "started": iso(now())})
        worker["phase"] = phase
        if run_id:
            worker["run_id"] = run_id
        self.snapshot()

    def leave(self, repo, number):
        self.workers.pop(f"{repo}#{number}", None)
        self.snapshot()

    def finished(self, repo, number, role, run_id, models, minutes, at=None):
        self.record("run_finished", at=at, task=f"{repo}#{number}" if number else None,
                    project=PROJECT_OF[repo] if repo else None, run_id=run_id, role=role, models=models,
                    duration_ms=int(minutes * 60000), is_error=False)

    def play_log(self, run_id, steps, every):
        """Writes a run's stream as an agent would, one tool call at a time, so its window fills live."""
        path = self.logs / f"{run_id}.jsonl"

        def write():
            for tool, detail in steps:
                key = "command" if tool == "Bash" else "pattern" if tool == "Grep" else "file_path"
                line = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": tool,
                                                                       "input": {key: detail}}]}}
                with open(path, "a", encoding="utf-8") as log:
                    log.write(json.dumps(line) + "\n")
                time.sleep(every)
        path.touch()
        thread = threading.Thread(target=write, daemon=True)
        thread.start()
        return thread

    def review_comment(self, repo, issue, round_, head, brief, findings, door, criteria, second=False):
        report = {"head_sha": head, "door": door, "summary": brief, "brief": brief,
                  "blast_radius": "the notes list" if repo == NOTES else "the orders table and checkout",
                  "criteria": [{"criterion": c, "result": "met"} for c in criteria], "wait_for_user": []}
        self.github.comment(repo, issue, comment_for(report, round_, findings, second=second))

    def reviewed(self, repo, number, run_id, round_, head, findings, blockers, door, user_brief, at=None):
        self.record("review_finished", at=at, task=f"{repo}#{number}", project=PROJECT_OF[repo], run_id=run_id,
                    round=round_, head_sha=head, findings=findings, blockers=blockers, door=door,
                    criteria_met=blockers == 0, wait_for_user=[], user_brief=user_brief)

    def sized(self, repo, number, size, files, door, at=None):
        self.record("task_sized", at=at, task=f"{repo}#{number}", project=PROJECT_OF[repo], run_id=None, size=size,
                    files=files, door=door, criteria=3, slices=0)

    # The world before the film starts: yesterday's work, so the comb and the daily usage are not empty.

    def seed(self):
        gh, day = self.github, now() - timedelta(days=1)
        at = lambda hours: day.replace(hour=9, minute=0) + timedelta(hours=hours)
        gh.add(NOTES, 12, "Show a word count under each note", "Done", closed_ago=timedelta(hours=20))
        gh.add(NOTES, 14, "Keep a draft when the page reloads", "Done", closed_ago=timedelta(hours=17))
        gh.add(NOTES, 18, "Search notes by tag", "Backlog")
        gh.add(NOTES, 19, "Export notes as Markdown", "Backlog")
        gh.add(NOTES, 16, "Tidy the settings page", "Technical debt")
        gh.add(TEA, 5, "Show the price per 100 g", "Done", closed_ago=timedelta(hours=22))
        gh.add(TEA, 7, "Store each order's delivery window", "In review")
        gh.add(TEA, 8, "Offer gift cards", "Needs info")
        gh.add(TEA, 9, "Show stock on the product page", "Backlog")
        gh.add(TEA, 10, "Group teas by region", "Backlog")
        gh.add(TEA, 11, "Rename the cart's labels", "Technical debt")

        self.record("supervisor_woke", at=at(0))
        for repo, number, size, files in ((NOTES, 12, "Small", 4), (NOTES, 14, "Small", 5), (NOTES, 18, "Small", 6),
                                          (NOTES, 19, "Medium", 11), (TEA, 5, "Small", 3), (TEA, 7, "Medium", 9),
                                          (TEA, 9, "Small", 5), (TEA, 10, "Medium", 12)):
            self.sized(repo, number, size, files, "one-way" if (repo, number) == (TEA, 7) else "two-way", at=at(0))
            self.finished(repo, number, "splitter", self.run_id(), tokens(1800, 900, 21000, SMALL_MODEL), 1, at=at(0))
        queen = self.run_id()
        self.finished(None, None, "queen", queen, tokens(4200, 1300, 38000), 1, at=at(0.1))
        self.record("queen_finished", at=at(0.1), run_id=queen, brief=self.text["queen_yesterday"], backlog=[])
        for hours, (repo, number, brief, head) in enumerate(((NOTES, 12, "brief_12", "c41e9a2"),
                                                              (NOTES, 14, "brief_14", "d07b3f1"),
                                                              (TEA, 5, "brief_5", "e5a90c4")), start=1):
            task, project = f"{repo}#{number}", PROJECT_OF[repo]
            self.record("task_picked", at=at(hours), task=task, project=project)
            self.finished(repo, number, "worker", self.run_id(), tokens(9000, 31000, 610000), 14, at=at(hours))
            review = self.run_id()
            self.finished(repo, number, "reviewer", review, tokens(6000, 9000, 240000), 6, at=at(hours + .3))
            self.reviewed(repo, number, review, 1, head, 0, 0, "two-way", self.text[brief], at=at(hours + .3))
            self.finished(repo, number, "live_check", self.run_id(), tokens(1500, 700, 30000, SMALL_MODEL), 2,
                          at=at(hours + .5))
            self.record("task_finished", at=at(hours + .5), task=task, project=project, outcome="merged")
        # tea-shop #7 was reviewed and waits for the user: a one-way door.
        task = f"{TEA}#7"
        self.record("task_picked", at=at(5), task=task, project="tea-shop")
        self.finished(TEA, 7, "worker", self.run_id(), tokens(12000, 44000, 820000), 22, at=at(5))
        review = self.run_id()
        gh.open_pr(TEA, 7, 34, "Store each order's delivery window", "store-each-order-s-delivery-window", "9f3c2d8")
        self.review_comment(TEA, 7, 1, "9f3c2d8", "Adds a delivery window to each order, with a migration.", [],
                            "one-way", ["Each order stores a delivery window", "Old orders still open",
                                        "Checkout asks for the window"])
        self.finished(TEA, 7, "reviewer", review, tokens(7000, 11000, 300000), 8, at=at(5.5))
        self.reviewed(TEA, 7, review, 1, "9f3c2d8", 0, 0, "one-way", self.text["brief_7"], at=at(5.5))
        self.record("decision", at=at(5.6), task=task, project="tea-shop", decision="merge_waits",
                    reason="a one-way door waits for the user")
        self.record("supervisor_slept", at=at(6), reason="nothing ready", next_wake=iso(at(7)))
        self.store.write_current({"supervisor": "asleep", "last_reason": "nothing ready",
                                  "next_wake": iso(now() + timedelta(minutes=41)), "usage": self.usage,
                                  "workers": [], "waiting_for_machine": [], "queen_run": None})

    # The story, scene by scene. Each step returns at once; the film waits between them.

    def new_task(self):
        """Scene 2 ends: Didban created the task on the board."""
        self.github.add(NOTES, 21, "Sort the notes list by date", "Backlog", body=ISSUE_21)

    def wake(self):
        """Scene 3: the user pressed Wake the queen."""
        self.record("supervisor_woke")
        self.usage = {"session": 21, "week": 28}
        self.sizer = self.run_id()
        self.phase(NOTES, 21, "sizing", self.sizer)

    def size(self):
        self.sized(NOTES, 21, "Small", 5, "two-way")
        self.finished(NOTES, 21, "splitter", self.sizer, tokens(2000, 1100, 26000, SMALL_MODEL), 1)
        self.leave(NOTES, 21)
        self.queen_run = self.run_id()
        self.snapshot()

    def release(self):
        for number in (21, 18):
            self.record("queen_decided", task=f"{NOTES}#{number}", project="pocket-notes", action="release",
                        note="Small, two-way, and the project has room")
            self.github.move(NOTES, number, "Ready")
        self.finished(None, None, "queen", self.queen_run, tokens(5100, 1500, 41000), 1)
        self.record("queen_finished", run_id=self.queen_run, brief=self.text["queen_release"],
                    backlog=[f"{NOTES}#19", f"{TEA}#9", f"{TEA}#10"])
        self.queen_run = None
        self.snapshot()

    def build(self):
        """Scene 4: two workers start."""
        self.worker_21, self.worker_18 = self.run_id(), self.run_id()
        for number, run in ((21, self.worker_21), (18, self.worker_18)):
            self.record("task_picked", task=f"{NOTES}#{number}", project="pocket-notes")
            self.github.move(NOTES, number, "In progress")
            self.phase(NOTES, number, "implement", run)
        self.play_log(self.worker_21, WORKER_STEPS, 1.1)
        self.play_log(self.worker_18, OTHER_STEPS, 2.5)
        self.usage = {"session": 26, "week": 29}

    def review(self):
        """Scene 5: the PR is opened and the reviewer finds a missing test."""
        self.finished(NOTES, 21, "worker", self.worker_21, tokens(8000, 27000, 540000), 12)
        self.github.open_pr(NOTES, 21, 41, "Sort the notes list by date", "sort-the-notes-list-by-date", "a1b2c3d")
        self.github.move(NOTES, 21, "In review")
        self.review_1 = self.run_id()
        self.phase(NOTES, 21, "review round 1", self.review_1)
        self.play_log(self.review_1, [("Read", "src/notes/sort.ts"), ("Grep", "sortNotes"),
                                      ("Bash", "npm test"), ("Read", "tests/notes/list.test.ts")], 1.0)

    def blocker(self):
        finding = {"n": 1, "blocker": True, "kind": "untested", "path": "tests/notes/list.test.ts", "line": None,
                   "criterion": 3, "body": "Criterion 3 is not shown: no test has two notes with the same date, "
                                           "so their order after sorting is never checked."}
        criteria = ["A Sort control offers newest and oldest first", "The order stays after a reload",
                    "Notes with the same date keep their created order"]
        self.review_comment(NOTES, 21, 1, "a1b2c3d", "The sort works, but notes with the same date have no test.",
                            [finding], "two-way", criteria)
        self.finished(NOTES, 21, "reviewer", self.review_1, tokens(6000, 8500, 230000), 5)
        self.reviewed(NOTES, 21, self.review_1, 1, "a1b2c3d", 1, 1, "two-way", self.text["brief_21_r1"])
        self.fix = self.run_id()
        self.phase(NOTES, 21, "fix round 1", self.fix)
        self.play_log(self.fix, FIX_STEPS, 1.0)

    def second_review(self):
        self.finished(NOTES, 21, "worker", self.fix, tokens(3000, 6000, 160000), 4)
        self.github.prs[(NOTES, 21)]["headRefOid"] = "b7e8f90"
        self.review_2 = self.run_id()
        self.phase(NOTES, 21, "review round 2", self.review_2)

    def approve_21(self):
        criteria = ["A Sort control offers newest and oldest first", "The order stays after a reload",
                    "Notes with the same date keep their created order"]
        self.review_comment(NOTES, 21, 2, "b7e8f90", "The missing test is added and passes.", [], "two-way",
                            criteria, second=True)
        self.finished(NOTES, 21, "reviewer", self.review_2, tokens(3000, 3500, 120000), 3)
        self.reviewed(NOTES, 21, self.review_2, 2, "b7e8f90", 0, 0, "two-way", self.text["brief_21_r2"])
        self.phase(NOTES, 21, "merge")

    def ship_21(self):
        """Scene 6: merge, deploy, live check, done."""
        self.record("decision", task=f"{NOTES}#21", project="pocket-notes", decision="merged",
                    reason="every merge condition holds on b7e8f90")
        self.github.prs[(NOTES, 21)]["state"] = "MERGED"
        self.phase(NOTES, 21, "deploy")
        self.record("deployed", task=f"{NOTES}#21", project="pocket-notes", run_id=None)
        self.live_21 = self.run_id()
        self.phase(NOTES, 21, "live check", self.live_21)

    def done_21(self):
        self.finished(NOTES, 21, "live_check", self.live_21, tokens(1400, 600, 28000, SMALL_MODEL), 2)
        self.record("task_finished", task=f"{NOTES}#21", project="pocket-notes", outcome="merged")
        self.github.move(NOTES, 21, "Done")
        self.leave(NOTES, 21)
        self.usage = {"session": 31, "week": 30}
        self.snapshot()

    def ship_7(self):
        """Scene 7: the user's yes on tea-shop #7 starts a cycle that merges it."""
        self.record("supervisor_woke")
        self.phase(TEA, 7, "merge")

    def deploy_7(self):
        self.record("decision", task=f"{TEA}#7", project="tea-shop", decision="merged",
                    reason="the user approved 9f3c2d8")
        self.github.prs[(TEA, 7)]["state"] = "MERGED"
        self.phase(TEA, 7, "deploy")
        self.record("deployed", task=f"{TEA}#7", project="tea-shop", run_id=None)
        self.live_7 = self.run_id()
        self.phase(TEA, 7, "live check", self.live_7)

    def done_7(self):
        self.finished(TEA, 7, "live_check", self.live_7, tokens(1500, 700, 31000, SMALL_MODEL), 2)
        self.record("task_finished", task=f"{TEA}#7", project="tea-shop", outcome="merged")
        self.github.move(TEA, 7, "Done")
        self.leave(TEA, 7)
