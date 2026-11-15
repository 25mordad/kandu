"""Keeps every Kandu process from calling GitHub while GitHub refuses it for a rate limit (DESIGN §3).

When GitHub answers with a rate limit, primary or secondary, the first process that hears it
writes `state/gh/backoff.json`: until when no call goes out, and why. Every call through
`github.run_gh` looks there first, so a retry, in any process, waits instead of keeping the
block going. The wait is the reset GitHub gives for a spent limit; when it gives none, as for a
secondary limit, it starts at a minute and doubles with each refusal in a row, up to an hour.
The first call that goes through clears it.

`meter/gh` keeps the same wait for every `gh` an agent runs: it waits before calling while it is in
force, and records a refusal with

    python -m kandu.ghlimit refused <backoff.json> < gh's stderr
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import ghmeter
from .state import DEFAULT_STATE_DIR

GATE_PATH = DEFAULT_STATE_DIR / "gh" / "backoff.json"
FIRST_WAIT_SECONDS = 60
LONGEST_WAIT_SECONDS = 3600
LOOKUP_TIMEOUT_SECONDS = 20
WHY_LENGTH = 200
REFUSAL = re.compile(r"rate.?limit|abuse detection|too quickly|retry-after", re.IGNORECASE)


def is_refusal(message):
    """True when GitHub refused a call for a rate limit."""
    return bool(REFUSAL.search(message or ""))


def spent_limit_reset(run=subprocess.run):
    """When the spent limit comes back, read with `gh api rate_limit`, which costs nothing. None if neither the
    GraphQL nor the REST limit is spent, or GitHub's answer cannot be read."""
    try:
        result = run(["gh", "api", "rate_limit"], capture_output=True, text=True, timeout=LOOKUP_TIMEOUT_SECONDS,
                     env=ghmeter.env(ghmeter.SAMPLE_CALLER))
        resources = json.loads(result.stdout)["resources"]
        spent = [r["reset"] for name, r in resources.items()
                 if name in ("graphql", "core") and isinstance(r, dict) and r.get("remaining") == 0]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return max(spent) if spent else None


class Gate:
    """The one wait every Kandu process honors before it calls GitHub."""

    def __init__(self, path=GATE_PATH, clock=time.time, lookup=spent_limit_reset):
        self.path = Path(path)
        self.clock = clock
        self.lookup = lookup

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def blocked(self):
        """The refusal in force, {"until", "why", "refusals"}, or None when a call may go."""
        data = self._load()
        if data and isinstance(data.get("until"), (int, float)) and data["until"] > self.clock():
            return data
        return None

    def refused(self, message):
        """Records a refusal and returns it, as `blocked` would: until when no call goes out, and why."""
        now = self.clock()
        refusals = ((self._load() or {}).get("refusals") or 0) + 1
        reset = self.lookup()
        if reset and reset > now:
            until = reset + 1
        else:
            until = now + min(FIRST_WAIT_SECONDS * 2 ** (refusals - 1), LONGEST_WAIT_SECONDS)
        text = (message or "").strip()
        why = text.splitlines()[0][:WHY_LENGTH] if text else "rate limit"
        data = {"until": int(until), "refusals": refusals, "why": why}
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        os.replace(temporary, self.path)
        return data

    def cleared(self):
        """A call went through: the next refusal starts from the shortest wait again."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def say(data):
        at = datetime.fromtimestamp(data["until"]).astimezone().strftime("%H:%M:%S")
        return f"GitHub's rate limit: Kandu calls it again at {at} ({data.get('why', '')})"


GATE = Gate()


def main(argv=None):
    """`refused <path>`: records GitHub's answer on stdin as a refusal, if it is one. Prints the wait."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] != "refused":
        print("usage: python -m kandu.ghlimit refused <backoff.json> < answer", file=sys.stderr)
        return 2
    message = sys.stdin.read()
    if is_refusal(message):
        print(json.dumps(Gate(Path(argv[1])).refused(message)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
