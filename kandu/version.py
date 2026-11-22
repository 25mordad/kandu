"""The install's version, whether it is the newest release of its public upstream repo, and the upgrade notice
(DESIGN §12).

The newest release is read from GitHub at most once a day, by the dashboard's own clock, and kept in the log;
the page reads it from Kandu, never from GitHub. It only reads: nothing here updates the install. The upgrade notice
reads the install's `CHANGELOG.md` and the read mark the user left in Kandu's state.
"""
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import upstream
from .github import GitHubError

VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
EVERY_HOURS = 24  # one read a day, a failed one too, so a dead network or a missing release is not asked again
REASON_CHARS = 200
GITHUB_REMOTE = re.compile(r"^(?:(?:https?|ssh)://(?:[^@/]*@)?|git@)github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
CHANGELOG_FILE = "CHANGELOG.md"
ENTRY_HEAD = re.compile(r"^## (\S+)")
RELEASE_PAGE = "https://github.com/{repo}/releases/tag/v{version}"


def installed(install_dir=upstream.INSTALL_DIR):
    """The install's own version, from the `VERSION` file at its root; None when the file is missing or empty."""
    try:
        text = (Path(install_dir) / VERSION_FILE.name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def read(path=VERSION_FILE):
    """The version written in `path`, such as `1.0.0`, or None when the file is missing."""
    try:
        return Path(path).read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def upstream_repo(settings, install_dir=upstream.INSTALL_DIR):
    """`owner/repo` of the public upstream on GitHub, from the install's git remote; None while the upstream check is
    off, or when the remote is missing or not on GitHub."""
    if not settings.enabled:
        return None
    url = upstream.public_url(install_dir, settings.remote)
    found = GITHUB_REMOTE.match(url or "")
    return f"{found.group(1)}/{found.group(2)}" if found else None


def _number(text):
    found = SEMVER.match((text or "").strip())
    return tuple(int(part) for part in found.groups()) if found else None


def is_newer(latest, mine):
    """True when the release `latest` (a tag such as `v1.2.0`) is newer than the install's `mine`."""
    a, b = _number(latest), _number(mine)
    if a is None or b is None:
        return (latest or "").strip().lstrip("v") != (mine or "").strip().lstrip("v")
    return a > b


def last_try(store):
    """The newest read, good or failed. By its place in the log: two in one second have the same time."""
    for day in reversed(store.days()):
        for event in reversed(store.events(day)):
            if event["type"] in ("release_read", "release_read_failed"):
                return event
    return None


def due(store, now):
    last = last_try(store)
    return last is None or now - datetime.fromisoformat(last["ts"]) >= timedelta(hours=EVERY_HOURS)


def read_latest(store, github, repo, now=None):
    """Reads the newest release once and logs it: `release_read`, or `release_read_failed` with GitHub's reason."""
    try:
        release = github.latest_release(repo)
    except GitHubError as e:
        return store.record("release_read_failed", now=now, repo=repo, reason=str(e)[:REASON_CHARS])
    return store.record("release_read", now=now, repo=repo, tag=release["tag"], url=release["url"])


def status(store, repo, install_dir=upstream.INSTALL_DIR):
    """What the header's badge shows, from the log alone: the install's version, and the newest release of `repo` when
    the last read found one. `latest` is None without an upstream or after a failed read; `tried_at` says when it last
    tried."""
    mine = installed(install_dir)
    last = last_try(store)
    good = repo is not None and last is not None and last["type"] == "release_read" and last["repo"] == repo
    latest = last["tag"] if good else None
    return {"installed": mine, "upstream": repo, "latest": latest, "url": last["url"] if good else None,
            "up_to_date": None if latest is None or mine is None else not is_newer(latest, mine),
            "tried_at": last and last["ts"], "failed": last is not None and last["type"] == "release_read_failed"}


def changelog_entries(text):
    """Each entry of a changelog, in the file's order, as (version, its text as written): from its `## X.Y.Z - date`
    line to the next entry, without the blank lines at its end."""
    entries, version, lines = [], None, []
    for line in (text or "").splitlines():
        head = ENTRY_HEAD.match(line)
        if head:
            if version is not None:
                entries.append((version, "\n".join(lines).rstrip()))
            version, lines = head.group(1), []
        if version is not None:
            lines.append(line)
    if version is not None:
        entries.append((version, "\n".join(lines).rstrip()))
    return entries


def _unread(entry, mine, mark):
    """Whether a changelog entry's version is one the user has not read: newer than the read mark and not newer than
    the install. With no mark yet, only the install's own version is unread."""
    if mark is None or _number(entry) is None or _number(mine) is None:
        return entry == mine and entry != mark
    return _number(mark) < _number(entry) <= _number(mine)


def _release_url(number, badge):
    """The page of a version's release on the public upstream, when it is published: the newest release the last
    read found, or an older version, which Kandu's deploy published before it."""
    latest = _number(badge["latest"]) if badge and badge["latest"] else None
    if latest is None or _number(number) is None or _number(number) > latest:
        return None
    if _number(number) == latest:
        return badge["url"]
    return RELEASE_PAGE.format(repo=badge["upstream"], version=number)


def upgrade_notice(store, badge, install_dir=upstream.INSTALL_DIR):
    """The upgrade notice: the installed `version`, the `entries` of each version the user has not marked as read,
    newest first, each with its `version`, `text` as the changelog writes it and its release's `url` or None, and
    `read`, the newest version shown, which «Mark as read» marks. None when nothing is unread, or no unread version
    has an entry in the install's changelog. `badge` is the version badge's status, for the release links."""
    mine = installed(install_dir)
    mark = store.version_read()
    if mine is None or (mark is not None and not is_newer(mine, mark)):
        return None
    try:
        text = (Path(install_dir) / CHANGELOG_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    found = [(number, entry) for number, entry in changelog_entries(text) if _unread(number, mine, mark)]
    if not found:
        return None
    found.sort(key=lambda pair: _number(pair[0]) or (), reverse=True)
    return {"version": mine, "read": found[0][0],
            "entries": [{"version": n, "text": e, "url": _release_url(n, badge)} for n, e in found]}


class Releases:
    """The daily read, with its lock, so the clock and a second dashboard thread read one at a time. It keeps the
    upstream repo its clock last worked out, so a page read runs no git (DESIGN §12)."""

    def __init__(self, install_dir=upstream.INSTALL_DIR):
        self.install_dir = Path(install_dir)
        self._lock = threading.Lock()
        self._repo = None  # ((enabled, remote), repo): the settings it was worked out for, and what it found.

    def _work_out(self, settings):
        repo = upstream_repo(settings, self.install_dir)
        self._repo = ((settings.enabled, settings.remote), repo)
        return repo

    def repo(self, settings):
        """The upstream repo, as the clock last worked it out; worked out here only before the first tick, or when the
        upstream settings changed since."""
        kept = self._repo
        if kept is not None and kept[0] == (settings.enabled, settings.remote):
            return kept[1]
        if not settings.enabled:
            return None
        return self._work_out(settings)

    def status(self, store, settings):
        return status(store, self.repo(settings), self.install_dir)

    def upgrade(self, store, settings):
        return upgrade_notice(store, self.status(store, settings), self.install_dir)

    def tick(self, store, github, settings, now=None):
        """Reads when a day passed since the last try. Returns True when it read. Without an upstream it reads nothing."""
        now = now or datetime.now().astimezone()
        with self._lock:
            repo = self._work_out(settings)
            if repo is None or not due(store, now):
                return False
            read_latest(store, github, repo, now)
            return True
