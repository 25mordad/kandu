"""Counts what each part of Kandu asks of GitHub, to find what spends the hourly limit.

Every `gh` that Kandu or one of its agents runs goes through `meter/gh`, which writes one
line per call in `state/gh/<day>.jsonl`. Beside the calls, the real limit is sampled now and
then: the points spent that no counted call explains came from outside Kandu.

    .venv/bin/python -m kandu.ghmeter [--day YYYY-MM-DD]
"""
import argparse
import json
import os
import subprocess
import sys
import threading
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

from . import compat
from .state import DEFAULT_STATE_DIR, StateStore

METER_DIR = Path(__file__).resolve().parent.parent / "meter"
METER = METER_DIR / "gh"
WINDOWS_METER = METER_DIR / "gh.py"
LOG_DIR = DEFAULT_STATE_DIR / "gh"
CALLER_ENV = "GH_METER_CALLER"
RUN_ENV = "GH_METER_RUN"
LOG_ENV = "GH_METER_LOG"
SAMPLE_CALLER = "meter"
SAMPLE_EVERY_SECONDS = 300
SAMPLE_TIMEOUT_SECONDS = 20


def caller():
    """Who this process is: what started it, or the name of its module."""
    return os.environ.get(CALLER_ENV) or Path(sys.argv[0]).stem or "other"


def env(name=None, run=None, base=None):
    """An environment whose `gh` is the meter, naming the caller and the run."""
    base = dict(os.environ if base is None else base)
    base["PATH"] = f"{METER_DIR}{os.pathsep}{base.get('PATH', '')}"
    base[CALLER_ENV] = name or base.get(CALLER_ENV) or caller()
    base[LOG_ENV] = str(LOG_DIR)
    if run:
        base[RUN_ENV] = run
    return base


def gh(*args):
    """The argv of one `gh` call that the meter counts.

    On Linux `gh` on the meter's PATH is the meter. Windows starts only `.exe` files by name, so
    there the call goes to the meter's Python version by its path.
    """
    if compat.WINDOWS:
        return [sys.executable, "-I", str(WINDOWS_METER), *args]
    return ["gh", *args]


def path_line():
    """A shell line that puts the meter first, for scripts that set PATH themselves."""
    return f'export PATH="{compat.posix_path(METER_DIR)}:$HOME/.local/bin:$PATH"'


def sample(log_dir=LOG_DIR, run=subprocess.run, now=None):
    """Writes the account's GraphQL and REST use as GitHub reports it. That call costs nothing."""
    try:
        result = run(gh("api", "rate_limit"), capture_output=True, text=True, encoding="utf-8", timeout=SAMPLE_TIMEOUT_SECONDS,
                     env=env(SAMPLE_CALLER))
        resources = json.loads(result.stdout)["resources"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None
    now = now or datetime.now().astimezone()
    line = {"v": 1, "ts": now.isoformat(timespec="seconds"), "type": "limit",
            "graphql_used": resources["graphql"]["used"], "graphql_reset": resources["graphql"]["reset"],
            "rest_used": resources["core"]["used"], "rest_reset": resources["core"]["reset"]}
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(log_dir / f"{now.date().isoformat()}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    return line


def sample_forever(stop=None, every=SAMPLE_EVERY_SECONDS):
    """Samples in a background thread until `stop` is set."""
    stop = stop or threading.Event()

    def loop():
        while not stop.is_set():
            sample()
            stop.wait(every)
    threading.Thread(target=loop, daemon=True, name="ghmeter").start()
    return stop


def read(day, log_dir=LOG_DIR):
    path = log_dir / f"{day}.jsonl"
    if not path.exists():
        return []
    lines = []
    for text in path.read_text(encoding="utf-8").split("\n"):
        try:
            lines.append(json.loads(text))
        except ValueError:
            continue  # a cut line
    return lines


def spent_by_hour(limits):
    """GraphQL points spent between samples, by the hour of the later sample."""
    spent = Counter()
    for before, after in zip(limits, limits[1:]):
        same_window = after["graphql_reset"] == before["graphql_reset"]
        spent[after["ts"][11:13]] += after["graphql_used"] - before["graphql_used"] if same_window else after["graphql_used"]
    return spent


def runs_by_id(store, day):
    return {e["run_id"]: e for e in store.events(day) if e.get("type") == "run_finished" and e.get("run_id")}


def who(call, runs):
    run = runs.get(call.get("run"))
    if run is None:
        return call["caller"] + (f" {call['run']}" if call.get("run") else "")
    return f"{run.get('role')} {run.get('task') or run.get('project') or ''}".strip()


def report(day, log_dir=LOG_DIR, store=None):
    store = store or StateStore()
    lines = read(day, log_dir)
    calls = [line for line in lines if line.get("type") == "call" and line.get("caller") != SAMPLE_CALLER]
    limits = sorted((line for line in lines if line.get("type") == "limit"), key=lambda line: line["ts"])
    graphql = [c for c in calls if c.get("kind") == "graphql"]
    runs = runs_by_id(store, day)
    out = [f"GitHub use on {day}: {len(graphql)} GraphQL calls and {len(calls) - len(graphql)} REST calls counted "
           f"from Kandu, {sum(1 for c in calls if c.get('exit'))} failed."]

    spent = spent_by_hour(limits)
    counted = Counter(c["ts"][11:13] for c in graphql)
    if spent:
        out += ["", "Hour  GraphQL points spent  calls counted  not from Kandu"]
        for hour in sorted(set(spent) | set(counted)):
            out.append(f"{hour}:00 {spent[hour]:>20} {counted[hour]:>14} {max(spent[hour] - counted[hour], 0):>15}")
    else:
        out += ["", "No samples of the limit yet: the dashboard writes one every few minutes."]

    sections = [("By caller", Counter(who(c, runs) for c in graphql)),
                ("By command", Counter(c["command"] for c in graphql))]
    for title, counter in sections:
        out += ["", f"{title} (GraphQL calls)"]
        out += [f"{count:>6}  {name}" for name, count in counter.most_common(15)]
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description="What spends GitHub's hourly limit.")
    parser.add_argument("--day", default=date.today().isoformat(), help="YYYY-MM-DD, default today")
    parser.add_argument("--sample", action="store_true", help="sample the limit once, then report")
    args = parser.parse_args()
    if args.sample:
        sample()
    print(report(args.day))
    return 0


if __name__ == "__main__":
    sys.exit(main())
