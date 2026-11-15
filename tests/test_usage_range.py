import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from kandu.config import load_config
from kandu.dashboard import Api, dispatch
from kandu.state import StateStore

ROOT = Path(__file__).resolve().parent.parent
REPO = "acme-org/website"
TODAY = "2026-11-04"  # a Wednesday


def tokens(input_=0, output=0, cache_read=0, cache_creation=0, model="opus"):
    return {model: {"input": input_, "output": output, "cache_read": cache_read, "cache_creation": cache_creation}}


class UsageTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        local = self.root / "local"
        local.mkdir()
        shutil.copy(ROOT / "config.example" / "kandu.example.yaml", local / "kandu.yaml")
        shutil.copy(ROOT / "config.example" / "projects.example.yaml", local / "projects.yaml")
        self.store = StateStore(self.root / "state")
        self.store.today = lambda: TODAY
        self.api = Api(load_config(local), self.store, None, self.root / "logs", "token", timer_on=lambda: True,
                       local_dir=local)

    def run_on(self, day, task, role="worker", project="website", used=None):
        now = datetime.fromisoformat(f"{day}T12:00:00").astimezone()
        self.store.record("run_finished", now=now, task=task, project=project, run_id="r-1", role=role,
                          models=used or tokens(100, 50), duration_ms=1, is_error=False)

    def usage(self, query=""):
        status, body = dispatch(self.api, "GET", f"/api/v1/usage{query}")
        self.assertEqual(status, 200, body)
        return body

    def test_projects_hold_their_tasks_most_first_and_the_rows_add_up(self):
        self.run_on(TODAY, f"{REPO}#1")
        self.run_on(TODAY, f"{REPO}#2", used=tokens(400, 100))
        self.run_on(TODAY, "acme-org/internal-tool#3", project="internal-tool", used=tokens(10, 5))
        self.run_on(TODAY, None, role="queen", project=None, used=tokens(7, 3))
        self.run_on(TODAY, f"{REPO}#4", role="splitter", used=tokens(20, 0))
        now = datetime.fromisoformat(f"{TODAY}T13:00:00").astimezone()
        self.store.record("task_finished", now=now, task=f"{REPO}#2", project="website", outcome="merged")
        body = self.usage()
        self.assertEqual((body["from"], body["to"]), (TODAY, TODAY))
        self.assertEqual([p["project"] for p in body["projects"]], ["website", "internal-tool"])
        website = body["projects"][0]
        self.assertEqual([(t["task"], t["total"], t["outcome"]) for t in website["tasks"]],
                         [(f"{REPO}#2", 500, "merged"), (f"{REPO}#1", 150, None)])
        self.assertEqual(body["kandu"], 30)  # the queen and the sizing
        self.assertEqual(body["total"], sum(p["total"] for p in body["projects"]) + body["kandu"] + body["sessions"])
        self.assertEqual(body["roles"]["splitter"], 20)

    def test_cache_reads_do_not_count_as_the_daily_limit_does_not_count_them(self):
        self.run_on(TODAY, f"{REPO}#1", used=tokens(10, 10, cache_read=1000, cache_creation=5))
        self.assertEqual(self.usage()["total"], 25)

    def test_today_carries_each_projects_limit_and_what_counts_against_it(self):
        self.run_on(TODAY, f"{REPO}#1")
        self.run_on(TODAY, f"{REPO}#4", role="splitter", used=tokens(20, 0))
        website = self.usage()["projects"][0]
        self.assertEqual(website["limit"], self.api.config.projects[0].daily_token_limit)
        self.assertEqual(website["counted"], 170)  # the limit counts its sizing too
        self.assertIsNone(self.usage("?range=week")["projects"][0]["limit"])

    def test_the_named_ranges(self):
        for name, first, last in (("week", "2026-11-02", "2026-11-08"), ("last7", "2026-10-29", TODAY),
                                  ("month", "2026-11-01", "2026-11-30"), ("today", TODAY, TODAY)):
            body = self.usage(f"?range={name}")
            self.assertEqual((body["from"], body["to"]), (first, last), name)

    def test_any_range_counts_only_its_days(self):
        self.run_on("2026-10-01", f"{REPO}#1")
        self.run_on("2026-10-31", f"{REPO}#1")
        self.run_on("2026-11-01", f"{REPO}#1")
        self.assertEqual(self.usage("?from=2026-10-01&to=2026-10-31")["total"], 300)
        self.assertEqual(self.usage("?from=2026-11-01&to=2026-11-01")["total"], 150)

    def test_sessions_count_on_the_day_they_started(self):
        self.store.write_didban_session({"id": "a", "started": f"{TODAY}T09:00:00Z",
                                         "tokens": {"input": 5, "output": 5, "cache_read": 900, "cache_creation": 0}})
        self.store.write_flown({"id": "b", "started": f"{TODAY}T10:00:00Z", "task": f"{REPO}#1", "project": "website",
                                "tokens": {"input": 1, "output": 2, "cache_read": 0, "cache_creation": 0}})
        self.store.write_flown({"id": "c", "started": "2026-10-01T10:00:00Z", "task": None, "project": None,
                                "tokens": {"input": 100, "output": 0, "cache_read": 0, "cache_creation": 0}})
        body = self.usage()
        self.assertEqual((body["sessions"], body["total"]), (13, 13))
        self.assertEqual((body["roles"]["didban"], body["roles"]["flight"]), (10, 3))
        self.assertEqual(body["projects"], [])

    def test_a_bad_range_is_a_400(self):
        for query in ("?range=year", "?from=2026-11-05&to=2026-11-01", "?from=yesterday&to=2026-11-01", "?from=2026-11-01"):
            self.assertEqual(dispatch(self.api, "GET", f"/api/v1/usage{query}")[0], 400, query)


class PageTest(unittest.TestCase):
    def test_the_days_tab_has_the_range_picker_and_its_words_in_both_languages(self):
        page = (ROOT / "kandu" / "dashboard.html").read_text(encoding="utf-8")
        days = page.split('data-panel="days"', 1)[1].split("</section>", 1)[0]
        for part in ('id="usagerange"', 'value="week"', 'value="last7"', 'value="month"', 'value="custom"',
                     'id="usagefrom" type="date" dir="ltr"', 'id="usage"'):
            self.assertIn(part, days)
        for key in ("usage_title", "range_today", "range_week", "range_last7", "range_month", "range_custom",
                    "kandu_itself", "usage_sessions", "of_limit", "no_usage"):
            self.assertEqual(page.count(f'"{key}": '), 2, key)
        self.assertIn("refreshUsage().catch", page.split("async function refreshDays()", 1)[1][:200])


if __name__ == "__main__":
    unittest.main()
