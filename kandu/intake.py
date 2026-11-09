"""Requests the user made that are not tasks yet (DESIGN §13).

Didban saves a request before it refines it, so a closed session loses nothing.
It leaves this list when its tasks exist: from there the board keeps it.
"""
import argparse
import hashlib
import sys

from .state import StateStore


# A decision map (DESIGN §13): each line starts with one mark, and the stored map keeps a list a mark.
MARKS = {"✅": "decided", "❓": "asked", "⏳": "later", "✂️": "out"}
VARIATION = "\ufe0f"  # ✂️ is ✂ with it, and a mark may come with or without it.


class IntakeError(Exception):
    pass


def add(store, text):
    """Returns the id. The same words give the same id, so saving twice keeps one request and its map."""
    text = " ".join(text.split())
    if not text:
        raise IntakeError("a request needs text")
    request_id = hashlib.sha256(text.encode("utf-8")).hexdigest()[:6]
    if _find(store, request_id) is None:
        store.write_request({"id": request_id, "text": text})
    return request_id


def _find(store, request_id):
    return next((request for request in store.requests() if request["id"] == request_id), None)


def _open(store, request_id):
    request = _find(store, request_id)
    if request is None:
        raise IntakeError(f"no open request {request_id}")
    return request


def parse_map(text):
    """Refuses a map not in the expected shape, saying why."""
    decision_map = {name: [] for name in MARKS.values()}
    lines = [(n, " ".join(line.split())) for n, line in enumerate(text.splitlines(), 1)]
    lines = [(n, line) for n, line in lines if line]
    if not lines:
        raise IntakeError("the map is empty")
    for n, line in lines:
        mark = next((mark for mark in MARKS if line.startswith(mark.rstrip(VARIATION))), None)
        if mark is None:
            raise IntakeError(f"map line {n} does not start with one of {' '.join(MARKS)}")
        entry = line[len(mark.rstrip(VARIATION)):].lstrip(VARIATION).strip()
        if not entry:
            raise IntakeError(f"map line {n} has a mark and no text")
        decision_map[MARKS[mark]].append(entry)
    return decision_map


def save_map(store, request_id, text):
    """Replaces the request's earlier map."""
    request = _open(store, request_id)
    store.write_request({**request, "map": parse_map(text)})


def close(store, request_id):
    _open(store, request_id)
    store.drop_request(request_id)


def listing(store):
    """One line a request, its map on the same line, so Didban opens with both."""
    lines = []
    for request in store.requests():
        decision_map = request.get("map")
        entries = [f"{mark} {entry}" for mark, name in MARKS.items() for entry in (decision_map or {}).get(name, [])]
        lines.append(f"{request['id']}: {request['text']}" + (f" [map: {' | '.join(entries)}]" if entries else ""))
    return lines


def main():
    parser = argparse.ArgumentParser(description="Keep what the user asked for until it is tasks.")
    parser.add_argument("command", choices=("list", "add", "map", "close"))
    parser.add_argument("id", nargs="?", help="the request's id, from `add` or `list`")
    args = parser.parse_args()
    store = StateStore()
    try:
        if args.command == "list":
            for line in listing(store):
                print(line)
        elif args.command == "add":
            print(add(store, sys.stdin.read()))  # On stdin, out of reach of a shell.
        elif args.id is None:
            parser.error(f"{args.command} needs a request id")
        elif args.command == "map":
            save_map(store, args.id, sys.stdin.read())
        else:
            close(store, args.id)
    except IntakeError as e:
        print(f"intake: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
