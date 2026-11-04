"""Reads GitHub into Kandu's copy, then sends the queue (DESIGN §3). Its timer runs it every few minutes.

    .venv/bin/python -m kandu.sync            # read everything, then send what waits
    .venv/bin/python -m kandu.sync --read     # read only
    .venv/bin/python -m kandu.sync --queue    # show what waits, and what was dropped lately
"""
import argparse
import json
import sys

from .config import DEFAULT_LOCAL_DIR, ConfigError, load_config
from .mirror import MIRROR_DIR, open_github

DROPPED_SHOWN = 10


def sync(config, mirror):
    """Reads first, then sends the queue, which wins over what changed on GitHub meanwhile. Returns a line to print."""
    failed = mirror.refresh(config, max_age=0)
    sent, kept, dropped = mirror.send()
    line = f"sync: sent {sent}, waiting {kept}, dropped {dropped}"
    return "\n".join([line, *[f"  could not read {f}" for f in failed]])


def show_queue(mirror):
    lines = [f"{len(mirror.outbox())} change(s) wait:"]
    for entry in mirror.outbox():
        target = entry.get("url") or f"{entry['repo']}#{entry['number']}"
        what = f"{entry['field']} -> {entry['value']}" if entry["kind"] == "card" else entry["kind"]
        lines.append(f"  {entry['queued_at']} {target} {what} (tries {entry['tries']})")
    try:
        dropped = (MIRROR_DIR / "dropped.jsonl").read_text(encoding="utf-8").splitlines()[-DROPPED_SHOWN:]
    except OSError:
        dropped = []
    if dropped:
        lines.append("Dropped lately:")
        lines += [f"  {d['at']} {d['change'].get('url') or d['change'].get('repo')}: {d['why']}"
                  for d in map(json.loads, dropped)]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Read GitHub into Kandu's copy, then send the queue.")
    parser.add_argument("--read", action="store_true", help="read only; send nothing")
    parser.add_argument("--queue", action="store_true", help="show the queue and what was dropped")
    parser.add_argument("--local", default=DEFAULT_LOCAL_DIR, help="config directory (default: local/)")
    args = parser.parse_args()
    try:
        config = load_config(args.local)
    except ConfigError as e:
        print(f"sync: {len(e.problems)} problem(s) in config; run: python -m kandu.config", file=sys.stderr)
        return 1
    mirror = open_github(config)
    if args.queue:
        print(show_queue(mirror))
    elif args.read:
        print("\n".join(mirror.refresh(config, max_age=0)) or "sync: read")
    else:
        print(sync(config, mirror))
    return 0


if __name__ == "__main__":
    sys.exit(main())
