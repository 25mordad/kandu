"""Requests the user made that are not tasks yet (DESIGN §13).

Didban saves a request before it refines it, so a closed session loses nothing.
It leaves this list when its tasks exist: from there the board keeps it.
"""
import argparse
import hashlib
import sys

from .state import StateStore


class IntakeError(Exception):
    pass


def add(store, text):
    """Returns the id. The same words give the same id, so saving twice keeps one request."""
    text = " ".join(text.split())
    if not text:
        raise IntakeError("a request needs text")
    request_id = hashlib.sha256(text.encode("utf-8")).hexdigest()[:6]
    store.write_request({"id": request_id, "text": text})
    return request_id


def close(store, request_id):
    if not any(request["id"] == request_id for request in store.requests()):
        raise IntakeError(f"no open request {request_id}")
    store.drop_request(request_id)


def main():
    parser = argparse.ArgumentParser(description="Keep what the user asked for until it is tasks.")
    parser.add_argument("command", choices=("list", "add", "close"))
    parser.add_argument("id", nargs="?", help="the request's id, from `add` or `list`")
    args = parser.parse_args()
    store = StateStore()
    try:
        if args.command == "list":
            for request in store.requests():
                print(f"{request['id']}: {request['text']}")
        elif args.command == "add":
            print(add(store, sys.stdin.read()))  # On stdin, out of reach of a shell.
        elif args.id is None:
            parser.error("close needs a request id")
        else:
            close(store, args.id)
    except IntakeError as e:
        print(f"intake: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
