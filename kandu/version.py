"""Kandu's version, read from the `VERSION` file at the root of the repo, so a running install knows which it is (DESIGN §12)."""
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def read(path=VERSION_FILE):
    """The version written in `path`, such as `1.0.0`, or None when the file is missing."""
    try:
        return Path(path).read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None
