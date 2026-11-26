"""Prints what Kandu's API serves at one path, by a GET (DESIGN §13). It changes nothing.

    bin/kandu-api tasks/OWNER/REPO/NUMBER
    bin/kandu-api boards

It takes one path under the API, and no URL, flag, file or query: the job check reads Kandu's API with this, so
its allowed command can neither reach another address nor write a file nor send data.
"""
import argparse
import sys
import urllib.error
import urllib.request

from .config import DEFAULT_LOCAL_DIR, ConfigError, load_config
from .flights import api_url

SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
TIMEOUT_SECONDS = 30


class PathError(ValueError):
    pass


def checked(path):
    """`path` as a path under the API, or PathError: segments of letters, digits, `_`, `.` and `-` only."""
    parts = path.strip("/").split("/")
    if not path.strip("/") or any(not part or part in (".", "..") or not set(part) <= SAFE for part in parts):
        raise PathError(f"{path!r} is not a path under Kandu's API, such as boards or tasks/OWNER/REPO/NUMBER")
    return "/".join(parts)


def read(api, path, open_url=urllib.request.urlopen):
    """The body Kandu's API serves at `path`, by a GET."""
    request = urllib.request.Request(f"{api}/{checked(path)}", method="GET")
    with open_url(request, timeout=TIMEOUT_SECONDS) as reply:
        return reply.read().decode("utf-8", "replace")


def main(argv=None, local_dir=DEFAULT_LOCAL_DIR):
    parser = argparse.ArgumentParser(description="Print what Kandu's API serves at one path, by a GET.")
    parser.add_argument("path", help="a path under the API, such as boards or tasks/OWNER/REPO/NUMBER")
    args = parser.parse_args(argv)
    try:
        path = checked(args.path)
        config = load_config(local_dir)
        print(read(api_url(config), path), end="")
    except PathError as e:
        print(f"kandu-api: {e}", file=sys.stderr)
        return 2
    except ConfigError as e:
        print(f"kandu-api: {len(e.problems)} problem(s) in config; run: python -m kandu.config", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as e:
        print(f"kandu-api: {e.code}: {e.read().decode('utf-8', 'replace')}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"kandu-api: Kandu's API did not answer: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
